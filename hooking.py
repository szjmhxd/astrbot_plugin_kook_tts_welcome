"""KOOK 适配器事件挂接（Mixin）。

AstrBot 自带的 KOOK 适配器只把聊天消息转成 AstrBot 事件，网关系统消息
（``type == 255``）大多被丢弃。本模块包装 ``KookClient.event_callback``，
额外拿到语音频道的加入 / 退出事件，并预留静音、闭麦等状态扩展点。

挂接可逆：插件卸载或重载时还原原始回调，热重载不会重复播放。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from .constants import (
    KOOK_EVENT_TO_STATE,
    KOOK_SYSTEM_MESSAGE_TYPE,
    STATE_ENTER,
    STATE_EXIT,
)
from .utils import to_int, to_mapping, to_text

# 用于识别「本插件挂接的回调」，热重载时先解开旧包装，避免重复播放
_WRAPPER_FLAG = "__kook_tts_welcome_wrapper__"
_WRAPPER_ORIGINAL = "__kook_tts_welcome_original__"


@dataclass(slots=True)
class VoiceStateEvent:
    """一条语音频道状态事件。"""

    state: str  # STATE_ENTER / STATE_EXIT / …
    user_id: str
    channel_id: str
    guild_id: str


def extract_voice_state_event(data: Any) -> VoiceStateEvent | None:
    """从 KOOK 网关事件中解析语音频道状态变化。

    只识别 ``extra.body`` 中带 ``user_id`` 的系统消息；``joined_channel`` 必须
    带 ``channel_id``（否则无法决定加入哪个频道）。

    扩展点：KOOK 将来若推送静音 / 闭麦事件，只需在
    :data:`KOOK_EVENT_TO_STATE` 里补映射即可，无需改动本函数。
    """
    payload = to_mapping(data)
    if not payload:
        return None
    if to_int(payload.get("type"), -1) != KOOK_SYSTEM_MESSAGE_TYPE:
        return None

    extra = to_mapping(payload.get("extra"))
    event_type = to_text(extra.get("type"))
    state = KOOK_EVENT_TO_STATE.get(event_type)
    if state is None:
        return None

    body = to_mapping(extra.get("body"))
    user_id = to_text(body.get("user_id"))
    channel_id = to_text(body.get("channel_id"))
    guild_id = to_text(payload.get("target_id"))
    if not user_id:
        return None
    if state == STATE_ENTER and not channel_id:
        return None
    return VoiceStateEvent(
        state=state,
        user_id=user_id,
        channel_id=channel_id,
        guild_id=guild_id,
    )


def is_exit_event(data: Any) -> bool:
    """快速判断是不是「退出语音频道」事件（用于机器人自身退出的止损）。"""
    event = extract_voice_state_event(data)
    return event is not None and event.state == STATE_EXIT


def describe_unmatched_event(data: Any) -> str | None:
    """系统消息是进出事件、但字段残缺导致提取失败时，给出诊断描述。

    普通消息、无关系统事件返回 ``None``（不刷屏）。这个入口解决一个观测盲区：
    提取失败和「事件根本没到」在诊断日志里都表现为无输出，下次排障分不清。
    """
    payload = to_mapping(data)
    if not payload:
        return None
    if to_int(payload.get("type"), -1) != KOOK_SYSTEM_MESSAGE_TYPE:
        return None

    extra = to_mapping(payload.get("extra"))
    event_type = to_text(extra.get("type"))
    if event_type not in KOOK_EVENT_TO_STATE:
        return None

    body = to_mapping(extra.get("body"))
    return (
        f"未能提取语音事件：type={event_type} "
        f"user={to_text(body.get('user_id')) or '缺失'} "
        f"channel={to_text(body.get('channel_id')) or '缺失'} "
        f"guild={to_text(payload.get('target_id')) or '缺失'}"
    )


@dataclass(slots=True)
class _Hook:
    """一条适配器回调挂接记录。"""

    client: Any
    original: Any
    wrapper: Any


class HookingMixin:
    """KOOK 适配器回调的挂接 / 还原 / 事件分发。方法由 main.py 的插件类继承。"""

    # ------------------------------------------------------------------ #
    # 适配器发现
    # ------------------------------------------------------------------ #

    def _iter_kook_adapters(self) -> list[Any]:
        """找出当前已加载的所有 KOOK 平台适配器实例。"""
        try:
            instances = self.context.platform_manager.get_insts()
        except Exception:
            return []
        adapters: list[Any] = []
        for instance in instances:
            try:
                if instance.meta().name == "kook":
                    adapters.append(instance)
            except Exception:
                continue
        return adapters

    # ------------------------------------------------------------------ #
    # 挂接与还原
    # ------------------------------------------------------------------ #

    async def _attach_loop(self) -> None:
        """兜底轮询：适配器可能比插件晚加载，或运行中被重载。"""
        while True:
            try:
                self._attach_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[KOOK TTS欢迎] 挂接 KOOK 适配器失败")
            await asyncio.sleep(5)

    def _attach_all(self) -> None:
        """把本插件的事件包装挂到所有 KOOK 适配器上。"""
        for adapter in self._iter_kook_adapters():
            self._attach_adapter(adapter)

    def _attach_adapter(self, adapter: Any) -> None:
        """包装单个适配器的 ``event_callback``（重复挂接前先检查 / 还原）。"""
        try:
            platform_id = adapter.meta().id or "kook"
        except Exception:
            platform_id = "kook"

        client = getattr(adapter, "client", None)
        if client is None:
            logger.debug("[KOOK TTS欢迎] KOOK 适配器尚未创建客户端，稍后重试")
            return

        hook = self._hooks.get(platform_id)
        if hook is not None:
            same_client = hook.client is client
            still_ours = getattr(client, "event_callback", None) is hook.wrapper
            if same_client and still_ours:
                return
            if not same_client:
                self._trace(f"适配器 {platform_id} 已更换客户端，先还原旧回调")
            self._detach(platform_id)

        callback = getattr(client, "event_callback", None)
        if callback is None:
            logger.warning(
                "[KOOK TTS欢迎] 当前 KOOK 适配器没有 event_callback，"
                "无法接收语音频道事件，请升级 AstrBot"
            )
            self._trace(f"挂接失败：适配器 {platform_id} 没有 event_callback")
            return

        # 剥开层层包装，找到最底层的原始回调
        original = callback
        guard = 0
        while getattr(original, _WRAPPER_FLAG, False) and guard < 5:
            original = getattr(original, _WRAPPER_ORIGINAL, original)
            guard += 1

        async def wrapper(
            data: Any, _original: Any = original, _adapter: Any = adapter
        ) -> Any:
            """包装回调：先处理语音事件，再把原始事件交还适配器。"""
            try:
                await self._on_kook_event(_adapter, data)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[KOOK TTS欢迎] 处理语音频道事件时出现异常")
                self._trace("处理语音频道事件时出现异常，详见 AstrBot 日志")
            return await _original(data)

        setattr(wrapper, _WRAPPER_FLAG, True)
        setattr(wrapper, _WRAPPER_ORIGINAL, original)
        client.event_callback = wrapper
        self._hooks[platform_id] = _Hook(
            client=client, original=original, wrapper=wrapper
        )
        logger.info(f"[KOOK TTS欢迎] 已挂接 KOOK 适配器 {platform_id} 的系统事件")
        self._trace(f"已挂接 KOOK 适配器 {platform_id} 的系统事件")

    def _detach(self, platform_id: str) -> None:
        """还原指定适配器的回调；回调已被第三方改写时保持原样。"""
        hook = self._hooks.pop(platform_id, None)
        if hook is None:
            return
        if getattr(hook.client, "event_callback", None) is hook.wrapper:
            hook.client.event_callback = hook.original
            logger.info(f"[KOOK TTS欢迎] 已还原 KOOK 适配器 {platform_id} 的回调")
            self._trace(f"已还原 KOOK 适配器 {platform_id} 的回调")
