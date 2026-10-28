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
    DEFAULT_ENTER_TEMPLATE,
    DEFAULT_EXIT_TEMPLATE,
    DEFAULT_LEAVE_DELAY,
    DEFAULT_MERGE_WINDOW_MS,
    DEFAULT_MULTI_USER_POLICY,
    DEFAULT_MUTE_POLL_SECONDS,
    DEFAULT_NOTIFY_ENTER,
    DEFAULT_NOTIFY_EXIT,
    DEFAULT_START_DELAY_MS,
    LEGACY_ENTER_TEMPLATE,
    LEGACY_EXIT_TEMPLATE,
    LEGACY_NOTIFY_ENTER,
    LEGACY_NOTIFY_EXIT,
    LOG_MAX_BYTES,
    MULTI_USER_POLICIES,
    MUTE_POLL_MIN_SECONDS,
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

    def _conf_float(
        self, key: str, default: float, minimum: float, maximum: float
    ) -> float:
        """读取浮点配置并夹在 [minimum, maximum] 区间内。"""
        raw = self.config.get(key, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = float(default)
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
    def _start_delay_ms(self) -> int:
        """入房到开播的最短间隔（毫秒）。"""
        return self._conf_int("start_delay_ms", DEFAULT_START_DELAY_MS, 0, 10000)

    @property
    def _multi_user_policy(self) -> str:
        """多人同时进房时的语音策略：ignore / interrupt / merge。"""
        value = self._conf_str("multi_user_policy", DEFAULT_MULTI_USER_POLICY).lower()
        return value if value in MULTI_USER_POLICIES else DEFAULT_MULTI_USER_POLICY

    @property
    def _merge_window_ms(self) -> int:
        """合并播报的收集窗口（毫秒）。"""
        return self._conf_int("merge_window_ms", DEFAULT_MERGE_WINDOW_MS, 0, 30000)

    @property
    def _notify_text_channel_id(self) -> str:
        """兜底文字频道 ID（空 = 交给自动识别）。"""
        return self._conf_str("notify_text_channel_id")

    @property
    def _notify_auto_channel(self) -> bool:
        """是否自动识别服务器默认文字频道。"""
        return self._conf_bool("notify_auto_channel", True)

    @property
    def _notify_enter_text(self) -> str:
        return self._conf_str("notify_enter_text", DEFAULT_NOTIFY_ENTER)

    @property
    def _notify_exit_text(self) -> str:
        return self._conf_str("notify_exit_text", DEFAULT_NOTIFY_EXIT)

    @property
    def _mute_notify_enabled(self) -> bool:
        """静音 / 闭麦播报总开关（网关事件与轮询共用）。"""
        return self._conf_bool("mute_notify_enabled", False)

    @property
    def _mute_poll_interval(self) -> int:
        """静音 / 闭麦轮询间隔（秒），开关未打开时返回 0（= 不轮询）。"""
        if not self._mute_notify_enabled:
            return 0
        raw = self._conf_int("mute_poll_interval", DEFAULT_MUTE_POLL_SECONDS, 0, 300)
        return max(MUTE_POLL_MIN_SECONDS, raw)

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
        bound = len(getattr(self, "_notify_channels", {}) or {})
        if bound:
            notify_desc = f"每服务器绑定 {bound} 个"
        elif self._notify_text_channel_id:
            notify_desc = f"兜底频道 {self._notify_text_channel_id}"
        elif self._notify_auto_channel:
            notify_desc = "自动识别各服务器默认文字频道"
        else:
            notify_desc = "未开启（语音动作不会同步到文字频道）"

        mute = self._mute_poll_interval
        mute_desc = f"每 {mute}s 轮询服务器静音名单" if mute else "关闭"
        summary = (
            f"已加载 {PLUGIN_VERSION}：启用={self._enabled}，监听频道={channel}，"
            f"退出播报={'开' if self._play_exit else '关'}，"
            f"音量={self._volume}，冷却={self._cooldown}s，"
            f"启动延迟={self._start_delay_ms}ms，尾部静音={self._leave_delay}s，"
            f"文字播报={notify_desc}，静音闭麦播报={mute_desc}"
        )
        logger.info(f"[KOOK TTS欢迎] {summary}")
        self._trace(f"插件加载：{summary}")
        self._trace(f"数据目录：{self._data_dir}")
        self._trace(f"诊断日志：{self._log_file}")

    def _migrate_config(self) -> None:
        """把历史版本写入配置的旧默认文案升级为新默认。

        只在当前值与旧默认**完全一致**时替换；用户自己改过的文案不动。
        """
        migrations = (
            ("template_enter", LEGACY_ENTER_TEMPLATE, DEFAULT_ENTER_TEMPLATE),
            ("template_exit", LEGACY_EXIT_TEMPLATE, DEFAULT_EXIT_TEMPLATE),
            ("notify_enter_text", LEGACY_NOTIFY_ENTER, DEFAULT_NOTIFY_ENTER),
            ("notify_exit_text", LEGACY_NOTIFY_EXIT, DEFAULT_NOTIFY_EXIT),
        )
        changed = False
        for key, legacy, new in migrations:
            if self._conf_str(key) == legacy:
                self.config[key] = new
                changed = True
                logger.info(
                    f"[KOOK TTS欢迎] 已升级默认文案 {key}：「{legacy}」→「{new}」"
                )
                self._trace(f"配置迁移：{key} 「{legacy}」→「{new}」")

        # v0.4.1 之前用「间隔 > 0」表示开启静音播报，迁移到独立开关；
        # 间隔为 0 但填过静音文案的，说明用户想要这个功能，一并开启
        if "mute_notify_enabled" not in self.config:
            old_interval = to_int(self.config.get("mute_poll_interval"), 0)
            has_template = any(
                self._conf_str(key)
                for key in (
                    "template_mute",
                    "template_unmute",
                    "template_mic_off",
                    "template_mic_on",
                )
            )
            if old_interval > 0 or has_template:
                self.config["mute_notify_enabled"] = True
                changed = True
                logger.info(
                    "[KOOK TTS欢迎] 已迁移静音播报开关："
                    f"原轮询间隔 {old_interval}s"
                    f"{'，已填静音文案' if has_template and old_interval <= 0 else ''}"
                    "，视为开启"
                )
                self._trace(f"配置迁移：静音播报开关 = 开（原间隔 {old_interval}s）")

        if changed:
            self._save_config("升级默认文案")

    def _trace(self, message: str) -> None:
        """把诊断信息追加到插件日志文件（不含 Bot Token）。"""
        if not self._conf_bool("diagnostic_log", True):
            return
        log_file = getattr(self, "_log_file", None)
        if log_file is None:
            return
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            if (
                log_file.exists()
                and log_file.stat().st_size > LOG_MAX_BYTES
            ):
                backup = log_file.with_name(f"{log_file.name}.1")
                with contextlib.suppress(OSError):
                    if backup.exists():
                        backup.unlink()
                    log_file.rename(backup)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with log_file.open("a", encoding="utf-8") as handle:
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
