"""昵称解析与 TTS 合成。

职责：

1. **昵称解析**：调用 ``/user/view`` 获取 ``nickname``（服务器昵称，优先）
   与 ``username``（全局用户名，兜底），带 TTL 内存缓存，避免每次进房都打 API；
2. **文案渲染**：按状态（进入 / 退出 …）取模板，支持指定用户定制文案，
   占位符 ``{nickname}`` ``{username}`` ``{user_id}``；
3. **语音合成**：优先用 AstrBot 已配置的 TTS Provider（Edge-TTS / OpenAI /
   GSVI 等都行），没有则直接用 edge-tts 兜底；结果按文本哈希缓存，同一句
   话不重复合成。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .constants import (
    DEFAULT_EDGE_VOICE,
    DEFAULT_ENTER_TEMPLATE,
    DEFAULT_EXIT_TEMPLATE,
    NICKNAME_CACHE_MAX,
    NICKNAME_CACHE_TTL,
    STATE_ENTER,
    STATE_EXIT,
    TTS_CACHE_DIR_NAME,
    TTS_TEXT_MAX_CHARS,
    USER_TEMPLATES_FILE_NAME,
)
from .kook_api import KookApiError
from .utils import to_text


class TtsError(RuntimeError):
    """TTS 合成失败。"""


class TtsMixin:
    """昵称解析、文案渲染与语音合成。方法由 main.py 的插件类继承。"""

    # ------------------------------------------------------------------ #
    # 昵称
    # ------------------------------------------------------------------ #

    async def _resolve_display_name(
        self, token: str, user_id: str, guild_id: str = ""
    ) -> dict[str, str]:
        """查询用户昵称，返回 ``{"nickname":…, "username":…}``。

        查询失败不阻塞欢迎流程：两项都回退为 user_id。
        """
        cache_key = f"{guild_id}:{user_id}"
        cached = self._nickname_cache.get(cache_key)
        now = time.monotonic()
        if cached is not None and now - cached[0] < NICKNAME_CACHE_TTL:
            return dict(cached[1])

        info = {"nickname": "", "username": ""}
        try:
            user = await self._api.get_user(token, user_id, guild_id)
            info["username"] = to_text(user.get("username"))
            info["nickname"] = to_text(user.get("nickname"))
        except KookApiError as exc:
            logger.debug(f"[KOOK TTS欢迎] 查询用户 {user_id} 信息失败：{exc}")
            self._trace(f"查询用户信息失败：user={user_id} {exc}")

        self._remember_nickname(cache_key, info)
        return info

    async def _prefetch_nickname(self, user_id: str, guild_id: str = "") -> None:
        """事件到达后立刻预取昵称，与队列调度并行，减少 worker 启动后的等待。"""
        token = self._resolve_token()
        if not token or not user_id:
            return
        with contextlib.suppress(Exception):
            await self._resolve_display_name(token, user_id, guild_id)

    def _remember_nickname(self, key: str, info: dict[str, str]) -> None:
        """写入昵称缓存，超出上限时淘汰最旧记录。"""
        self._nickname_cache[key] = (time.monotonic(), dict(info))
        while len(self._nickname_cache) > NICKNAME_CACHE_MAX:
            self._nickname_cache.popitem(last=False)

    # ------------------------------------------------------------------ #
    # 文案渲染
    # ------------------------------------------------------------------ #

    def _state_template(self, state: str) -> str:
        """取某个状态的播报文案模板；未配置时用默认模板，空字符串表示关闭。"""
        key = f"template_{state}"
        if key in self.config:
            return self._conf_str(key)
        return {
            STATE_ENTER: DEFAULT_ENTER_TEMPLATE,
            STATE_EXIT: DEFAULT_EXIT_TEMPLATE,
        }.get(state, "")

    def _render_text(
        self,
        template: str,
        user_id: str,
        names: dict[str, str],
    ) -> str:
        """把模板渲染成最终播报文本。"""
        nickname = names.get("nickname") or names.get("username") or user_id
        username = names.get("username") or nickname
        text = template.replace("{nickname}", nickname)
        text = text.replace("{username}", username)
        text = text.replace("{user_id}", user_id)
        text = " ".join(text.split())
        return text[:TTS_TEXT_MAX_CHARS].strip()

    def _custom_template(self, user_id: str, state: str) -> str:
        """取指定用户的定制文案；没有则返回空串。

        存储结构（``user_templates.json``）::

            {
              "<user_id>": {
                "enter": "欢迎老大 {nickname}",
                "exit": "老大慢走"
              }
            }
        """
        entry = self._user_templates.get(user_id)
        if not isinstance(entry, dict):
            return ""
        return to_text(entry.get(state))

    def _build_speak_text(
        self,
        state: str,
        user_id: str,
        names: dict[str, str],
    ) -> str:
        """合成播报文本：用户定制文案优先，其次状态模板。"""
        template = self._custom_template(user_id, state) or self._state_template(state)
        if not template:
            return ""
        return self._render_text(template, user_id, names)

    # ------------------------------------------------------------------ #
    # TTS 合成
    # ------------------------------------------------------------------ #

    async def synthesize(self, text: str) -> Path:
        """把文本合成为音频文件，返回文件路径。

        优先级：TTS 缓存 → AstrBot TTS Provider → edge-tts 兜底。
        """
        if not text:
            raise TtsError("合成文本为空")

        cache_path = self._tts_cache_path(text)
        if cache_path.is_file() and cache_path.stat().st_size > 0:
            self._trace(f"TTS 命中缓存：{cache_path.name}")
            return cache_path

        audio_path = await self._synthesize_via_astrbot(text)
        if audio_path is None:
            audio_path = await self._synthesize_via_edge_tts(text, cache_path)
        if audio_path is None:
            raise TtsError("所有 TTS 引擎均不可用或合成失败")

        # 非缓存引擎的结果复制进缓存目录
        if audio_path != cache_path:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(Path(audio_path).read_bytes())
                return cache_path
            except OSError as exc:
                logger.warning(f"[KOOK TTS欢迎] 写入 TTS 缓存失败：{exc}")
                return Path(audio_path)
        return cache_path

    async def _synthesize_via_astrbot(self, text: str) -> Path | None:
        """通过 AstrBot 配置的 TTS Provider 合成；不可用时返回 None。"""
        provider = await self._get_tts_provider()
        if provider is None:
            return None
        try:
            result = await provider.get_audio(text)
        except Exception as exc:
            logger.warning(f"[KOOK TTS欢迎] AstrBot TTS 合成失败，尝试 edge-tts：{exc}")
            self._trace(f"AstrBot TTS 合成失败：{exc!r}")
            return None

        path = Path(to_text(result)) if result else None
        if path is None or not path.is_file():
            logger.warning("[KOOK TTS欢迎] AstrBot TTS 未返回有效音频文件")
            self._trace(f"AstrBot TTS 返回无效路径：{result!r}")
            return None
        self._trace(f"AstrBot TTS 合成成功：{path}")
        return path

    async def _get_tts_provider(self) -> Any:
        """取 AstrBot 当前使用的 TTS Provider 实例，没有则返回 None。"""
        manager = getattr(self.context, "provider_manager", None)
        if manager is None:
            return None
        try:
            from astrbot.core.provider.entities import ProviderType

            getter = getattr(manager, "get_using_provider_async", None)
            if getter is not None:
                provider = await getter(ProviderType.TEXT_TO_SPEECH)
            else:
                provider = manager.get_using_provider(ProviderType.TEXT_TO_SPEECH)
            if provider is not None:
                return provider
        except Exception as exc:
            logger.debug(f"[KOOK TTS欢迎] 获取 TTS Provider 失败：{exc}")

        # 退回第一个已加载的 TTS 实例
        insts = getattr(manager, "tts_provider_insts", None) or []
        return insts[0] if insts else None

    async def _synthesize_via_edge_tts(self, text: str, target: Path) -> Path | None:
        """edge-tts 兜底合成，直接写到缓存路径。"""
        voice = self._conf_str("edge_voice") or DEFAULT_EDGE_VOICE
        try:
            import edge_tts  # noqa: PLC0415 - 延迟导入，未安装时不影响其它路径
        except ImportError:
            logger.error(
                "[KOOK TTS欢迎] 未配置 AstrBot TTS Provider，且未安装 edge-tts。"
                "请在管理面板配置语音合成提供商，或 pip install edge-tts"
            )
            self._trace("失败：无可用 TTS 引擎（未配置 Provider，未安装 edge-tts）")
            return None

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(str(target))
        except Exception as exc:
            logger.error(f"[KOOK TTS欢迎] edge-tts 合成失败：{exc}")
            self._trace(f"edge-tts 合成失败：{exc!r}")
            return None

        if not target.is_file() or target.stat().st_size == 0:
            logger.error("[KOOK TTS欢迎] edge-tts 输出了空文件")
            self._trace("失败：edge-tts 输出空文件")
            return None
        self._trace(f"edge-tts 合成成功（{voice}）：{target}")
        return target

    def _tts_cache_path(self, text: str) -> Path:
        """按文本 + 音色哈希生成缓存文件路径。"""
        voice = self._conf_str("edge_voice") or DEFAULT_EDGE_VOICE
        digest = hashlib.sha256(f"{voice}|{text}".encode("utf-8")).hexdigest()[:20]
        return self._data_dir / TTS_CACHE_DIR_NAME / f"{digest}.mp3"

    # ------------------------------------------------------------------ #
    # 用户定制文案的存取
    # ------------------------------------------------------------------ #

    def _load_user_templates(self) -> dict[str, Any]:
        """从数据目录读取用户定制文案表。"""
        path = self._data_dir / USER_TEMPLATES_FILE_NAME
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError) as exc:
            logger.warning(f"[KOOK TTS欢迎] 读取用户定制文案失败：{exc}")
            return {}

    def _save_user_templates(self) -> bool:
        """落盘用户定制文案表。"""
        path = self._data_dir / USER_TEMPLATES_FILE_NAME
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self._user_templates, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return True
        except OSError as exc:
            logger.warning(f"[KOOK TTS欢迎] 保存用户定制文案失败：{exc}")
            self._trace(f"保存用户定制文案失败：{exc!r}")
            return False
