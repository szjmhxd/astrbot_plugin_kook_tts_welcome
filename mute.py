"""静音 / 闭麦状态监听（Mixin）。

KOOK 网关只推送「进入 / 退出」语音频道事件，**没有任何静音 / 闭麦事件**，
也不提供「用户当前麦克风状态」接口。唯一能拿到的是服务器级的静音名单：

``GET /api/v3/guild-mute/list?guild_id=&return_type=detail``

返回 ``mic``（被服务器**闭麦**）和 ``headset``（被服务器**静音**）两份
user_id 列表。所以这里的做法是**轮询 + 快照比对**：

1. 只在有语音活动的服务器上轮询（``MUTE_POLL_IDLE_SECONDS`` 内有过事件），
   空闲服务器的名单不查，避免无意义请求；
2. 第一次拿到某服务器的名单只建快照、不播报（否则一上线就把所有已静音的
   人念一遍）；
3. 之后名单的增减映射成 ``mic_off`` / ``mic_on`` / ``mute`` / ``unmute``
   四种状态事件，交给和网关事件完全相同的播报管线。

明确的边界：**用户在客户端自己点的静音 / 闭麦官方不暴露**，这里只能识别
「被服务器静音 / 闭麦」。默认关闭（``mute_poll_interval = 0``）。
"""

from __future__ import annotations

import asyncio
import time

from astrbot.api import logger

from .constants import (
    MUTE_POLL_IDLE_SECONDS,
    MUTE_POLL_MIN_SECONDS,
    PLUGIN_NAME,
    STATE_ENTER,
    STATE_EXIT,
    STATE_MIC_OFF,
    STATE_MIC_ON,
    STATE_MUTE,
    STATE_UNMUTE,
    USER_VOICE_CHANNEL_TTL,
)
from .hooking import VoiceStateEvent
from .kook_api import KookApiError

# 每轮最多查询几个服务器，防止服务器特别多时把接口打爆
MAX_GUILDS_PER_POLL = 5

# 名单类型 → (进入该状态, 退出该状态)
MUTE_KIND_STATES = {
    "mic": (STATE_MIC_OFF, STATE_MIC_ON),
    "headset": (STATE_MUTE, STATE_UNMUTE),
}


