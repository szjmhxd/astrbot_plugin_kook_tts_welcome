"""文字频道同步播报（Mixin）。

把一个成员的语音动作（进 / 出 / 静音 / 闭麦）同步成一条文字消息。难点不在
发送，而在**找到该发到哪个频道**——用户往往有多个服务器，一个全局频道 ID
根本不适用。解析优先级：

1. **每服务器手动绑定**：``/语音欢迎文字频道 当前`` 写入
   ``notify_channels.json``（``{guild_id: channel_id}``）；
2. **全局兜底配置**：插件设置里的「兜底文字频道 ID」（所有服务器共用）；
3. **自动识别**：``GET /guild/view`` 的 ``default_channel_id``（服务器默认
   频道），校验是文字频道才用；不是则退到频道列表里第一个非分类文字频道。

三级都拿不到 → 该服务器**不发文字**（只写诊断日志），不会把消息发错地方。
解析结果按服务器缓存，命中失败的结果用更短的 TTL，方便用户新建频道后自动生效。
"""

from __future__ import annotations

import asyncio
import json
import time

from astrbot.api import logger

from .constants import (
    GUILD_INFO_CACHE_TTL,
    NOTIFY_CHANNELS_FILE_NAME,
    NOTIFY_DISABLED,
    PLUGIN_NAME,
    STATE_ENTER,
    STATE_EXIT,
    TEXT_CHANNEL_NEGATIVE_TTL,
)
from .hooking import VoiceStateEvent
from .kook_api import KookApiError
from .utils import to_bool, to_int, to_text

# KOOK 频道类型：1 = 文字频道，2 = 语音频道
CHANNEL_TYPE_TEXT = 1


def is_text_channel(channel: dict) -> bool:
    """判断频道对象是不是可发消息的文字频道（排除分类分组）。"""
    if to_bool(channel.get("is_category")):
        return False
    return to_int(channel.get("type")) == CHANNEL_TYPE_TEXT


