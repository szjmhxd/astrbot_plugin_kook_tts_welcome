"""通用小工具：类型转换、子进程执行、ffprobe 封装。"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any


def to_mapping(value: Any) -> dict[str, Any]:
    """把 pydantic 模型 / 字典统一转成 dict，失败时返回空字典。"""
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            result = dump(mode="python")
        except TypeError:
            result = dump()
        if isinstance(result, dict):
            return result
    return {}


def to_text(value: Any) -> str:
    """把枚举 / 数字 / None 统一转成去空白字符串。"""
    if value is None:
        return ""
    raw = getattr(value, "value", value)
    return str(raw).strip()


def to_int(value: Any, default: int = 0) -> int:
    """把任意值转成 int，失败时返回默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def to_bool(value: Any, default: bool = False) -> bool:
    """把 bool / 数字 / 字符串统一转成布尔值（KOOK 部分字段是字符串）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


async def run_capture(
    args: list[str],
    timeout: float,
    on_start: Callable[[asyncio.subprocess.Process], None] | None = None,
) -> tuple[int, str, bool]:
    """执行子进程并收集输出，返回 ``(returncode, output, timed_out)``。

    Windows 上 Selector 事件循环没有子进程支持时自动退回线程中的同步执行。
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except NotImplementedError:
        return await asyncio.to_thread(_run_capture_sync, args, timeout)

    if on_start is not None:
        on_start(process)

    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        raise
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        return -1, "", True

    return (
        process.returncode if process.returncode is not None else -1,
        (stdout or b"").decode("utf-8", "ignore"),
        False,
    )


def _run_capture_sync(args: list[str], timeout: float) -> tuple[int, str, bool]:
    """``run_capture`` 的同步兜底实现。"""
    try:
        completed = subprocess.run(  # noqa: S603 - 参数由本插件拼装
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or b""
        return -1, output.decode("utf-8", "ignore"), True
    return completed.returncode, completed.stdout.decode("utf-8", "ignore"), False


def resolve_ffprobe(ffmpeg_path: str) -> str:
    """根据 FFmpeg 路径推断同级目录下的 ffprobe。"""
    if not ffmpeg_path:
        return "ffprobe"
    base = os.path.basename(ffmpeg_path)
    if base.lower().startswith("ffprobe"):
        return ffmpeg_path
    directory = os.path.dirname(ffmpeg_path)
    suffix = ".exe" if base.lower().endswith(".exe") else ""
    probe = f"ffprobe{suffix}"
    return os.path.join(directory, probe) if directory else probe


def ffprobe_available(ffmpeg_path: str) -> bool:
    """判断推导出的 ffprobe 是否存在（区分「音频损坏」与「没装 ffprobe」）。"""
    probe = resolve_ffprobe(ffmpeg_path)
    if os.path.isabs(probe) or os.path.dirname(probe):
        return Path(probe).is_file()
    return shutil.which(probe) is not None


async def probe_audio_duration(ffmpeg_path: str, audio_path: Path) -> float | None:
    """读取音频时长（秒），失败时返回 ``None``。"""
    args = [
        resolve_ffprobe(ffmpeg_path),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=nw=1:nk=1",
        str(audio_path),
    ]
    try:
        code, output, timed_out = await run_capture(args, timeout=12)
    except (OSError, FileNotFoundError):
        return None
    if code != 0 or timed_out:
        return None
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = float(line)
        except ValueError:
            continue
        if value > 0:
            return value
    return None
