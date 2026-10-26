"""KOOK 开放平台 HTTP 客户端（语音接口 + 用户信息查询）。

会话管理两条硬性规则：

- API 会话带 ``Authorization: Bot <token>``，只用于 kookapp.cn；
- 任何第三方域名的请求都不带鉴权头，避免泄露 Bot Token。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import Any

import aiohttp

from .constants import KOOK_API_BASE
from .utils import to_bool, to_int, to_text


class KookApiError(RuntimeError):
    """KOOK 接口返回非 0 code 或网络异常。"""

    def __init__(self, code: Any, message: str, path: str = "") -> None:
        self.code = code
        self.kook_message = message
        self.path = path
        super().__init__(f"KOOK 接口 {path} 调用失败（code={code}）：{message}")


@dataclass(slots=True)
class VoiceJoinInfo:
    """``/voice/join`` 返回的推流信息。"""

    ip: str
    port: str
    rtcp_mux: bool
    rtcp_port: str
    bitrate: int
    audio_ssrc: str
    audio_pt: str

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> VoiceJoinInfo:
        """从接口返回值构造推流信息，字段缺失时给出安全默认值。"""
        info = cls(
            ip=to_text(data.get("ip")),
            port=to_text(data.get("port")),
            rtcp_mux=to_bool(data.get("rtcp_mux"), True),
            rtcp_port=to_text(data.get("rtcp_port")),
            bitrate=to_int(data.get("bitrate"), 48000),
            audio_ssrc=to_text(data.get("audio_ssrc")) or "1111",
            audio_pt=to_text(data.get("audio_pt")) or "111",
        )
        if not info.ip or not info.port:
            raise KookApiError(-1, f"返回的推流地址不完整：{data}", "/voice/join")
        return info


class KookApi:
    """最小化的 KOOK HTTP 客户端。"""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._token = ""

    async def _ensure_session(self, token: str | None = None) -> aiohttp.ClientSession:
        """获取（并按需创建）带鉴权头的 API 会话，token 变化时自动更新。"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            )
        if token and token != self._token:
            self._token = token
            self._session.headers["Authorization"] = f"Bot {token}"
        return self._session

    async def request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """调用 KOOK 开放接口，返回 ``data`` 字段；失败抛 :class:`KookApiError`。"""
        if not token:
            raise KookApiError(-1, "未配置 KOOK Bot Token", path)

        session = await self._ensure_session(token)
        url = f"{KOOK_API_BASE}{path}"
        try:
            async with session.request(
                method,
                url,
                params=params,
                json=payload,
            ) as response:
                text = await response.text()
                if response.status != 200:
                    raise KookApiError(
                        response.status,
                        f"HTTP {response.status}: {text[:300]}",
                        path,
                    )
                try:
                    result = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise KookApiError(
                        -1, f"响应不是合法 JSON：{text[:300]}", path
                    ) from exc

                if to_int(result.get("code"), -1) != 0:
                    raise KookApiError(
                        result.get("code"),
                        str(result.get("message") or "未知错误"),
                        path,
                    )

                data = result.get("data")
                return data if isinstance(data, dict) else {}
        except aiohttp.ClientError as exc:
            raise KookApiError(-1, f"网络请求失败：{exc!s}", path) from exc
        except asyncio.TimeoutError as exc:
            raise KookApiError(-1, "请求超时", path) from exc

    # ------------------------------------------------------------------ #
    # 语音接口
    # ------------------------------------------------------------------ #

    async def join_voice(
        self,
        token: str,
        channel_id: str,
        password: str = "",
    ) -> VoiceJoinInfo:
        """加入语音频道，返回推流所需的地址与 RTP 参数。"""
        # 显式要求独立 RTCP 端口：rtcp_mux=true 时 FFmpeg 不发 RTCP，
        # 部分媒体服务器需要 RTCP SR 才会转发音频。
        payload: dict[str, Any] = {"channel_id": channel_id, "rtcp_mux": False}
        if password:
            payload["password"] = password
        data = await self.request("POST", "/voice/join", token, payload=payload)
        return VoiceJoinInfo.from_data(data)

    async def leave_voice(self, token: str, channel_id: str) -> None:
        """离开语音频道。"""
        await self.request(
            "POST",
            "/voice/leave",
            token,
            payload={"channel_id": channel_id},
        )

    async def keep_alive(self, token: str, channel_id: str) -> None:
        """语音保活，长播放时必须定期调用。"""
        await self.request(
            "POST",
            "/voice/keep-alive",
            token,
            payload={"channel_id": channel_id},
        )

    async def voice_list(self, token: str) -> list[dict[str, Any]]:
        """查询本 Bot 当前加入的语音频道列表。"""
        data = await self.request("GET", "/voice/list", token)
        items = data.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        return []

    # ------------------------------------------------------------------ #
    # 用户信息
    # ------------------------------------------------------------------ #

    async def get_me(self, token: str) -> dict[str, Any]:
        """查询机器人自身信息（拿 bot_id）。"""
        return await self.request("GET", "/user/me", token)

    async def get_user(
        self, token: str, user_id: str, guild_id: str = ""
    ) -> dict[str, Any]:
        """查询目标用户信息，返回含 ``username`` / ``nickname`` 的 User 对象。

        ``nickname`` 是用户在该服务器的昵称（可能为空字符串）；
        ``username`` 是全局用户名。``guild_id`` 传入后才能拿到服务器昵称。
        """
        params: dict[str, Any] = {"user_id": user_id}
        if guild_id:
            params["guild_id"] = guild_id
        return await self.request("GET", "/user/view", token, params=params)

    async def close(self) -> None:
        """关闭 HTTP 会话，插件卸载时调用。"""
        session = self._session
        self._session = None
        if session and not session.closed:
            with contextlib.suppress(Exception):
                await session.close()