class NotifyMixin:
    """服务器文字频道的解析、绑定与发送。方法由 main.py 的插件类继承。"""

    # ------------------------------------------------------------------ #
    # 每服务器绑定表
    # ------------------------------------------------------------------ #

    def _load_notify_channels(self) -> dict[str, str]:
        """读取 ``notify_channels.json``（服务器 → 文字频道）。"""
        path = self._data_dir / NOTIFY_CHANNELS_FILE_NAME
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(f"[KOOK TTS欢迎] 读取文字频道绑定失败：{exc}")
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            to_text(key): to_text(value)
            for key, value in data.items()
            if to_text(key) and to_text(value)
        }

    def _save_notify_channels(self) -> bool:
        """落盘 ``notify_channels.json``。"""
        path = self._data_dir / NOTIFY_CHANNELS_FILE_NAME
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self._notify_channels, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return True
        except OSError as exc:
            logger.warning(f"[KOOK TTS欢迎] 保存文字频道绑定失败：{exc}")
            self._trace(f"保存文字频道绑定失败：{exc!r}")
            return False

    def _bind_notify_channel(self, guild_id: str, channel_id: str) -> bool:
        """绑定（或解绑，channel_id 为空）某服务器的文字播报频道。"""
        self._clear_text_channel_cache(guild_id)
        if channel_id:
            self._notify_channels[guild_id] = channel_id
        else:
            self._notify_channels.pop(guild_id, None)
        return self._save_notify_channels()

    def _clear_text_channel_cache(self, guild_id: str = "") -> None:
        """清掉文字频道的解析缓存（改绑定后必须调用）。"""
        cache = getattr(self, "_text_channel_cache", None)
        if cache is None:
            return
        if guild_id:
            cache.pop(guild_id, None)
        else:
            cache.clear()

    # ------------------------------------------------------------------ #
    # 服务器信息
    # ------------------------------------------------------------------ #

    async def _resolve_guild_info(self, token: str, guild_id: str) -> dict:
        """查询服务器详情（名称 / 默认频道 / 频道列表），带 TTL 缓存。"""
        if not guild_id:
            return {}
        cache = getattr(self, "_guild_info_cache", {})
        cached = cache.get(guild_id)
        now = time.monotonic()
        if cached is not None and now - cached[0] < GUILD_INFO_CACHE_TTL:
            return cached[1]

        info: dict = {}
        try:
            guild = await self._api.get_guild(token, guild_id)
            channels = guild.get("channels")
            info = {
                "name": to_text(guild.get("name")),
                "default_channel_id": to_text(guild.get("default_channel_id")),
                "welcome_channel_id": to_text(guild.get("welcome_channel_id")),
                "channels": [c for c in channels if isinstance(c, dict)]
                if isinstance(channels, list)
                else [],
            }
            if not info["channels"]:
                info["channels"] = await self._api.get_channels(token, guild_id)
        except KookApiError as exc:
            logger.debug(f"[KOOK TTS欢迎] 查询服务器 {guild_id} 信息失败：{exc}")
            self._trace(f"查询服务器信息失败：guild={guild_id} {exc}")
            return {}

        cache[guild_id] = (time.monotonic(), info)
        while len(cache) > 256:
            cache.pop(next(iter(cache)))
        return info

    async def _resolve_guild_name(self, token: str, guild_id: str) -> str:
        """服务器名称（用于 ``{guild}`` 占位符），失败返回空串。"""
        if not guild_id:
            return ""
        info = await self._resolve_guild_info(token, guild_id)
        return to_text(info.get("name"))

    @staticmethod
    def _pick_text_channel(info: dict) -> str:
        """从服务器信息里挑一个能发消息的文字频道。

        先看 ``default_channel_id``（服务器默认频道），它必须是文字频道；
        否则按频道列表顺序取第一个非分类文字频道。都找不到返回空串。
        """
        channels = info.get("channels") or []
        default_id = to_text(info.get("default_channel_id"))
        if default_id:
            for channel in channels:
                if to_text(channel.get("id")) == default_id and is_text_channel(channel):
                    return default_id
        for channel in channels:
            if is_text_channel(channel):
                channel_id = to_text(channel.get("id"))
                if channel_id:
                    return channel_id
        return ""

    # ------------------------------------------------------------------ #
    # 目标频道解析
    # ------------------------------------------------------------------ #

    async def _resolve_text_channel(
        self, token: str, guild_id: str
    ) -> tuple[str, str]:
        """解析某服务器的文字播报频道，返回 ``(channel_id, 来源说明)``。

        三级兜底：每服务器绑定 → 全局配置 → 自动识别服务器默认文字频道。
        全部落空时返回 ``("", "")``，调用方应静默跳过。
        """
        if guild_id:
            bound = self._notify_channels.get(guild_id, "")
            if bound == NOTIFY_DISABLED:
                return "", "本服务器已关闭文字播报"
            if bound:
                return bound, "手动绑定"

        global_id = self._notify_text_channel_id
        if global_id:
            return global_id, "全局配置"

        if not self._notify_auto_channel:
            return "", ""
        if not guild_id:
            return "", ""

        cache = getattr(self, "_text_channel_cache", {})
        cached = cache.get(guild_id)
        now = time.monotonic()
        if cached is not None:
            age = now - cached[0]
            ttl = GUILD_INFO_CACHE_TTL if cached[1] else TEXT_CHANNEL_NEGATIVE_TTL
            if age < ttl:
                return cached[1], cached[2]

        info = await self._resolve_guild_info(token, guild_id)
        channel_id = self._pick_text_channel(info)
        source = "自动识别默认文字频道" if channel_id else ""
        cache[guild_id] = (time.monotonic(), channel_id, source)
        if channel_id:
            self._trace(
                f"服务器 {guild_id} 文字播报频道自动识别为 {channel_id}"
                f"（默认频道={info.get('default_channel_id') or '无'}）"
            )
        else:
            self._trace(
                f"服务器 {guild_id} 没有可用的文字频道，跳过文字播报"
                f"（频道数={len(info.get('channels') or [])}）"
            )
        return channel_id, source

    # ------------------------------------------------------------------ #
    # 发送
    # ------------------------------------------------------------------ #

    def _dispatch_text_notify(self, event: VoiceStateEvent) -> None:
        """把一次语音动作同步到文字频道（每条事件立即发，不参与合并）。

        只处理进入 / 退出：静音、闭麦是轮询出来的**估计状态**，写进文字频道
        噪音大且容易误报，不做同步。
        """
        if event.state == STATE_ENTER:
            template = self._notify_enter_text
        elif event.state == STATE_EXIT:
            template = self._notify_exit_text
        else:
            return
        if not template:
            return
        asyncio.create_task(
            self._send_text_notify(event, template),
            name=f"{PLUGIN_NAME}-notify",
        )

    async def _send_text_notify(self, event: VoiceStateEvent, template: str) -> None:
        """渲染并发送一条文字频道通知。"""
        token = self._resolve_token()
        if not token:
            return
        channel_id, source = await self._resolve_text_channel(token, event.guild_id)
        if not channel_id:
            return
        try:
            names = await self._resolve_display_name(
                token, event.user_id, event.guild_id
            )
            channel_name = await self._resolve_channel_name(token, event.channel_id)
            guild_name = await self._resolve_guild_name(token, event.guild_id)
            text = self._render_text(
                template, event.user_id, names, channel_name, guild_name, event.state
            )
            if not text:
                return
            await self._api.send_message(token, channel_id, text)
            logger.info(f"[KOOK TTS欢迎] 文字播报已发送到 {channel_id}：{text}")
            self._trace(f"文字播报已发送（{source}，channel={channel_id}）：{text}")
        except Exception as exc:
            logger.warning(
                f"[KOOK TTS欢迎] 文字播报发送失败（channel={channel_id}）：{exc}"
            )
            self._trace(f"文字播报失败：channel={channel_id} {exc!r}")

    async def _send_test_notice(self, token: str, channel_id: str) -> tuple[bool, str]:
        """向指定频道发一条测试消息，返回 ``(是否成功, 错误说明)``。"""
        try:
            await self._api.send_message(
                token,
                channel_id,
                "✅ 文字频道同步播报已开启，成员进出语音频道会在这里通知",
            )
            return True, ""
        except Exception as exc:
            return False, str(exc)

    def _notify_map_lines(self) -> list[str]:
        """把每服务器绑定表渲染成指令输出的若干行。"""
        if not self._notify_channels:
            return ["（暂无手动绑定，全部走自动识别）"]
        lines = []
        for guild, channel in sorted(self._notify_channels.items()):
            shown = "已关闭文字播报" if channel == NOTIFY_DISABLED else f"频道 {channel}"
            lines.append(f"· 服务器 {guild} → {shown}")
        return lines
