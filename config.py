"""配置读取、目录解析与诊断日志（Mixin）。"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.star import StarTools

from .constants import (
    CONFIG_LAYOUT_VERSION,
    DEFAULT_LEAVE_DELAY,
    DEFAULT_MERGE_WINDOW_MS,
    DEFAULT_MULTI_USER_POLICY,
    DEFAULT_MUTE_POLL_SECONDS,
    DEFAULT_NOTIFY_ENTER,
    DEFAULT_NOTIFY_EXIT,
    DEFAULT_NOTIFY_MIC_OFF,
    DEFAULT_NOTIFY_MIC_ON,
    DEFAULT_NOTIFY_MUTE,
    DEFAULT_NOTIFY_UNMUTE,
    DEFAULT_START_DELAY_MS,
    LEGACY_CONFIG_MAP,
    LEGACY_TEXT_UPGRADE,
    LOG_MAX_BYTES,
    MULTI_USER_POLICIES,
    MUTE_POLL_MIN_SECONDS,
    PLUGIN_NAME,
    PLUGIN_VERSION,
    STATE_TEXT_TEMPLATE_PATH,
    STATE_TOGGLE_PATH,
    TARGET_CHANNELS_FILE_NAME,
)
from .utils import to_bool, to_int, to_text


class ConfigMixin:
    """配置读取、目录解析与诊断日志。方法由 main.py 的插件类继承。"""

    # ------------------------------------------------------------------ #
    # 配置读取（支持 a.b.c 嵌套路径）
    # ------------------------------------------------------------------ #

    def _conf_get(self, path: str, default: Any = None) -> Any:
        """按点号路径取配置值；任一层缺失返回 default。"""
        node: Any = self.config
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return default if node is None else node

    def _conf_set(self, path: str, value: Any) -> None:
        """按点号路径写配置值，中间层缺失时自动创建。"""
        parts = path.split(".")
        node: Any = self.config
        for part in parts[:-1]:
            child = node.get(part) if isinstance(node, dict) else None
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value

    def _conf_str(self, path: str, default: str = "") -> str:
        """读取字符串配置，None 与两端空白统一处理。"""
        value = self._conf_get(path, default)
        if value is None:
            return default
        return str(value).strip()

    def _conf_bool(self, path: str, default: bool = True) -> bool:
        """读取布尔配置，兼容 WebUI 可能保存的字符串形式。"""
        value = self._conf_get(path, default)
        return to_bool(value, default)

    def _conf_int(self, path: str, default: int, minimum: int, maximum: int) -> int:
        """读取整数配置并夹在 [minimum, maximum] 区间内。"""
        value = to_int(self._conf_get(path, default), default)
        clamped = max(minimum, min(maximum, value))
        if clamped != value:
            self._warn_clamped(path, value, clamped, minimum, maximum)
        return clamped

    def _conf_float(
        self, path: str, default: float, minimum: float, maximum: float
    ) -> float:
        """读取浮点配置并夹在 [minimum, maximum] 区间内。"""
        raw = self._conf_get(path, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = float(default)
        clamped = max(minimum, min(maximum, value))
        if clamped != value:
            self._warn_clamped(path, value, clamped, minimum, maximum)
        return clamped

    def _warn_clamped(
        self, path: str, value: Any, clamped: Any, minimum: Any, maximum: Any
    ) -> None:
        """配置越界被夹逼时告警，同一项只提醒一次（避免刷屏）。

        静默改值会让管理面板显示与实际生效不一致——用户填 999 秒冷却、
        实际生效 60 秒，排查时毫无线索，所以这里必须留痕。
        """
        warned = getattr(self, "_conf_warned", None)
        if warned is None:
            warned = set()
            self._conf_warned = warned
        if path in warned:
            return
        warned.add(path)
        message = (
            f"配置 {path} = {value} 超出允许范围 [{minimum}, {maximum}]，"
            f"已按 {clamped} 生效；请在管理面板改正，否则显示与实际不一致"
        )
        logger.warning(f"[KOOK TTS欢迎] {message}")
        self._trace(f"配置夹逼：{message}")

    # ------------------------------------------------------------------ #
    # 常用配置
    # ------------------------------------------------------------------ #

    @property
    def _enabled(self) -> bool:
        return self._conf_bool("enabled", True)

    @property
    def _enabled_guild_ids(self) -> frozenset[str]:
        """启用范围（目标服务器 ID 集合，空 = 所有服务器）。"""
        raw = self._conf_str("enabled_guilds")
        ids = {part.strip() for part in raw.replace("；", ";").split(";")}
        return frozenset(part for part in ids if part)

    def _guild_enabled(self, guild_id: str) -> bool:
        """该服务器是否在启用范围内。"""
        ids = self._enabled_guild_ids
        return not ids or guild_id in ids

    @property
    def _cooldown(self) -> int:
        return self._conf_int("playback.cooldown", 3, 0, 60)

    @property
    def _volume(self) -> int:
        return self._conf_int("playback.volume", 100, 0, 100)

    @property
    def _start_delay_ms(self) -> int:
        """入房到开播的最短间隔（毫秒）。"""
        return self._conf_int(
            "playback.start_delay_ms", DEFAULT_START_DELAY_MS, 0, 10000
        )

    @property
    def _multi_user_policy(self) -> str:
        """多人同时进房时的语音策略：ignore / interrupt / merge。"""
        value = self._conf_str(
            "strategy.multi_user_policy", DEFAULT_MULTI_USER_POLICY
        ).lower()
        return value if value in MULTI_USER_POLICIES else DEFAULT_MULTI_USER_POLICY

    @property
    def _merge_window_ms(self) -> int:
        """合并播报的收集窗口（毫秒）。"""
        return self._conf_int(
            "strategy.merge_window_ms", DEFAULT_MERGE_WINDOW_MS, 0, 30000
        )

    @property
    def _notify_text_channel_id(self) -> str:
        """兜底文字频道 ID（空 = 交给自动识别）。"""
        return self._conf_str("strategy.notify_text_channel_id")

    @property
    def _notify_auto_channel(self) -> bool:
        """是否自动识别服务器默认文字频道。"""
        return self._conf_bool("strategy.notify_auto_channel", True)

    @property
    def _notify_enter_text(self) -> str:
        return self._conf_str(
            "welcome_exit.welcome.text_template", DEFAULT_NOTIFY_ENTER
        )

    @property
    def _notify_exit_text(self) -> str:
        return self._conf_str("welcome_exit.exit.text_template", DEFAULT_NOTIFY_EXIT)

    @property
    def _notify_mute_text(self) -> str:
        return self._conf_str("mute_mic.mute.mute_text", DEFAULT_NOTIFY_MUTE)

    @property
    def _notify_unmute_text(self) -> str:
        return self._conf_str(
            "mute_mic.mute.unmute_text", DEFAULT_NOTIFY_UNMUTE
        )

    @property
    def _notify_mic_off_text(self) -> str:
        return self._conf_str("mute_mic.mic.mic_off_text", DEFAULT_NOTIFY_MIC_OFF)

    @property
    def _notify_mic_on_text(self) -> str:
        return self._conf_str("mute_mic.mic.mic_on_text", DEFAULT_NOTIFY_MIC_ON)

    @property
    def _welcome_voice_enabled(self) -> bool:
        """欢迎语音开关（只控语音；文字由文字模版留空控制）。"""
        return self._conf_bool("welcome_exit.welcome.enabled", True)

    @property
    def _exit_voice_enabled(self) -> bool:
        """退出语音开关（只控语音；文字由文字模版留空控制）。"""
        return self._conf_bool("welcome_exit.exit.enabled", False)

    @property
    def _mic_notify_enabled(self) -> bool:
        """闭麦播报开关（控制闭麦 / 开麦的语音 + 文字）。"""
        return self._conf_bool("mute_mic.mic.enabled", False)

    @property
    def _mute_notify_enabled(self) -> bool:
        """静音播报开关（控制静音 / 解除静音的语音 + 文字）。"""
        return self._conf_bool("mute_mic.mute.enabled", False)

    def _state_announce_enabled(self, state: str) -> bool:
        """静音 / 闭麦类状态的播报开关（语音 + 文字统一把关）。"""
        path = STATE_TOGGLE_PATH.get(state, "")
        return bool(path) and self._conf_bool(path, False)

    @property
    def _mute_poll_interval(self) -> int:
        """静音 / 闭麦轮询间隔（秒），两类开关都关闭时返回 0（= 不轮询）。"""
        if not (self._mic_notify_enabled or self._mute_notify_enabled):
            return 0
        raw = self._conf_int(
            "strategy.mute_poll_interval", DEFAULT_MUTE_POLL_SECONDS, 0, 300
        )
        return max(MUTE_POLL_MIN_SECONDS, raw)

    @property
    def _leave_delay(self) -> int:
        return self._conf_int("playback.leave_delay", DEFAULT_LEAVE_DELAY, 0, 10)

    @property
    def _ffmpeg_path(self) -> str:
        return self._conf_str("playback.ffmpeg_path") or "ffmpeg"

    def _ffmpeg_executable(self) -> str:
        """把配置里的 FFmpeg 路径解析成可执行文件（相对路径走 PATH 查找）。"""
        path = self._ffmpeg_path
        if os.path.isabs(path):
            return path
        return shutil.which(path) or path

    def _state_text_template(self, state: str) -> str:
        """取某个状态的文字播报文案（空字符串表示不发）。"""
        path = STATE_TEXT_TEMPLATE_PATH.get(state, "")
        return self._conf_str(path) if path else ""

    # ------------------------------------------------------------------ #
    # 目标语音频道绑定（每服务器一份，/语音欢迎目标频道 维护）
    # ------------------------------------------------------------------ #

    def _target_channels_file(self) -> Path:
        return self._data_dir / TARGET_CHANNELS_FILE_NAME

    def _load_target_channels(self) -> dict[str, str]:
        """读取 ``target_channels.json``（``{guild_id: voice_channel_id}``）。"""
        path = self._target_channels_file()
        if not path.is_file():
            return {}
        try:
            with path.open(encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"[KOOK TTS欢迎] 读取目标频道绑定失败：{exc}")
            return {}
        if not isinstance(data, dict):
            return {}
        return {to_text(k): to_text(v) for k, v in data.items() if to_text(k)}

    def _save_target_channels(self) -> bool:
        """落盘 ``target_channels.json``。"""
        try:
            self._target_channels_file().parent.mkdir(parents=True, exist_ok=True)
            with self._target_channels_file().open("w", encoding="utf-8") as handle:
                json.dump(
                    getattr(self, "_target_channels", {}), handle, ensure_ascii=False, indent=2
                )
            return True
        except OSError as exc:
            logger.warning(f"[KOOK TTS欢迎] 保存目标频道绑定失败：{exc}")
            self._trace(f"保存目标频道绑定失败：{exc!r}")
            return False

    def _bind_target_channel(self, guild_id: str, channel_id: str) -> bool:
        """绑定 / 解绑某服务器的目标语音频道（空频道 ID = 解绑）。"""
        if not guild_id:
            return False
        store = getattr(self, "_target_channels", None)
        if not isinstance(store, dict):
            store = {}
            self._target_channels = store
        if channel_id:
            store[guild_id] = channel_id
        else:
            store.pop(guild_id, None)
        return self._save_target_channels()

    def _target_channel_for(self, guild_id: str) -> str:
        """该服务器的目标语音频道：每服务器绑定 > 旧版全局配置 > 空（跟随事件）。"""
        bound = getattr(self, "_target_channels", {}).get(guild_id, "")
        return bound or self._conf_str("target_channel_id")

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
        guild_scope = (
            "、".join(sorted(self._enabled_guild_ids))
            if self._enabled_guild_ids
            else "全部服务器"
        )
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
            f"已加载 {PLUGIN_VERSION}：启用={self._enabled}，启用范围={guild_scope}，"
            f"欢迎语音={'开' if self._welcome_voice_enabled else '关'}，"
            f"退出语音={'开' if self._exit_voice_enabled else '关'}，"
            f"音量={self._volume}，冷却={self._cooldown}s，"
            f"启动延迟={self._start_delay_ms}ms，尾部静音={self._leave_delay}s，"
            f"文字播报={notify_desc}，静音闭麦播报="
            f"闭麦{'开' if self._mic_notify_enabled else '关'}/"
            f"静音{'开' if self._mute_notify_enabled else '关'}（{mute_desc}）"
        )
        logger.info(f"[KOOK TTS欢迎] {summary}")
        self._trace(f"插件加载：{summary}")
        self._trace(f"数据目录：{self._data_dir}")
        self._trace(f"诊断日志：{self._log_file}")

    def _migrate_config(self) -> None:
        """一次性迁移：v0.5.0 起配置按分组嵌套，把旧平铺键搬进新结构。

        旧键在 _conf_schema.json 中以 invisible 影子键保留（AstrBot 加载时
        会清掉 schema 之外的键，必须留着才能读到旧值）。迁移完成后写入
        ``_schema_version`` 标记，之后不再执行。
        """
        if to_int(self._conf_get("_schema_version"), 0) >= CONFIG_LAYOUT_VERSION:
            return

        # 1) 远古默认文案升级（在平铺影子键上做，随后随迁移到新路径）
        for key, (legacy_text, new_text) in LEGACY_TEXT_UPGRADE.items():
            if self._conf_str(key) == legacy_text:
                self._conf_set(key, new_text)
                logger.info(
                    f"[KOOK TTS欢迎] 已升级默认文案 {key}：「{legacy_text}」→「{new_text}」"
                )
                self._trace(f"配置迁移：{key} 「{legacy_text}」→「{new_text}」")

        # 2) 平铺 → 嵌套（旧键与新键默认值一致，全新安装时等价于无操作）
        for old, new in LEGACY_CONFIG_MAP.items():
            value = self._conf_get(old)
            if value is None:
                continue
            self._conf_set(new, value)

        # 3) 旧「启用静音 / 闭麦播报」总开关拆成闭麦 / 静音两个开关
        mute_on = self._conf_bool("mute_notify_enabled", False)
        self._conf_set("mute_mic.mic.enabled", mute_on)
        self._conf_set("mute_mic.mute.enabled", mute_on)

        self._conf_set("_schema_version", CONFIG_LAYOUT_VERSION)
        logger.info(
            "[KOOK TTS欢迎] 配置已迁移到 v0.5 分组布局"
            f"（闭麦播报={'开' if mute_on else '关'}，静音播报={'开' if mute_on else '关'}）"
        )
        self._trace(f"配置迁移：布局版本 → {CONFIG_LAYOUT_VERSION}")
        self._save_config("迁移配置到分组布局")

    def _trace(self, message: str) -> None:
        """把诊断信息追加到插件日志文件（不含 Bot Token）。"""
        if not self._conf_bool("playback.diagnostic_log", True):
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
