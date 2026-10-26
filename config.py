"""配置读取、目录解析与诊断日志（Mixin）。"""

from __future__ import annotations

import contextlib
import os
import shutil
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.star import StarTools

from .constants import (
    DEFAULT_LEAVE_DELAY,
    DEFAULT_START_DELAY,
    LOG_MAX_BYTES,
    PLUGIN_NAME,
    PLUGIN_VERSION,
)
from .utils import to_int


class ConfigMixin:
    """配置读取、目录解析与诊断日志。方法由 main.py 的插件类继承。"""

    # ------------------------------------------------------------------ #
    # 配置读取
    # ------------------------------------------------------------------ #

    def _conf_str(self, key: str, default: str = "") -> str:
        """读取字符串配置，None 与两端空白统一处理。"""
        value = self.config.get(key, default)
        if value is None:
            return default
        return str(value).strip()

    def _conf_bool(self, key: str, default: bool = True) -> bool:
        """读取布尔配置，兼容 WebUI 可能保存的字符串形式。"""
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "是", "开"}
        return bool(value)

    def _conf_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        """读取整数配置并夹在 [minimum, maximum] 区间内。"""
        value = to_int(self.config.get(key, default), default)
        return max(minimum, min(maximum, value))

    # ------------------------------------------------------------------ #
    # 常用配置
    # ------------------------------------------------------------------ #

    @property
    def _enabled(self) -> bool:
        return self._conf_bool("enabled", True)

    @property
    def _target_channel(self) -> str:
        return self._conf_str("target_channel_id")

    @property
    def _cooldown(self) -> int:
        return self._conf_int("cooldown", 3, 0, 60)

    @property
    def _volume(self) -> int:
        return self._conf_int("volume", 100, 0, 100)

    @property
    def _start_delay(self) -> int:
        return self._conf_int("start_delay", DEFAULT_START_DELAY, 0, 10)

    @property
    def _leave_delay(self) -> int:
        return self._conf_int("leave_delay", DEFAULT_LEAVE_DELAY, 0, 10)

    @property
    def _play_exit(self) -> bool:
        """是否播放退出语音。"""
        return self._conf_bool("play_exit", False)

    @property
    def _ffmpeg_path(self) -> str:
        return self._conf_str("ffmpeg_path") or "ffmpeg"

    def _ffmpeg_executable(self) -> str:
        """把配置里的 FFmpeg 路径解析成可执行文件（相对路径走 PATH 查找）。"""
        path = self._ffmpeg_path
        if os.path.isabs(path):
            return path
        return shutil.which(path) or path

    # ------------------------------------------------------------------ #
    # 目录
    # ------------------------------------------------------------------ #

    def _resolve_data_dir(self) -> Path:
        """插件数据目录，StarTools 失败时回退到 ``data/plugin_data/<插件名>``。"""
        try:
            return StarTools.get_data_dir(PLUGIN_NAME)
        except Exception as exc:  # pragma: no cover - 仅异常环境触发
            fallback = Path(os.getcwd()) / "data" / "plugin_data" / PLUGIN_NAME
            fallback.mkdir(parents=True, exist_ok=True)
            logger.warning(
                f"[KOOK TTS欢迎] 无法通过 StarTools 获取数据目录（{exc}），"
                f"回退到 {fallback}"
            )
            return fallback

    # ------------------------------------------------------------------ #
    # 诊断日志
    # ------------------------------------------------------------------ #

    def _log_startup(self) -> None:
        """在 AstrBot 日志与诊断日志里输出启动配置摘要。"""
        channel = self._target_channel or "自动跟随事件频道"
        summary = (
            f"已加载 {PLUGIN_VERSION}：启用={self._enabled}，监听频道={channel}，"
            f"退出播报={'开' if self._play_exit else '关'}，"
            f"音量={self._volume}，冷却={self._cooldown}s，"
            f"启动延迟={self._start_delay}s，尾部静音={self._leave_delay}s"
        )
        logger.info(f"[KOOK TTS欢迎] {summary}")
        self._trace(f"插件加载：{summary}")
        self._trace(f"数据目录：{self._data_dir}")
        self._trace(f"诊断日志：{self._log_file}")

    def _trace(self, message: str) -> None:
        """把诊断信息追加到插件日志文件（不含 Bot Token）。"""
        if not self._conf_bool("diagnostic_log", True):
            return
        try:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            if (
                self._log_file.exists()
                and self._log_file.stat().st_size > LOG_MAX_BYTES
            ):
                backup = self._log_file.with_name(f"{self._log_file.name}.1")
                with contextlib.suppress(OSError):
                    if backup.exists():
                        backup.unlink()
                    self._log_file.rename(backup)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with self._log_file.open("a", encoding="utf-8") as handle:
                handle.write(f"[{stamp}] {message}\n")
        except OSError:
            pass

    def _save_config(self, action: str) -> bool:
        """保存配置并统一处理失败。"""
        try:
            self.config.save_config()
        except Exception as exc:
            logger.warning(f"[KOOK TTS欢迎] 保存配置失败（{action}）：{exc}")
            self._trace(f"保存配置失败（{action}）：{exc!r}")
            return False
        return True

    async def _log_ffmpeg_state(self) -> None:
        """启动时检测一次 FFmpeg 并写日志。"""
        ok, detail = await self._check_ffmpeg()
        if ok:
            logger.info(f"[KOOK TTS欢迎] FFmpeg 检查通过：{detail}")
        else:
            logger.error(f"[KOOK TTS欢迎] FFmpeg 不可用：{detail}")
