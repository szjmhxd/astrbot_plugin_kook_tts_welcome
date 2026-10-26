"""KOOK TTS 语音欢迎插件 —— 插件入口。

当成员进入 KOOK 语音频道时，机器人自动加入频道，**按用户昵称实时合成**
欢迎语音并播放，播放完成后立即离开。

功能
----
- 默认按用户昵称生成欢迎语音（TTS，占位符 ``{nickname}`` / ``{username}`` /
  ``{user_id}``，文案模板可配置）；
- 支持指定用户定制文案（``/语音定制`` 指令，覆盖默认模板）；
- 多状态播报骨架：进入 / 退出各有独立文案模板；静音、闭麦状态的模板与
  分发入口已预留（KOOK 网关暂不推送这两类事件，见 README「已知限制」）；
- 播报自动排除机器人自己（进入 / 退出事件都不会触发自己的播报）。

模块结构
--------
- ``constants.py``   全局常量
- ``utils.py``       类型转换、子进程执行、ffprobe 封装
- ``kook_api.py``    KOOK HTTP 客户端（语音接口 + 用户信息）
- ``ffmpeg.py``      FFmpeg RTP 推流命令拼装
- ``tts.py``         昵称解析、文案渲染、TTS 合成与缓存
- ``config.py``      配置读取与诊断日志
- ``hooking.py``     KOOK 适配器事件挂接
- ``playback.py``    播报队列与推流流程
- ``resources.py``   Token / FFmpeg 资源解析

开发者文档：https://docs.astrbot.app/dev/star/plugin-new.html
KOOK 语音接口：https://developer.kookapp.cn/doc/http/voice
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .config import ConfigMixin
from .constants import (
    DEFAULT_ENTER_TEMPLATE,
    DEFAULT_EXIT_TEMPLATE,
    PLUGIN_NAME,
    PLUGIN_VERSION,
    STATE_ENTER,
    STATE_EXIT,
    STATES_WITH_EVENTS,
)
from .hooking import HookingMixin, VoiceStateEvent, _Hook, extract_voice_state_event
from .kook_api import KookApi
from .playback import PlaybackMixin, _PlayRequest
from .resources import ResourcesMixin
from .tts import TtsMixin


@register(
    PLUGIN_NAME,
    "szjm",
    "成员进入 KOOK 语音频道时，按用户昵称实时合成欢迎语音并播放",
    PLUGIN_VERSION,
)
class KookTtsWelcomePlugin(
    TtsMixin,
    ResourcesMixin,
    PlaybackMixin,
    HookingMixin,
    ConfigMixin,
    Star,
):
    """插件主体：生命周期 + 指令 handler，实现分布在各 Mixin 中。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.config = config

        self._data_dir = self._resolve_data_dir()
        self._plugin_dir = Path(__file__).resolve().parent
        self._log_file = self._data_dir / "logs" / "kook_tts_welcome.log"
        self._api = KookApi()

        self._queue: asyncio.Queue[_PlayRequest] = asyncio.Queue(maxsize=1)
        self._worker: asyncio.Task | None = None
        self._attach_task: asyncio.Task | None = None
        self._ffmpeg_task: asyncio.Task | None = None

        self._hooks: dict[str, _Hook] = {}
        self._bot_id = ""
        self._voice_channel = ""
        self._playing = False
        self._last_finished = 0.0
        self._ffmpeg_cache: tuple[bool, str] | None = None
        self._ffmpeg_proc: asyncio.subprocess.Process | None = None
        self._stream_aborted = False

        # TTS 模块使用的缓存（OrderedDict 以便按插入顺序淘汰）
        self._nickname_cache: OrderedDict[str, tuple[float, dict[str, str]]] = (
            OrderedDict()
        )
        self._user_templates: dict[str, Any] = self._load_user_templates()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self) -> None:
        """启动后台任务，并尝试挂接已经加载的 KOOK 适配器。"""
        self._log_startup()
        self._worker = asyncio.create_task(
            self._worker_loop(), name=f"{PLUGIN_NAME}-worker"
        )
        self._attach_task = asyncio.create_task(
            self._attach_loop(), name=f"{PLUGIN_NAME}-attach"
        )
        self._attach_all()
        self._ffmpeg_task = asyncio.create_task(
            self._log_ffmpeg_state(), name=f"{PLUGIN_NAME}-ffmpeg"
        )

    async def terminate(self) -> None:
        """还原回调、停止任务并释放语音连接。"""
        for platform_id in list(self._hooks):
            self._detach(platform_id)

        for task in (self._attach_task, self._worker, self._ffmpeg_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        token = self._resolve_token()
        channel = self._voice_channel
        proc = self._ffmpeg_proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        self._ffmpeg_proc = None

        if token and channel:
            left = await self._leave_voice(token, channel)
            if not left:
                with contextlib.suppress(Exception):
                    await self._release_stale_voice(token, channel)
        self._voice_channel = ""

        await self._api.close()
        self._trace("插件卸载：已还原回调并释放语音资源")
        logger.info("[KOOK TTS欢迎] 插件已卸载，语音资源已释放")

    # ------------------------------------------------------------------ #
    # KOOK 事件分发
    # ------------------------------------------------------------------ #

    async def _on_kook_event(self, adapter: Any, data: Any) -> None:
        """处理包装回调捕获到的每一条 KOOK 网关事件。"""
        event = extract_voice_state_event(data)
        if event is None:
            return

        bot_id = self._resolve_bot_id()
        is_self = bool(bot_id) and event.user_id == bot_id

        # 退出事件优先处理：机器人被移出频道时要能立刻止损推流
        if event.state == STATE_EXIT and is_self:
            self._on_bot_left_voice(event.channel_id)
            return

        # 排除自己：机器人自身进出语音不播报
        if is_self:
            self._trace(f"忽略：{event.state} 事件来自机器人自己")
            return

        if not self._enabled:
            return
        if event.state not in STATES_WITH_EVENTS:
            self._trace(f"忽略：状态 {event.state} 暂无网关事件来源")
            return
        if event.state == STATE_EXIT and not self._play_exit:
            self._trace("忽略：退出播报未开启")
            return

        self._trace(
            f"收到语音事件：state={event.state} user={event.user_id} "
            f"channel={event.channel_id} guild={event.guild_id}"
        )

        target = self._target_channel
        if target and event.channel_id != target:
            logger.debug(f"[KOOK TTS欢迎] 忽略非目标频道 {event.channel_id} 的事件")
            self._trace(f"忽略：频道 {event.channel_id} 不是目标频道 {target}")
            return

        if self._playing or not self._queue.empty():
            logger.debug("[KOOK TTS欢迎] 正在播报中，忽略新的语音事件")
            self._trace("忽略：正在播报中")
            return

        if self._last_finished and time.monotonic() - self._last_finished < self._cooldown:
            logger.debug("[KOOK TTS欢迎] 仍在冷却时间内，忽略本次事件")
            self._trace("忽略：仍在冷却时间内")
            return

        self._enqueue(_PlayRequest(event=event))
        logger.info(
            f"[KOOK TTS欢迎] 用户 {event.user_id} {event.state} 语音频道 "
            f"{event.channel_id}，准备播报"
        )
        self._trace(f"已加入播报队列：state={event.state} user={event.user_id}")

        # 预取昵称：与队列调度并行，worker 启动时多半已命中缓存
        asyncio.create_task(
            self._prefetch_nickname(event.user_id, event.guild_id),
            name=f"{PLUGIN_NAME}-prefetch",
        )

    @filter.on_platform_loaded()
    async def _on_platform_loaded(self) -> None:
        """平台适配器加载完成后立即尝试挂接。"""
        self._attach_all()

    # ------------------------------------------------------------------ #
    # 指令：状态与帮助
    # ------------------------------------------------------------------ #

    @filter.command("语音欢迎")
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    async def cmd_status(self, event: AstrMessageEvent):
        """查看 KOOK TTS 语音欢迎插件的当前状态"""
        ffmpeg_ok, ffmpeg_detail = await self._check_ffmpeg()
        token_ready = "已配置" if self._resolve_token() else "未获取"
        adapters = len(self._iter_kook_adapters())

        if self._playing:
            state = f"正在播报（频道 {self._voice_channel or '未知'}）"
        elif self._voice_channel:
            state = f"占用语音频道 {self._voice_channel}"
        else:
            state = "空闲"

        lines = [
            "🎧 KOOK TTS 语音欢迎",
            f"插件状态：{'已启用' if self._enabled else '已关闭'}",
            f"监听频道：{self._target_channel or '自动跟随事件频道'}",
            f"进入文案：{self._state_template(STATE_ENTER) or '（未配置）'}",
            f"退出文案：{self._state_template(STATE_EXIT) or '（未配置）'}"
            + ("" if self._play_exit else "［退出播报已关闭］"),
            f"定制用户：{len(self._user_templates)} 人",
            f"音量：{self._volume} / 冷却：{self._cooldown} 秒",
            f"启动延迟：{self._start_delay} 秒 / 尾部静音：{self._leave_delay} 秒",
            f"FFmpeg：{'可用' if ffmpeg_ok else '不可用'}"
            + ("" if ffmpeg_ok else f"（{ffmpeg_detail}）"),
            f"Bot Token：{token_ready} / KOOK 适配器：{adapters} 个",
            f"运行状态：{state}",
            f"诊断日志：{self._log_file}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("语音欢迎帮助")
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    async def cmd_help(self, event: AstrMessageEvent):
        """查看 KOOK TTS 语音欢迎插件指令列表"""
        lines = [
            "🎧 KOOK TTS 语音欢迎 · 指令帮助",
            "",
            "普通指令",
            "· /语音欢迎 —— 查看插件状态",
            "· /语音欢迎帮助 —— 查看本帮助",
            "",
            "管理员指令",
            "· /语音欢迎模板 enter|exit <文案> —— 设置状态播报模板",
            "· /语音欢迎定制 <用户ID> enter|exit <文案> —— 指定用户定制文案",
            "· /语音欢迎定制删除 <用户ID> —— 删除该用户的定制文案",
            "· /语音欢迎试听 [文案] —— 合成一段语音并在频道里试听",
            "· /语音欢迎重载 —— 重新读取配置与定制文案",
            "",
            "模板占位符：{nickname} 服务器昵称 / {username} 用户名 / {user_id}",
            f"默认进入模板：{DEFAULT_ENTER_TEMPLATE}",
            f"默认退出模板：{DEFAULT_EXIT_TEMPLATE}",
            "",
            "注意：KOOK 网关只推送「进入 / 退出」语音频道事件，",
            "静音 / 闭麦状态官方无事件可监听（详见 README）。",
        ]
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ #
    # 指令：管理员
    # ------------------------------------------------------------------ #

    @filter.command("语音欢迎模板")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    async def cmd_template(self, event: AstrMessageEvent):
        """设置状态播报模板：/语音欢迎模板 enter|exit <文案>"""
        parts = self._strip_command(event.message_str, "语音欢迎模板", maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result(
                "用法：/语音欢迎模板 enter|exit <文案>\n"
                "占位符：{nickname} {username} {user_id}"
            )
            return

        state = parts[0].lower()
        text = parts[1].strip()
        if state not in ("enter", "exit"):
            yield event.plain_result("状态只支持 enter（进入）或 exit（退出）")
            return
        if not text:
            yield event.plain_result("文案不能为空；填「清空」可关闭该状态播报")
            return

        if text in ("清空", "clear", "none"):
            text = ""
        self.config[f"template_{state}"] = text
        saved = self._save_config(f"设置 {state} 模板")
        label = "进入" if state == STATE_ENTER else "退出"
        shown = text or "（已关闭）"
        yield event.plain_result(
            f"{'已设置' if saved else '设置失败（配置写入出错）'}：{label}文案 = {shown}"
        )

    @filter.command("语音欢迎定制")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    async def cmd_user_template(self, event: AstrMessageEvent):
        """指定用户定制文案：/语音欢迎定制 <用户ID> enter|exit <文案>"""
        parts = self._strip_command(event.message_str, "语音欢迎定制", maxsplit=2)
        if len(parts) < 3:
            yield event.plain_result(
                "用法：/语音欢迎定制 <用户ID> enter|exit <文案>\n"
                "例：/语音欢迎定制 123456 enter 欢迎大佬 {nickname} 闪亮登场"
            )
            return

        user_id, state, text = parts[0], parts[1].lower(), parts[2].strip()
        if not user_id.isdigit():
            yield event.plain_result("用户 ID 必须是纯数字（KOOK 用户 ID）")
            return
        if state not in ("enter", "exit"):
            yield event.plain_result("状态只支持 enter 或 exit")
            return
        if not text:
            yield event.plain_result("文案不能为空")
            return

        entry = self._user_templates.get(user_id)
        if not isinstance(entry, dict):
            entry = {}
            self._user_templates[user_id] = entry
        entry[state] = text
        saved = self._save_user_templates()
        label = "进入" if state == STATE_ENTER else "退出"
        yield event.plain_result(
            f"{'已设置' if saved else '保存失败'}：用户 {user_id} 的{label}定制文案 = {text}"
        )

    @filter.command("语音欢迎定制删除")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    async def cmd_user_template_delete(self, event: AstrMessageEvent):
        """删除指定用户的定制文案：/语音欢迎定制删除 <用户ID>"""
        parts = self._strip_command(event.message_str, "语音欢迎定制删除")
        if not parts or not parts[0].isdigit():
            yield event.plain_result("用法：/语音欢迎定制删除 <用户ID>")
            return

        user_id = parts[0]
        if user_id not in self._user_templates:
            yield event.plain_result(f"用户 {user_id} 没有定制文案")
            return
        del self._user_templates[user_id]
        saved = self._save_user_templates()
        yield event.plain_result(
            f"{'已删除' if saved else '删除失败（写入出错）'}：用户 {user_id} 的定制文案"
        )

    @filter.command("语音欢迎试听")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    async def cmd_preview(self, event: AstrMessageEvent):
        """试听：/语音欢迎试听 [文案] —— 合成并推流到事件所在语音频道"""
        text = self._strip_command(event.message_str, "语音欢迎试听", maxsplit=1)
        speak = text[0].strip() if text else ""
        if not speak:
            names = {"nickname": "试听用户", "username": "preview"}
            speak = self._build_speak_text(STATE_ENTER, "0", names) or "这是一条试听语音"

        channel_id = self._target_channel
        user_id = getattr(event, "get_sender_id", lambda: "")()
        guild_id = ""
        try:
            guild_id = str(getattr(event, "get_group_id", lambda: "")() or "")
        except Exception:
            guild_id = ""

        request = _PlayRequest(
            event=VoiceStateEvent(
                state=STATE_ENTER,
                user_id=str(user_id or "0"),
                channel_id=channel_id,
                guild_id=guild_id,
            ),
            reason="preview",
            override_text=speak,
        )
        if not channel_id:
            # 事件没有带出语音频道时，让播报流程用配置里的目标频道；
            # 两者都空则播报会失败并写诊断日志。
            self._trace(f"试听请求：channel 未知（配置={self._target_channel or '空'}）")

        self._enqueue(request)
        yield event.plain_result(
            f"已提交试听：「{speak}」\n"
            f"目标频道：{channel_id or '未指定（请先配置目标语音频道 ID）'}"
        )

    @filter.command("语音欢迎重载")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    async def cmd_reload(self, event: AstrMessageEvent):
        """重新读取配置、定制文案并检测 FFmpeg"""
        self._user_templates = self._load_user_templates()
        self._ffmpeg_cache = None
        self._nickname_cache.clear()
        ok, detail = await self._check_ffmpeg()
        self._attach_all()
        yield event.plain_result(
            f"已重载：定制用户 {len(self._user_templates)} 人；"
            f"FFmpeg {'可用（' + detail + '）' if ok else '不可用（' + detail + '）'}"
        )

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #

    @staticmethod
    def _strip_command(message_str: str, command: str, maxsplit: int = -1) -> list[str]:
        """去掉指令名（含可能的前缀）后按空白切分参数。"""
        text = (message_str or "").strip()
        text = re.sub(rf"^[~/!#]{{0,2}}{re.escape(command)}\s*", "", text)
        if not text:
            return []
        return text.split(None, maxsplit) if maxsplit >= 0 else text.split()