class MuteWatchMixin:
    """静音 / 闭麦轮询。方法由 main.py 的插件类继承。"""

    # ------------------------------------------------------------------ #
    # 活动记录
    # ------------------------------------------------------------------ #

    def _note_guild_activity(self, guild_id: str) -> None:
        """记录某服务器刚有语音活动（决定是否值得轮询静音名单）。"""
        if not guild_id:
            return
        activity = getattr(self, "_guild_activity", None)
        if activity is not None:
            activity[guild_id] = time.monotonic()

    def _track_user_voice(self, event: VoiceStateEvent) -> None:
        """记录成员当前所在的语音频道，供静音事件定位频道用。"""
        channels = getattr(self, "_user_channels", None)
        if channels is None or not event.user_id:
            return
        key = (event.guild_id, event.user_id)
        if event.state == STATE_ENTER:
            channels[key] = (time.monotonic(), event.channel_id)
            if event.guild_id and event.channel_id:
                self._guild_last_channel[event.guild_id] = (
                    time.monotonic(),
                    event.channel_id,
                )
        elif event.state == STATE_EXIT:
            channels.pop(key, None)

    def _user_voice_channel(self, guild_id: str, user_id: str) -> str:
        """查某个成员当前所在的语音频道。

        优先用进入事件记下的频道；没有记录则退到该服务器最近一次语音活动的
        频道（多人同服时通常就是同一个房间）。都没有返回空串。
        """
        channels = getattr(self, "_user_channels", None) or {}
        record = channels.get((guild_id, user_id))
        if record and time.monotonic() - record[0] < USER_VOICE_CHANNEL_TTL:
            return record[1]
        last = (getattr(self, "_guild_last_channel", None) or {}).get(guild_id)
        if last and time.monotonic() - last[0] < USER_VOICE_CHANNEL_TTL:
            return last[1]
        return ""

    def _has_state_content(self, state: str, user_id: str) -> bool:
        """该状态是否配了任何可播内容（文案或定制音频）。"""
        return bool(
            self._custom_template(user_id, state)
            or self._state_template(state)
            or self._state_text_template(state)
            or self._custom_audio(user_id, state)
        )

    # ------------------------------------------------------------------ #
    # 轮询
    # ------------------------------------------------------------------ #

    async def _mute_loop(self) -> None:
        """按配置间隔轮询静音名单；间隔为 0 时静默待命。"""
        while True:
            try:
                await self._poll_mute_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - 轮询不允许把插件打挂
                logger.debug(f"[KOOK TTS欢迎] 静音轮询异常：{exc}")
                self._trace(f"静音轮询异常：{exc!r}")

            interval = self._mute_poll_interval
            await asyncio.sleep(max(1, interval) if interval > 0 else 5)

    async def _poll_mute_once(self) -> None:
        """对所有「最近有语音活动」的服务器各查一次静音名单。"""
        if self._mute_poll_interval <= 0:
            return
        token = self._resolve_token()
        if not token:
            return

        now = time.monotonic()
        activity = getattr(self, "_guild_activity", {})
        # 清掉长时间没有语音活动的服务器，避免无限增长
        for guild_id in [g for g, ts in activity.items() if now - ts > MUTE_POLL_IDLE_SECONDS]:
            activity.pop(guild_id, None)
            self._mute_state.pop(guild_id, None)

        guilds = list(activity)[:MAX_GUILDS_PER_POLL]
        for guild_id in guilds:
            await self._poll_guild_mute(token, guild_id)

    async def _poll_guild_mute(self, token: str, guild_id: str) -> None:
        """查一个服务器的静音名单，比对快照并派发状态事件。"""
        try:
            data = await self._api.guild_mute_list(token, guild_id)
        except KookApiError as exc:
            logger.debug(f"[KOOK TTS欢迎] 查询服务器 {guild_id} 静音名单失败：{exc}")
            self._trace(f"查询静音名单失败：guild={guild_id} {exc}")
            return

        current = {
            "mic": set(data.get("mic") or []),
            "headset": set(data.get("headset") or []),
        }
        previous = self._mute_state.get(guild_id)
        self._mute_state[guild_id] = current

        if previous is None:
            # 首次只建快照：否则一上线就把所有已静音的人念一遍
            self._trace(
                f"静音名单快照建立：guild={guild_id} "
                f"闭麦={len(current['mic'])} 静音={len(current['headset'])}"
            )
            return

        bot_id = self._resolve_bot_id()
        for kind, (on_state, off_state) in MUTE_KIND_STATES.items():
            for user_id in current[kind] - previous[kind]:
                if user_id == bot_id:
                    continue
                self._emit_polled_state(guild_id, user_id, on_state)
            for user_id in previous[kind] - current[kind]:
                if user_id == bot_id:
                    continue
                self._emit_polled_state(guild_id, user_id, off_state)

    def _emit_polled_state(self, guild_id: str, user_id: str, state: str) -> None:
        """把轮询到的状态变化包装成事件，走与网关事件相同的播报管线。"""
        if not self._has_state_content(state, user_id):
            self._trace(
                f"跳过：{state} user={user_id} 未配置任何播报文案（语音 / 文字都为空）"
            )
            return
        channel_id = self._user_voice_channel(guild_id, user_id)
        if not channel_id:
            self._trace(
                f"忽略轮询状态：user={user_id} state={state} 找不到所在语音频道"
            )
            return

        event = VoiceStateEvent(
            state=state,
            user_id=user_id,
            channel_id=channel_id,
            guild_id=guild_id,
        )
        self._trace(
            f"轮询到状态变化：user={user_id} state={state} channel={channel_id}"
        )
        self._handle_state_event(event, source="poll")
