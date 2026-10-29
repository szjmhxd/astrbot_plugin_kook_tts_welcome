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
    DEFAULT_MUTE_POLL_SECONDS,
    MUTE_POLL_MIN_SECONDS,
    PLUGIN_NAME,
    PLUGIN_VERSION,
    POLICY_IGNORE,
    POLICY_INTERRUPT,
    POLICY_MERGE,
    STATE_ENTER,
    STATE_VOICE_TEMPLATE_PATH,
    STATE_EXIT,
    STATE_LABELS,
    STATE_MIC_OFF,
    STATE_MIC_ON,
    STATE_MUTE,
    STATE_UNMUTE,
    STATES_SUPPORTED,
    STATES_WITH_POLLING,
)
from .hooking import (
    HookingMixin,
    VoiceStateEvent,
    _Hook,
    describe_unmatched_event,
    extract_voice_state_event,
)
from .kook_api import KookApi
from .mute import MuteWatchMixin
from .notify import NotifyMixin
from .playback import PlaybackMixin, _PlayRequest
from .resources import ResourcesMixin
from .tts import TtsMixin
from .utils import to_text


@register(
    PLUGIN_NAME,
    "szjm",
    "成员进入 KOOK 语音频道时，按用户昵称实时合成欢迎语音并播放",
    PLUGIN_VERSION,
)
class KookTtsWelcomePlugin(
    TtsMixin,
    NotifyMixin,
    MuteWatchMixin,
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
        self._mute_task: asyncio.Task | None = None

        self._hooks: dict[str, _Hook] = {}
        self._bot_id = ""
        self._voice_channel = ""
        self._playing = False
        self._last_finished = 0.0
        self._gateway_events = 0  # 本次启动收到的网关事件数（区分「没事件」与「没认出来」）
        self._scope_warned: set[str] = set()  # 已提示过「不在启用范围」的服务器，防刷屏
        self._ffmpeg_cache: tuple[bool, str] | None = None
        self._ffmpeg_proc: asyncio.subprocess.Process | None = None
        self._stream_aborted = False

        # TTS 模块使用的缓存（OrderedDict 以便按插入顺序淘汰）
        self._nickname_cache: OrderedDict[str, tuple[float, dict[str, str]]] = (
            OrderedDict()
        )
        self._channel_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._user_templates: dict[str, Any] = self._load_user_templates()

        # 文字播报：每服务器绑定表与解析缓存
        self._notify_channels: dict[str, str] = self._load_notify_channels()
        self._target_channels: dict[str, str] = self._load_target_channels()
        self._guild_info_cache: dict[str, tuple[float, dict]] = {}
        self._text_channel_cache: dict[str, tuple[float, str, str]] = {}

        # 静音 / 闭麦轮询：名单快照、活动服务器、成员所在频道
        self._mute_state: dict[str, dict[str, set[str]]] = {}
        self._guild_activity: dict[str, float] = {}
        self._user_channels: dict[tuple[str, str], tuple[float, str]] = {}
        self._guild_last_channel: dict[str, tuple[float, str]] = {}

        # 合并播报缓冲与调度
        self._merge_buffer: list[VoiceStateEvent] = []
        self._merge_task: asyncio.Task | None = None
        self._interrupted = False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self) -> None:
        """启动后台任务，并尝试挂接已经加载的 KOOK 适配器。"""
        self._migrate_config()
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
        self._mute_task = asyncio.create_task(
            self._mute_loop(), name=f"{PLUGIN_NAME}-mute"
        )

    async def terminate(self) -> None:
        """还原回调、停止任务并释放语音连接。"""
        for platform_id in list(self._hooks):
            self._detach(platform_id)

        for task in (
            self._attach_task,
            self._worker,
            self._ffmpeg_task,
            self._mute_task,
            self._merge_task,
        ):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._merge_task = None
        self._mute_task = None
        self._merge_buffer.clear()

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
        self._gateway_events += 1
        event = extract_voice_state_event(data)
        if event is None:
            # 提取失败是静默路径，不留痕就分不清「事件没到」和「到了但没认出来」
            unmatched = describe_unmatched_event(data)
            if unmatched:
                logger.warning(f"[KOOK TTS欢迎] {unmatched}")
                self._trace(unmatched)
            return
        self._handle_state_event(event, source="gateway")

    def _handle_state_event(
        self, event: VoiceStateEvent, source: str = "gateway"
    ) -> None:
        """统一的状态事件入口：网关事件与静音轮询共用。

        ``source`` 只影响日志措辞与「该状态有没有网关事件来源」的校验。
        """
        if event.guild_id:
            self._note_guild_activity(event.guild_id)
        if event.state in (STATE_ENTER, STATE_EXIT):
            self._track_user_voice(event)

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
        if not event.guild_id and self._enabled_guild_ids:
            # 已限定启用范围但事件没带服务器 ID：无法确认，按不放行处理
            self._trace(
                f"忽略：事件未携带服务器 ID，且已限定启用范围（state={event.state}）"
            )
            return
        if event.guild_id and not self._guild_enabled(event.guild_id):
            self._trace(f"忽略：服务器 {event.guild_id} 不在启用范围内")
            if event.guild_id not in self._scope_warned:
                self._scope_warned.add(event.guild_id)
                scope = "、".join(sorted(self._enabled_guild_ids)) or "（空）"
                logger.warning(
                    f"[KOOK TTS欢迎] 服务器 {event.guild_id} 的事件被「目标服务器 ID」"
                    f"过滤掉了（当前配置：{scope}）。上面这串才是服务器 ID；"
                    "如果填错了（比如填成 /sid 的 Session ID / 频道 ID），"
                    "请改成这串 ID，或清空该配置 = 所有服务器启用"
                )
            return
        if event.state not in STATES_SUPPORTED:
            self._trace(f"忽略：未知状态 {event.state}")
            return

        # 静音 / 闭麦：网关事件（add/delete_guild_mute）与轮询共用此入口，
        # 由「闭麦播报」「静音播报」两个开关分别把关（语音 + 文字一起控）
        if event.state in STATES_WITH_POLLING and not self._state_announce_enabled(
            event.state
        ):
            self._trace(
                f"忽略：{event.state} 播报未开启（user={event.user_id}）"
            )
            return

        # 静音 / 闭麦事件不带频道 ID：按成员最近所在的语音频道定位
        if event.state in STATES_WITH_POLLING and not event.channel_id:
            resolved = self._user_voice_channel(event.guild_id, event.user_id)
            if not resolved:
                self._trace(
                    f"忽略：{event.state} user={event.user_id} 找不到所在语音频道"
                )
                return
            event = VoiceStateEvent(
                state=event.state,
                user_id=event.user_id,
                channel_id=resolved,
                guild_id=event.guild_id,
            )

        self._trace(
            f"收到语音事件（{source}）：state={event.state} user={event.user_id} "
            f"channel={event.channel_id} guild={event.guild_id}"
        )

        target = self._target_channel_for(event.guild_id)
        if target and event.channel_id != target:
            logger.debug(f"[KOOK TTS欢迎] 忽略非目标频道 {event.channel_id} 的事件")
            self._trace(f"忽略：频道 {event.channel_id} 不是目标频道 {target}")
            return

        # 文字频道同步播报：独立于语音开关与策略，按人立即发送
        self._dispatch_text_notify(event)

        if event.state == STATE_ENTER and not self._welcome_voice_enabled:
            self._trace("忽略：欢迎语音未开启")
            return

        if event.state == STATE_EXIT and not self._exit_voice_enabled:
            self._trace("忽略：退出语音未开启")
            return

        policy = self._multi_user_policy

        # 合并策略：攒够窗口一次性播报
        if policy == POLICY_MERGE:
            self._buffer_for_merge(event)
            asyncio.create_task(
                self._prefetch_nickname(event.user_id, event.guild_id),
                name=f"{PLUGIN_NAME}-prefetch",
            )
            return

        # 打断策略：后进的立刻顶掉先进
        if policy == POLICY_INTERRUPT:
            if self._playing or not self._queue.empty():
                self._interrupt_playback()
            self._enqueue(_PlayRequest(events=[event], force=True))
            logger.info(
                f"[KOOK TTS欢迎] 用户 {event.user_id} {event.state} 语音频道 "
                f"{event.channel_id}，打断并播报"
            )
            self._trace(f"打断策略入队：state={event.state} user={event.user_id}")
            asyncio.create_task(
                self._prefetch_nickname(event.user_id, event.guild_id),
                name=f"{PLUGIN_NAME}-prefetch",
            )
            return

        # 忽略策略（默认保守行为）：播报中 / 冷却中的事件直接丢弃
        if self._playing or not self._queue.empty():
            logger.debug("[KOOK TTS欢迎] 正在播报中，忽略新的语音事件")
            self._trace("忽略：正在播报中")
            return

        if self._last_finished and time.monotonic() - self._last_finished < self._cooldown:
            logger.debug("[KOOK TTS欢迎] 仍在冷却时间内，忽略本次事件")
            self._trace("忽略：仍在冷却时间内")
            return

        self._enqueue(_PlayRequest(events=[event]))
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
        ctx = await self._channel_context(event)
        guild_id = ctx["guild_id"]
        blocked = await self._scope_gate(event, ctx)
        if blocked:
            yield event.plain_result(blocked)
            return

        ffmpeg_ok, ffmpeg_detail = await self._check_ffmpeg()
        token_ready = "已配置" if self._resolve_token() else "未获取"
        adapters = len(self._iter_kook_adapters())

        if self._playing:
            state = f"正在播报（频道 {self._voice_channel or '未知'}）"
        elif self._voice_channel:
            state = f"占用语音频道 {self._voice_channel}"
        else:
            state = "空闲"

        policy_label = {
            POLICY_IGNORE: "忽略后进的",
            POLICY_INTERRUPT: "后进的打断",
            POLICY_MERGE: f"合并（窗口 {self._merge_window_ms}ms）",
        }.get(self._multi_user_policy, self._multi_user_policy)

        # 本服务器的文字播报频道（按三级兜底实时解析）
        token = self._resolve_token()
        notify_channel, notify_source = ("", "")
        if token and guild_id:
            notify_channel, notify_source = await self._resolve_text_channel(
                token, guild_id
            )
        notify = (
            f"{notify_channel}（{notify_source}）"
            if notify_channel
            else "不会发送（本服务器没有可用文字频道）"
        )
        bound = self._notify_channels.get(guild_id, "") if guild_id else ""
        scope_ids = self._enabled_guild_ids
        if not scope_ids:
            scope_text = "全部服务器都启用（目标服务器 ID 为空）"
        elif not guild_id:
            scope_text = f"已限定 {len(scope_ids)} 个服务器（本服务器 ID 未知，无法判断）"
        elif self._guild_enabled(guild_id):
            scope_text = "本服务器在启用范围内 ✓"
        else:
            scope_text = (
                f"⚠️ 本服务器**不在**启用范围内，事件全部被忽略！"
                f"（目标服务器 ID 当前是：{'、'.join(sorted(scope_ids))}）"
            )
        mute = self._mute_poll_interval
        mute_label = f"每 {mute} 秒轮询服务器静音名单" if mute else "关闭"

        lines = [
            "🎧 KOOK TTS 语音欢迎",
            f"插件状态：{'已启用' if self._enabled else '已关闭'}",
            f"监听频道：{self._target_channel_for(guild_id) or '自动跟随事件频道'}",
            f"进入语音：{'开' if self._welcome_voice_enabled else '关'} / "
            f"退出语音：{'开' if self._exit_voice_enabled else '关'}",
            f"进入文案：{self._state_template(STATE_ENTER) or '（未配置）'}",
            f"退出文案：{self._state_template(STATE_EXIT) or '（未配置）'}",
            f"多人策略：{policy_label}",
            f"本服务器 ID：{guild_id or '未知'}　←「目标服务器 ID」填的就是这串数字",
            f"启用范围：{scope_text}",
            f"文字频道手动绑定：{bound or '无'}",
            f"文字播报：{notify}",
            f"静音/闭麦播报：{mute_label}",
            f"定制用户：{len(self._user_templates)} 人",
            f"音量：{self._volume} / 冷却：{self._cooldown} 秒",
            f"入房间隔：{self._start_delay_ms} 毫秒 / 尾部静音：{self._leave_delay} 秒",
            f"FFmpeg：{'可用' if ffmpeg_ok else '不可用'}"
            + ("" if ffmpeg_ok else f"（{ffmpeg_detail}）"),
            f"Bot Token：{token_ready} / KOOK 适配器：{adapters} 个",
            f"运行状态：{state}",
            f"网关事件：本次启动收到 {self._gateway_events} 条"
            "（语音进出有反应但这里为 0 = 事件没进插件）",
            f"诊断日志：{self._log_file}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("语音欢迎帮助")
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    async def cmd_help(self, event: AstrMessageEvent):
        """查看 KOOK TTS 语音欢迎插件指令列表"""
        blocked = await self._scope_gate(event)
        if blocked:
            yield event.plain_result(blocked)
            return
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
            "· /语音欢迎定制音频 <用户ID> enter|exit <路径或链接> —— 指定用户播放定制音频",
            "· /语音欢迎定制删除 <用户ID> —— 删除该用户的定制文案与音频",
            "· /语音欢迎文字频道 当前|<频道ID>|自动|关闭 —— 设置【本服务器】文字播报频道",
            "· /语音欢迎文字频道 列表 —— 查看各服务器的文字频道绑定",
            "· /语音欢迎目标频道 当前|<频道ID>|自动|关闭|列表 —— 设置【本服务器】目标语音频道",
            "· /语音欢迎静音播报 开 [秒]|关 —— 静音 / 闭麦状态播报",
            "· /语音欢迎试听 [文案] —— 合成一段语音并在频道里试听",
            "· /语音欢迎重载 —— 重新读取配置与定制文案",
            "",
            "模板占位符：{nickname} 服务器昵称 / {username} 用户名 / {user_id} / "
            "{channel} 频道名 / {guild} 服务器名 / {mute_type} 静音类型",
            f"默认进入模板：{DEFAULT_ENTER_TEMPLATE}",
            f"默认退出模板：{DEFAULT_EXIT_TEMPLATE}",
            "",
            "多人同时进房策略（插件设置里切换）：",
            "· merge —— 窗口内合并成一条「欢迎 A、B 进入 …」",
            "· interrupt —— 后进的打断正在播的",
            "· ignore —— 只播第一个，后进的忽略",
            "",
            "文字播报频道怎么定：每服务器手动绑定 > 兜底配置 > 自动识别",
            "服务器默认文字频道；三级都没有就不发文字，不会发错地方。",
            "",
            "静音 / 闭麦：网关事件 add/delete_guild_mute 实时推送（服务器级），",
            "轮询 /guild-mute/list 兜底。只能识别「被管理员服务器静音 / 闭麦」，",
            "用户在客户端自己点的静音官方不暴露。默认关闭，需 /语音欢迎静音播报 开。",
        ]
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ #
    # 指令：管理员
    # ------------------------------------------------------------------ #

    @filter.command("语音欢迎文字频道")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    async def cmd_notify_channel(self, event: AstrMessageEvent):
        """设置**本服务器**的文字播报频道（多服务器各管各的）"""
        blocked = await self._scope_gate(event)
        if blocked:
            yield event.plain_result(blocked)
            return
        parts = self._strip_command(event.message_str, "语音欢迎文字频道")
        arg = parts[0].strip() if parts else "当前"

        if arg in ("列表", "list"):
            yield event.plain_result(
                "文字播报频道绑定：\n" + "\n".join(self._notify_map_lines())
            )
            return

        token = self._resolve_token()
        ctx = await self._channel_context(event)
        guild_id = ctx["guild_id"]

        # ---- 全局兜底频道（所有服务器共用，默认留空） ---------------- #
        if arg in ("全局", "global"):
            sub = parts[1].strip() if len(parts) > 1 else ""
            if not sub:
                yield event.plain_result(
                    f"当前兜底文字频道：{self._notify_text_channel_id or '未设置'}"
                    "（为空时按服务器自动识别）\n"
                    "用法：/语音欢迎文字频道 全局 <频道ID>｜全局 关闭"
                )
                return
            if sub in ("关闭", "关", "off", "none"):
                self._conf_set("strategy.notify_text_channel_id", "")
                self._clear_text_channel_cache()
                saved = self._save_config("关闭兜底文字频道")
                yield event.plain_result(
                    f"{'已关闭' if saved else '保存失败'}兜底文字频道（改回自动识别）"
                )
                return
            if not sub.isdigit():
                yield event.plain_result("频道 ID 必须是纯数字")
                return
            self._conf_set("strategy.notify_text_channel_id", sub)
            self._clear_text_channel_cache()
            saved = self._save_config("设置兜底文字频道")
            ok, detail = (False, "Bot Token 未就绪")
            if token:
                ok, detail = await self._send_test_notice(token, sub)
            yield event.plain_result(
                f"{'已设置' if saved else '保存失败'}兜底文字频道：{sub}"
                "（所有服务器共用，优先级高于自动识别）\n"
                + ("测试消息已发送 ✅" if ok else f"⚠️ 测试消息发送失败：{detail}")
            )
            return

        if not guild_id:
            yield event.plain_result(
                "无法识别当前服务器 ID，请到目标服务器的文字频道里再试，"
                "或直接用频道 ID 绑定：/语音欢迎文字频道 <频道ID>"
            )
            return

        # ---- 关闭本服务器 ------------------------------------------- #
        if arg in ("关闭", "关", "off", "disable", "none"):
            saved = self._bind_notify_channel(guild_id, "off")
            yield event.plain_result(
                f"{'已关闭' if saved else '保存失败'}本服务器（{guild_id}）的文字播报"
            )
            return

        # ---- 恢复自动识别 ------------------------------------------- #
        if arg in ("自动", "auto", "默认", "default"):
            saved = self._bind_notify_channel(guild_id, "")
            channel_id, source = ("", "")
            if token:
                channel_id, source = await self._resolve_text_channel(token, guild_id)
            yield event.plain_result(
                f"{'已切换为自动识别' if saved else '保存失败'}\n"
                f"本服务器（{guild_id}）当前解析结果："
                + (f"{channel_id}（{source}）" if channel_id else "没有可用文字频道")
            )
            return

        # ---- 绑定到当前 / 指定频道 ---------------------------------- #
        if arg in ("当前", "here", "this"):
            channel_id = ctx["channel_id"]
            if not channel_id:
                yield event.plain_result(
                    "无法识别当前文字频道 ID，请直接指定：/语音欢迎文字频道 <频道ID>"
                )
                return
        elif arg.isdigit():
            channel_id = arg
        else:
            yield event.plain_result(
                "用法：/语音欢迎文字频道 当前｜<频道ID>｜自动｜关闭｜列表\n"
                "· 当前 = 绑定你发这条指令所在的文字频道（只对本服务器生效）\n"
                "· 自动 = 删除绑定，改为自动识别服务器默认文字频道\n"
                "· 关闭 = 本服务器不同步文字\n"
                "· 全局 <频道ID> = 设置所有服务器共用的兜底频道"
            )
            return

        saved = self._bind_notify_channel(guild_id, channel_id)
        if not saved:
            yield event.plain_result("保存绑定失败，请查看日志")
            return

        ok, detail = (False, "Bot Token 未就绪")
        if token:
            ok, detail = await self._send_test_notice(token, channel_id)
        yield event.plain_result(
            f"已绑定：服务器 {guild_id} → 文字频道 {channel_id}"
            f"（{ctx['name'] or '名称未知'}）\n"
            + ("测试消息已发送 ✅" if ok else f"⚠️ 测试消息发送失败：{detail}")
        )

    @filter.command("语音欢迎目标频道")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    async def cmd_target_channel(self, event: AstrMessageEvent):
        """目标语音频道绑定：/语音欢迎目标频道 当前|<频道ID>|自动|关闭|列表"""
        blocked = await self._scope_gate(event)
        if blocked:
            yield event.plain_result(blocked)
            return
        parts = self._strip_command(event.message_str, "语音欢迎目标频道")
        arg = parts[0].strip().lower() if parts else "列表"
        ctx = await self._channel_context(event)
        guild_id = ctx["guild_id"]
        if not guild_id:
            yield event.plain_result(
                "无法识别当前服务器 ID，请到目标服务器的文字频道里再试"
            )
            return

        if arg in ("列表", "list"):
            current = self._target_channel_for(guild_id)
            bound = self._target_channels.get(guild_id, "")
            source = f"手动绑定 {bound}" if bound else (
                f"旧版全局配置 {self._conf_str('target_channel_id')}"
                if self._conf_str("target_channel_id")
                else "自动跟随事件频道"
            )
            yield event.plain_result(
                f"目标语音频道：{current or '自动跟随事件频道'}（{source}）\n"
                "用法：/语音欢迎目标频道 当前｜<频道ID>｜自动｜关闭"
            )
            return

        if arg in ("自动", "auto", "关闭", "关", "off", "none"):
            saved = self._bind_target_channel(guild_id, "")
            yield event.plain_result(
                f"{'已解绑' if saved else '保存失败'}：本服务器改为自动跟随事件中的语音频道"
            )
            return

        if arg in ("当前", "here", "this"):
            channel_id = self._user_voice_channel(guild_id, str(
                getattr(event, "get_sender_id", lambda: "")() or ""
            ))
            if not channel_id:
                yield event.plain_result(
                    "找不到你当前所在的语音频道，请直接指定：/语音欢迎目标频道 <语音频道ID>"
                )
                return
        elif arg.isdigit():
            channel_id = arg
        else:
            yield event.plain_result(
                "用法：/语音欢迎目标频道 当前｜<频道ID>｜自动｜关闭｜列表\n"
                "· 当前 = 绑定你当前所在的语音频道（只对本服务器生效）\n"
                "· <频道ID> = 绑定指定语音频道，只有它触发播报\n"
                "· 自动 / 关闭 = 删除绑定，恢复跟随事件频道"
            )
            return

        saved = self._bind_target_channel(guild_id, channel_id)
        yield event.plain_result(
            f"{'已绑定' if saved else '保存失败'}：服务器 {guild_id} → 语音频道 {channel_id}"
        )

    async def _channel_context(self, event: AstrMessageEvent) -> dict[str, str]:
        """取指令所在频道的信息：``channel_id`` / ``guild_id`` / ``name``。

        KOOK 的消息事件只带频道 ID，服务器 ID 要再查一次 ``/channel/view``。
        """
        info = {"channel_id": self._current_text_channel_id(event), "guild_id": "", "name": ""}
        token = self._resolve_token()
        if not token or not info["channel_id"]:
            return info
        try:
            channel = await self._api.get_channel(token, info["channel_id"])
        except Exception as exc:
            self._trace(f"查询当前频道信息失败：{exc!r}")
            return info
        info["guild_id"] = to_text(channel.get("guild_id"))
        info["name"] = to_text(channel.get("name"))
        return info

    async def _scope_gate(
        self, event: AstrMessageEvent, ctx: dict[str, str] | None = None
    ) -> str:
        """服务器范围把关。

        返回非空字符串 = 本服务器不在「目标服务器 ID」范围内，调用方直接回复该文本
        并 return。非启用服务器里插件应当像没装一样：不回应业务内容，只回一句
        「未启用 + 本服务器 ID」，顺带把 ID 告诉用户——查 ID 是最容易搞错的一步
        （/sid 里的 Session ID 是文字频道 ID，不是服务器 ID）。

        取不到 guild_id（token 缺失 / 接口失败）时放行，避免配错 Token 把自己锁死。
        """
        ctx = ctx or await self._channel_context(event)
        guild_id = ctx["guild_id"]
        if not guild_id or self._guild_enabled(guild_id):
            return ""
        scope = "、".join(sorted(self._enabled_guild_ids)) or "（空）"
        return (
            f"本服务器（{guild_id}）未在「目标服务器 ID」范围内，语音欢迎不生效。\n"
            f"当前范围：{scope}\n"
            "启用方法：把这个服务器 ID 加进配置（多个用 ; 分割），"
            "或清空该配置 = 全部服务器生效。"
        )

    @staticmethod
    def _current_text_channel_id(event: AstrMessageEvent) -> str:
        """从指令事件里取当前文字频道 ID（KOOK 的 group_id / session_id 即频道 ID）。"""
        for getter_name in ("get_group_id", "get_channel_id"):
            getter = getattr(event, getter_name, None)
            if callable(getter):
                try:
                    value = str(getter() or "").strip()
                except Exception:
                    value = ""
                if value and value not in ("unknown", "0"):
                    return value
        message_obj = getattr(event, "message_obj", None)
        for attr in ("group_id", "session_id", "channel_id"):
            value = str(getattr(message_obj, attr, "") or "").strip()
            if value and value not in ("unknown", "0"):
                return value
        return ""

    @filter.command("语音欢迎模板")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    async def cmd_template(self, event: AstrMessageEvent):
        """设置状态播报模板：/语音欢迎模板 enter|exit <文案>"""
        blocked = await self._scope_gate(event)
        if blocked:
            yield event.plain_result(blocked)
            return
        parts = self._strip_command(event.message_str, "语音欢迎模板", maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result(
                "用法：/语音欢迎模板 <状态> <文案>\n"
                "状态：" + " / ".join(sorted(STATES_SUPPORTED)) + "\n"
                "占位符：{nickname} {username} {user_id} {channel} {guild} {mute_type}"
            )
            return

        state = parts[0].lower()
        text = parts[1].strip()
        if state not in STATES_SUPPORTED:
            yield event.plain_result(
                "状态只支持：" + " / ".join(sorted(STATES_SUPPORTED))
            )
            return
        if not text:
            yield event.plain_result("文案不能为空；填「清空」可关闭该状态播报")
            return

        if text in ("清空", "clear", "none"):
            text = ""
        self._conf_set(STATE_VOICE_TEMPLATE_PATH[state], text)
        saved = self._save_config(f"设置 {state} 模板")
        label = {
            STATE_ENTER: "进入",
            STATE_EXIT: "退出",
            **STATE_LABELS,
        }.get(state, state)
        shown = text or "（已关闭）"
        yield event.plain_result(
            f"{'已设置' if saved else '设置失败（配置写入出错）'}：{label}文案 = {shown}"
        )

    @filter.command("语音欢迎定制")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    async def cmd_user_template(self, event: AstrMessageEvent):
        """指定用户定制文案：/语音欢迎定制 <用户ID> enter|exit <文案>"""
        blocked = await self._scope_gate(event)
        if blocked:
            yield event.plain_result(blocked)
            return
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
        blocked = await self._scope_gate(event)
        if blocked:
            yield event.plain_result(blocked)
            return
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
            f"{'已删除' if saved else '删除失败（写入出错）'}：用户 {user_id} 的定制文案与音频"
        )

    @filter.command("语音欢迎定制音频")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    async def cmd_user_audio(self, event: AstrMessageEvent):
        """指定用户的定制音频：/语音欢迎定制音频 <用户ID> enter|exit <路径或链接>"""
        blocked = await self._scope_gate(event)
        if blocked:
            yield event.plain_result(blocked)
            return
        parts = self._strip_command(event.message_str, "语音欢迎定制音频", maxsplit=2)
        if len(parts) < 3:
            yield event.plain_result(
                "用法：/语音欢迎定制音频 <用户ID> enter|exit <音频路径或 http 链接>\n"
                "例：/语音欢迎定制音频 123456 enter https://example.com/vip.mp3\n"
                "该用户进出语音频道时直接播放这段音频，不再走 TTS。"
            )
            return

        user_id, state, source = parts[0].strip(), parts[1].lower(), parts[2].strip()
        if not user_id.isdigit():
            yield event.plain_result("用户 ID 必须是纯数字（KOOK 用户 ID）")
            return
        if state not in STATES_SUPPORTED:
            yield event.plain_result(
                "状态只支持：" + " / ".join(sorted(STATES_SUPPORTED))
            )
            return
        if not source:
            yield event.plain_result("音频路径或链接不能为空")
            return
        if not source.lower().startswith(("http://", "https://")):
            # 本地路径：立刻校验，避免存进配置后才发现写错
            if not Path(source).expanduser().is_file():
                yield event.plain_result(f"本地音频文件不存在：{source}")
                return

        entry = self._user_templates.get(user_id)
        if not isinstance(entry, dict):
            entry = {}
            self._user_templates[user_id] = entry
        entry[f"{state}_audio"] = source
        saved = self._save_user_templates()
        yield event.plain_result(
            f"{'已设置' if saved else '保存失败'}：用户 {user_id} 的"
            f"{STATE_LABELS.get(state, state)}定制音频 = {source}"
        )

    @filter.command("语音欢迎定制音频删除")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    async def cmd_user_audio_delete(self, event: AstrMessageEvent):
        """删除指定用户的定制音频：/语音欢迎定制音频删除 <用户ID> [状态]"""
        blocked = await self._scope_gate(event)
        if blocked:
            yield event.plain_result(blocked)
            return
        parts = self._strip_command(event.message_str, "语音欢迎定制音频删除")
        if not parts or not parts[0].isdigit():
            yield event.plain_result(
                "用法：/语音欢迎定制音频删除 <用户ID> [enter|exit|全部]"
            )
            return

        user_id = parts[0]
        state = parts[1].lower() if len(parts) > 1 else "全部"
        entry = self._user_templates.get(user_id)
        if not isinstance(entry, dict):
            yield event.plain_result(f"用户 {user_id} 没有定制内容")
            return

        states = list(STATES_SUPPORTED) if state == "全部" else [state]
        removed = [s for s in states if entry.pop(f"{s}_audio", None) is not None]
        if not removed:
            yield event.plain_result(f"用户 {user_id} 没有这些状态的定制音频")
            return
        if not entry:
            del self._user_templates[user_id]
        saved = self._save_user_templates()
        yield event.plain_result(
            f"{'已删除' if saved else '删除失败（写入出错）'}："
            f"用户 {user_id} 的定制音频（{'、'.join(removed)}）"
        )

    @filter.command("语音欢迎静音播报")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    async def cmd_mute_watch(self, event: AstrMessageEvent):
        """静音 / 闭麦播报开关：/语音欢迎静音播报 开 [秒]|关|状态"""
        blocked = await self._scope_gate(event)
        if blocked:
            yield event.plain_result(blocked)
            return
        parts = self._strip_command(event.message_str, "语音欢迎静音播报")
        arg = parts[0].strip().lower() if parts else "状态"
        current = self._mute_poll_interval

        if arg in ("状态", "status"):
            yield event.plain_result(self._mute_status_text(current))
            return

        if arg in ("关", "关闭", "off", "disable"):
            self._conf_set("mute_mic.mic.enabled", False)
            self._conf_set("mute_mic.mute.enabled", False)
            saved = self._save_config("关闭静音播报")
            yield event.plain_result(
                f"{'已关闭' if saved else '保存失败'}静音 / 闭麦播报"
            )
            return

        if arg in ("开", "on", "enable"):
            interval = DEFAULT_MUTE_POLL_SECONDS
            if len(parts) > 1 and parts[1].strip().isdigit():
                interval = max(MUTE_POLL_MIN_SECONDS, int(parts[1].strip()))
            self._conf_set("mute_mic.mic.enabled", True)
            self._conf_set("mute_mic.mute.enabled", True)
            self._conf_set("strategy.mute_poll_interval", interval)
            saved = self._save_config("开启静音播报")
            missing = [
                state
                for state in (STATE_MUTE, STATE_UNMUTE, STATE_MIC_OFF, STATE_MIC_ON)
                if not self._state_template(state)
            ]
            hint = (
                "\n⚠️ 这些状态还没有文案，不会播报：" + "、".join(missing)
                + "\n用 /语音欢迎模板 mic_off <文案> 这类指令补上。"
                if missing
                else ""
            )
            yield event.plain_result(
                f"{'已开启' if saved else '保存失败'}：每 {interval} 秒轮询服务器静音名单\n"
                "只能识别「被服务器静音 / 闭麦」，客户端自己点的不行。" + hint
            )
            return

        yield event.plain_result(
            "用法：/语音欢迎静音播报 开 [秒]｜关｜状态\n"
            "开启后插件轮询 /guild-mute/list，识别服务器级静音与闭麦。"
        )

    def _mute_status_text(self, current: int) -> str:
        """静音播报状态文本（指令输出用）。"""
        label = f"每 {current} 秒轮询服务器静音名单（兜底）" if current else "已关闭"
        templates = "、".join(
            f"{STATE_LABELS[state]}："
            f"{'开' if self._state_announce_enabled(state) else '关'}"
            for state in (STATE_MUTE, STATE_UNMUTE, STATE_MIC_OFF, STATE_MIC_ON)
        )
        watched = len(self._guild_activity)
        return (
            f"静音 / 闭麦播报：{label}\n"
            f"状态文案：{templates}\n"
            f"正在跟踪的服务器：{watched} 个\n"
            "说明：只能识别服务器级静音 / 闭麦（/guild-mute/list），"
            "自己在客户端点的静音官方不暴露。"
        )

    @filter.command("语音欢迎试听")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.KOOK)
    async def cmd_preview(self, event: AstrMessageEvent):
        """试听：/语音欢迎试听 [文案] —— 合成并推流到事件所在语音频道"""
        blocked = await self._scope_gate(event)
        if blocked:
            yield event.plain_result(blocked)
            return
        text = self._strip_command(event.message_str, "语音欢迎试听", maxsplit=1)
        speak = text[0].strip() if text else ""
        if not speak:
            names = {"nickname": "试听用户", "username": "preview"}
            speak = self._build_speak_text(STATE_ENTER, "0", names) or "这是一条试听语音"

        user_id = getattr(event, "get_sender_id", lambda: "")()
        guild_id = ""
        try:
            guild_id = str(getattr(event, "get_group_id", lambda: "")() or "")
        except Exception:
            guild_id = ""
        channel_id = self._target_channel_for(guild_id)

        request = _PlayRequest(
            events=[
                VoiceStateEvent(
                    state=STATE_ENTER,
                    user_id=str(user_id or "0"),
                    channel_id=channel_id,
                    guild_id=guild_id,
                )
            ],
            reason="preview",
            override_text=speak,
        )
        if not channel_id:
            # 事件没有带出语音频道时，让播报流程用配置里的目标频道；
            # 两者都空则播报会失败并写诊断日志。
            self._trace(
                f"试听请求：channel 未知（绑定={self._target_channel_for(guild_id) or '空'}）"
            )

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
        blocked = await self._scope_gate(event)
        if blocked:
            yield event.plain_result(blocked)
            return
        self._user_templates = self._load_user_templates()
        self._notify_channels = self._load_notify_channels()
        self._ffmpeg_cache = None
        self._nickname_cache.clear()
        self._channel_cache.clear()
        self._guild_info_cache.clear()
        self._text_channel_cache.clear()
        self._mute_state.clear()
        ok, detail = await self._check_ffmpeg()
        self._attach_all()
        yield event.plain_result(
            f"已重载：定制用户 {len(self._user_templates)} 人；"
            f"文字频道绑定 {len(self._notify_channels)} 个；"
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
