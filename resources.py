"""Token / FFmpeg 资源解析（Mixin）。

- Bot Token 与机器人 ID：优先插件配置，其次从 KOOK 适配器读取；
- FFmpeg 可用性检测与缓存。
"""

from __future__ import annotations

from astrbot.api import logger

from .utils import run_capture, to_text


class ResourcesMixin:
    """资源解析与检测。方法由 main.py 的插件类继承。"""

    def _resolve_token(self) -> str:
        """Bot Token：优先插件配置，其次从 KOOK 适配器配置读取。"""
        token = self._conf_str("connection.bot_token")
        if token:
            return token
        for adapter in self._iter_kook_adapters():
            config = getattr(adapter, "kook_config", None)
            candidate = getattr(config, "token", "") if config is not None else ""
            if candidate:
                return str(candidate).strip()
        return ""

    def _resolve_bot_id(self) -> str:
        """机器人用户 ID：优先缓存，其次从适配器客户端读取。"""
        if self._bot_id:
            return self._bot_id
        for adapter in self._iter_kook_adapters():
            client = getattr(adapter, "client", None)
            candidate = getattr(client, "bot_id", "") if client is not None else ""
            if candidate:
                self._bot_id = str(candidate).strip()
                return self._bot_id
        return ""

    async def _ensure_bot_id(self, token: str) -> None:
        """缓存里没有 bot_id 时通过 ``/user/me`` 拉取一次。"""
        if self._bot_id:
            return
        try:
            me = await self._api.get_me(token)
        except Exception as exc:
            logger.debug(f"[KOOK TTS欢迎] 获取机器人信息失败：{exc}")
            return
        self._bot_id = to_text(me.get("id"))

    async def _check_ffmpeg(self) -> tuple[bool, str]:
        """检测 FFmpeg 可用性与 libopus 支持，结果缓存。"""
        if self._ffmpeg_cache is not None:
            return self._ffmpeg_cache

        ffmpeg = self._ffmpeg_executable()
        try:
            code, output, timed_out = await run_capture(
                [ffmpeg, "-hide_banner", "-version"], timeout=10
            )
        except (OSError, FileNotFoundError) as exc:
            self._ffmpeg_cache = (False, f"无法执行 {ffmpeg}：{exc}")
            return self._ffmpeg_cache

        if timed_out or code != 0:
            self._ffmpeg_cache = (False, f"{ffmpeg} 执行失败（exit={code}）")
            return self._ffmpeg_cache

        try:
            code, encoders, timed_out = await run_capture(
                [ffmpeg, "-hide_banner", "-encoders"], timeout=10
            )
        except (OSError, FileNotFoundError):
            encoders, timed_out = "", False

        has_opus = (not timed_out and code == 0 and "libopus" in encoders)
        if not has_opus:
            self._ffmpeg_cache = (
                False,
                f"{ffmpeg} 缺少 libopus 编码器，无法推流 Opus",
            )
            return self._ffmpeg_cache

        version = output.splitlines()[0] if output.strip() else ffmpeg
        self._ffmpeg_cache = (True, version)
        return self._ffmpeg_cache
