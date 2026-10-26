"""播报流程（Mixin）。

完整流程：查昵称 → 渲染文案 → TTS 合成 → 校验 FFmpeg → 加入语音频道 →
保活 → 延迟启动 → FFmpeg RTP 推流 → 离开频道。其中「音频准备」与「加入
语音频道」并行执行，压缩事件到开播的整体延迟。同时负责：

- 播放请求队列与 worker 循环（同一时间只播一条）；
- 机器人被移出频道时立刻中止推流；
- 尾部静音的预合成与缓存（整段播放只有一条 RTP 流）；
- 残留语音连接的释放与重试。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger

from .constants import (
    DEFAULT_PLAYBACK_TIMEOUT,
    PLUGIN_NAME,
    STALE_VOICE_RELEASE_WAIT,
    VOICE_KEEP_ALIVE_INTERVAL,
)
from .ffmpeg import build_ffmpeg_args, playback_timeout
from .hooking import VoiceStateEvent
from .kook_api import KookApiError, VoiceJoinInfo
from .utils import ffprobe_available, probe_audio_duration, run_capture, to_text
from .tts import TtsError


@dataclass(slots=True)
class _PlayRequest:
    """一次播报请求。``reason`` 为 ``event``（成员状态变化）或 ``preview``。

    ``events`` 可以是多条（合并播报）；``force`` 为 True 时跳过冷却检查
    （打断策略产生的请求必须立刻播，不能被冷却拦住）。
    """

    events: list[VoiceStateEvent]
    reason: str = "event"
    # 试听用：直接指定播报文本，跳过模板渲染
    override_text: str = ""
    force: bool = False


class PlaybackMixin:
    """播报队列与推流流程。方法由 main.py 的插件类继承。"""

    # ------------------------------------------------------------------ #
    # 队列
    # ------------------------------------------------------------------ #

    def _enqueue(self, request: _PlayRequest) -> None:
        """入队一次播报请求；队列已满时用新请求顶掉旧请求。"""
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(request)

    # ------------------------------------------------------------------ #
    # 多人冲突策略
    # ------------------------------------------------------------------ #

    def _interrupt_playback(self) -> None:
        """打断策略：中止当前推流、清空待播队列，为新请求让路。"""
        self._interrupted = True
        self._stream_aborted = True
        while not self._queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        proc = self._ffmpeg_proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        logger.info("[KOOK TTS欢迎] 新的进房事件打断了当前播报")
        self._trace("打断：后进事件中止当前播报")

    def _buffer_for_merge(self, event: VoiceStateEvent) -> None:
        """合并策略：把事件攒进缓冲区，窗口结束后一次性播报。"""
        self._merge_buffer.append(event)
        if self._merge_task is None or self._merge_task.done():
            self._merge_task = asyncio.create_task(
                self._merge_flush_loop(),
                name=f"{PLUGIN_NAME}-merge",
            )

    async def _merge_flush_loop(self) -> None:
        """等待合并窗口关闭，把攒下的事件作为一条播报入队。"""
        window = self._merge_window_ms / 1000.0
        if window > 0:
            await asyncio.sleep(window)

        while self._merge_buffer:
            # 播报中 / 队列已占 / 冷却中：继续攒着，稍后再投递
            if self._playing or not self._queue.empty():
                await asyncio.sleep(0.2)
                continue
            if (
                self._last_finished
                and time.monotonic() - self._last_finished < self._cooldown
            ):
                await asyncio.sleep(0.2)
                continue

            events = self._merge_buffer[:]
            self._merge_buffer.clear()
            self._enqueue(_PlayRequest(events=events))
            names = "、".join(e.user_id for e in events)
            logger.info(
                f"[KOOK TTS欢迎] 合并播报 {len(events)} 人（{names}），准备播放"
            )
            self._trace(f"合并播报入队：{len(events)} 人 user={names}")

    async def _worker_loop(self) -> None:
        """串行消费播报请求，保证同一时间只有一次推流。"""
        while True:
            request = await self._queue.get()
            self._playing = True
            try:
                await self._play_request(request)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("[KOOK TTS欢迎] 播报失败")
                self._trace(f"播报异常：{exc!r}")
            finally:
                self._playing = False
                self._last_finished = time.monotonic()
                self._queue.task_done()

    # ------------------------------------------------------------------ #
    # 推流子进程跟踪
    # ------------------------------------------------------------------ #

    def _track_ffmpeg_process(self, process: asyncio.subprocess.Process) -> None:
        """记录当前推流进程；若已收到中止事件，创建后立刻结束它。"""
        self._ffmpeg_proc = process
        if self._stream_aborted and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()

    def _on_bot_left_voice(self, channel_id: str) -> None:
        """机器人自己被移出语音频道：立刻停止推流。"""
        current = self._voice_channel
        if not current:
            self._trace(f"忽略机器人退出事件：当前不在语音频道（channel={channel_id}）")
            return
        if channel_id and channel_id != current:
            self._trace(
                f"忽略机器人退出事件：channel={channel_id} 不是正在使用的 {current}"
            )
            return

        self._stream_aborted = True
        logger.warning(f"[KOOK TTS欢迎] 机器人被移出语音频道 {current}，中止本次推流")
        self._trace(f"机器人被移出语音频道 {current}，中止本次推流")
        proc = self._ffmpeg_proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #

    async def _play_request(self, request: _PlayRequest) -> None:
        """一次完整的播报流程，由 worker 调用，异常不应逃逸。"""
        if not request.events:
            self._trace("跳过：播报请求没有事件")
            return
        event = request.events[0]
        channel_id = event.channel_id
        self._trace(
            f"开始播报流程：state={event.state} channel={channel_id} "
            f"user={event.user_id} n={len(request.events)} "
            f"reason={request.reason} force={request.force}"
        )

        if (
            request.reason == "event"
            and not request.force
            and time.monotonic() - self._last_finished < self._cooldown
        ):
            logger.debug("[KOOK TTS欢迎] 冷却未结束，跳过本次播报")
            self._trace("跳过：冷却未结束")
            return

        token = self._resolve_token()
        if not token:
            logger.error(
                "[KOOK TTS欢迎] 未获取到 KOOK Bot Token：请确认 KOOK 适配器已启用，"
                "或在插件配置中填写 bot_token"
            )
            self._trace("失败：未获取到 Bot Token")
            return

        ffmpeg_ok, ffmpeg_detail = await self._check_ffmpeg()
        if not ffmpeg_ok:
            logger.error(f"[KOOK TTS欢迎] FFmpeg 不可用，无法推流：{ffmpeg_detail}")
            self._trace(f"失败：FFmpeg 不可用（{ffmpeg_detail}）")
            return

        await self._ensure_bot_id(token)

        # 无文案直接跳过，避免白白占用语音频道
        if not request.override_text:
            single_user = event.user_id if len(request.events) == 1 else ""
            if not (
                (single_user and self._custom_template(single_user, event.state))
                or self._state_template(event.state)
            ):
                self._trace(f"跳过：状态 {event.state} 未配置播报文案")
                return

        self._stream_aborted = False
        self._interrupted = False

        # ---- 音频准备与加入频道并行，压缩事件到开播的延迟 ---- #
        join_done_at: list[float] = []

        async def _join_and_stamp() -> VoiceJoinInfo:
            info = await self._join_voice(token, channel_id)
            join_done_at.append(time.monotonic())
            return info

        parallel_started = time.monotonic()
        audio_task = asyncio.create_task(
            self._prepare_audio_bundle(request, token),
            name=f"{PLUGIN_NAME}-audio",
        )
        join_task = asyncio.create_task(
            _join_and_stamp(),
            name=f"{PLUGIN_NAME}-join",
        )
        audio_result, join_result = await asyncio.gather(
            audio_task, join_task, return_exceptions=True
        )
        parallel_ms = (time.monotonic() - parallel_started) * 1000

        if isinstance(join_result, BaseException):
            if isinstance(join_result, asyncio.CancelledError):
                raise join_result
            logger.error(f"[KOOK TTS欢迎] 加入语音频道失败：{join_result}")
            self._trace(f"失败：加入语音频道（{join_result!r}）")
            return

        info = join_result
        self._voice_channel = channel_id
        logger.info(
            f"[KOOK TTS欢迎] 已加入语音频道 {channel_id}"
            f"（{info.ip}:{info.port}，码率 {info.bitrate}）"
        )
        self._trace(
            f"加入语音频道成功：channel={channel_id} media={info.ip}:{info.port} "
            f"bitrate={info.bitrate} ssrc={info.audio_ssrc} pt={info.audio_pt} "
            f"并行准备={parallel_ms:.0f}ms"
        )

        if isinstance(audio_result, BaseException):
            if isinstance(audio_result, asyncio.CancelledError):
                raise audio_result
            logger.error(f"[KOOK TTS欢迎] 语音准备失败：{audio_result}")
            self._trace(f"失败：音频准备（{audio_result!r}）")
            with contextlib.suppress(Exception):
                await self._leave_voice(token, channel_id)
            return

        speak_text, stream_path, duration, speech_duration, tail_seconds = audio_result

        keepalive = asyncio.create_task(
            self._keepalive_loop(token, channel_id),
            name=f"{PLUGIN_NAME}-keepalive",
        )
        try:
            # start_delay_ms 是「入房 → 开播」的最短间隔；若准备工作已经耗掉
            # 这段时间（TTS 比入房慢），就不再额外等待。
            waited = (
                time.monotonic() - join_done_at[0] if join_done_at else 0.0
            )
            effective_delay = max(0.0, self._start_delay_ms / 1000.0 - waited)
            if effective_delay >= 0.05:
                logger.info(
                    f"[KOOK TTS欢迎] 等待 {effective_delay * 1000:.0f} 毫秒后开始播放，"
                    "给听众客户端建立语音接收的时间"
                )
                self._trace(
                    f"启动延迟：{effective_delay * 1000:.0f}ms"
                    f"（配置 {self._start_delay_ms}ms）"
                )
                await asyncio.sleep(effective_delay)
            else:
                self._trace(
                    f"启动延迟已被准备工作吸收（入房后已过 {waited * 1000:.0f}ms）"
                )

            if self._stream_aborted:
                self._trace("启动前收到机器人退出事件，取消本次播放")
                return

            await self._stream_audio(
                stream_path,
                info,
                display_name=speak_text,
                duration=duration,
                speech_duration=speech_duration,
                tail_seconds=tail_seconds,
            )
        finally:
            keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keepalive
            # 卸载 / 重载会取消 worker，用 shield 保证离开请求一定发得出去
            with contextlib.suppress(Exception):
                await asyncio.shield(self._leave_voice(token, channel_id))
            self._stream_aborted = False

    async def _prepare_audio_bundle(
        self, request: _PlayRequest, token: str
    ) -> tuple[str, Path, float | None, float | None, float]:
        """昵称 → 文案 → TTS → 尾部静音 → 时长探测，返回可推流的音频。

        返回 ``(播报文本, 推流文件, 总时长, 语音时长, 尾部静音秒)``。
        与「加入语音频道」并行执行，因此这里的耗时大部分被入房过程掩盖。
        """
        events = request.events
        event = events[0]
        t0 = time.monotonic()

        speak_text = request.override_text
        if not speak_text:
            names = await self._resolve_many_names(token, events)
            channel_name = await self._resolve_channel_name(token, event.channel_id)
            single_user = event.user_id if len(events) == 1 else ""
            speak_text = self._build_speak_text(
                event.state, single_user, names, channel_name
            )
        if not speak_text:
            raise TtsError("未配置播报文案")
        t1 = time.monotonic()

        audio_path = await self.synthesize(speak_text)
        t2 = time.monotonic()

        # 先把「播放结束后停留」的静音拼进音频，整段播放只保留一条 RTP 流
        tail_seconds = float(self._leave_delay)
        stream_path = await self._prepare_stream_audio(audio_path)
        if stream_path == audio_path:
            tail_seconds = 0.0
        t3 = time.monotonic()

        speech_duration = await probe_audio_duration(
            self._ffmpeg_executable(), audio_path
        )
        duration = await probe_audio_duration(self._ffmpeg_executable(), stream_path)
        if duration is None and ffprobe_available(self._ffmpeg_executable()):
            raise TtsError(f"音频无法解析（可能已损坏）：{audio_path}")
        t4 = time.monotonic()

        self._trace(
            "音频准备完成："
            f"文案={1000 * (t1 - t0):.0f}ms "
            f"TTS={1000 * (t2 - t1):.0f}ms "
            f"尾部静音={1000 * (t3 - t2):.0f}ms "
            f"探测={1000 * (t4 - t3):.0f}ms "
            f"合计={1000 * (t4 - t0):.0f}ms"
        )
        return speak_text, stream_path, duration, speech_duration, tail_seconds

    async def _stream_audio(
        self,
        audio_path: Path,
        info: VoiceJoinInfo,
        display_name: str = "",
        duration: float | None = None,
        speech_duration: float | None = None,
        tail_seconds: float = 0.0,
    ) -> None:
        """启动 FFmpeg 推流并等待结束，按退出情况记录日志。"""
        ffmpeg = self._ffmpeg_executable()
        args = build_ffmpeg_args(
            ffmpeg=ffmpeg,
            audio_path=audio_path,
            info=info,
            volume=self._volume,
        )

        if duration is None:
            duration = await probe_audio_duration(ffmpeg, audio_path)
        timeout = playback_timeout(duration, DEFAULT_PLAYBACK_TIMEOUT)

        # 明确区分：音频文件时长（含尾部静音）≠ 进房后等待时间 ≠ 推流墙钟时间
        if duration and speech_duration and tail_seconds > 0:
            duration_text = (
                f"语音 {speech_duration:.1f} 秒 + 尾部静音 {tail_seconds:.1f} 秒"
                f"，共 {duration:.1f} 秒"
            )
        elif duration:
            duration_text = f"{duration:.1f} 秒"
        else:
            duration_text = "未知时长"
        logger.info(
            f"[KOOK TTS欢迎] 开始播报「{display_name}」"
            f"（音频时长：{duration_text}）"
        )
        self._trace(f"开始推流：{' '.join(args)}")

        started = time.monotonic()
        self._ffmpeg_proc = None
        try:
            code, output, timed_out = await run_capture(
                args,
                timeout,
                on_start=self._track_ffmpeg_process,
            )
        finally:
            self._ffmpeg_proc = None
        elapsed = time.monotonic() - started

        if self._interrupted:
            logger.info("[KOOK TTS欢迎] 播报已被新的进房事件打断")
            self._trace(f"推流中止：被新事件打断（用时 {elapsed:.1f}s）")
            self._interrupted = False
        elif self._stream_aborted:
            logger.info("[KOOK TTS欢迎] 机器人已离开语音频道，推流被中止")
            self._trace(f"推流中止：机器人不在频道内（用时 {elapsed:.1f}s）")
        elif timed_out:
            logger.warning(f"[KOOK TTS欢迎] 推流超时（{timeout:.0f}s）已被终止")
            self._trace(f"推流超时：{timeout:.0f}s")
        elif code != 0:
            detail = output.strip()[:500] or "无错误输出"
            logger.error(f"[KOOK TTS欢迎] FFmpeg 推流失败（退出码 {code}）：{detail}")
            self._trace(f"推流失败：exit={code} {detail}")
        else:
            logger.info(
                f"[KOOK TTS欢迎] 播报完成（FFmpeg 推流墙钟用时 {elapsed:.1f} 秒）"
            )
            self._trace(f"推流完成：用时 {elapsed:.2f}s")

    # ------------------------------------------------------------------ #
    # 尾部静音合成
    # ------------------------------------------------------------------ #

    async def _prepare_stream_audio(self, audio_path: Path) -> Path:
        """把「播放结束后停留」的静音拼进音频，返回真正用于推流的文件。

        合成结果按（路径、大小、修改时间、静音长度）缓存；失败或音频过长时
        退回原始文件。
        """
        tail = self._leave_delay
        if tail <= 0:
            return audio_path

        try:
            stat = audio_path.stat()
        except OSError as exc:
            logger.debug(f"[KOOK TTS欢迎] 读取音频信息失败，跳过尾部静音：{exc}")
            return audio_path

        key = f"{audio_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{tail}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        target = self._data_dir / "stream_cache" / f"tail_{digest}.wav"
        try:
            if target.is_file() and target.stat().st_size > 44:
                return target
        except OSError:
            pass

        args = [
            self._ffmpeg_executable(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(audio_path),
            "-f",
            "lavfi",
            "-t",
            f"{tail:.2f}",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-filter_complex",
            "[0:a]aformat=sample_fmts=fltp:sample_rates=48000:"
            "channel_layouts=stereo[a0];"
            "[1:a]aformat=sample_fmts=fltp:sample_rates=48000:"
            "channel_layouts=stereo[a1];"
            "[a0][a1]concat=n=2:v=0:a=1[out]",
            "-map",
            "[out]",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-f",
            "wav",
        ]

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(f"[KOOK TTS欢迎] 无法创建缓存目录 {target.parent}：{exc}")
            return audio_path

        temp = target.with_name(target.name + ".part")
        self._trace(f"合成尾部静音：{tail}s -> {target}")
        try:
            code, output, timed_out = await run_capture([*args, str(temp)], timeout=30)
        except Exception as exc:
            logger.warning(f"[KOOK TTS欢迎] 合成尾部静音失败：{exc}")
            with contextlib.suppress(OSError):
                temp.unlink()
            return audio_path

        if timed_out or code != 0:
            detail = output.strip()[:300] or "无输出"
            logger.warning(
                f"[KOOK TTS欢迎] 合成尾部静音失败（exit={code}），本次不推尾部静音：{detail}"
            )
            self._trace(f"合成尾部静音失败：exit={code} {detail}")
            with contextlib.suppress(OSError):
                temp.unlink()
            return audio_path

        try:
            os.replace(temp, target)
        except OSError as exc:
            logger.warning(f"[KOOK TTS欢迎] 保存尾部静音缓存失败：{exc}")
            with contextlib.suppress(OSError):
                temp.unlink()
            return audio_path

        self._trace(f"尾部静音已合成：{target}")
        return target

    # ------------------------------------------------------------------ #
    # 语音连接管理
    # ------------------------------------------------------------------ #

    async def _keepalive_loop(self, token: str, channel_id: str) -> None:
        """推流期间定期发送语音保活。"""
        while True:
            await asyncio.sleep(VOICE_KEEP_ALIVE_INTERVAL)
            try:
                await self._api.keep_alive(token, channel_id)
                logger.debug("[KOOK TTS欢迎] 已发送语音保活")
            except Exception as exc:
                logger.debug(f"[KOOK TTS欢迎] 语音保活失败：{exc}")

    async def _join_voice(self, token: str, channel_id: str) -> VoiceJoinInfo:
        """加入语音频道；失败时释放残留连接并重试一次。"""
        password = self._conf_str("voice_password")
        try:
            return await self._api.join_voice(token, channel_id, password=password)
        except KookApiError as exc:
            logger.warning(
                f"[KOOK TTS欢迎] 加入语音频道失败：{exc}；尝试释放残留连接后重试"
            )
            self._trace(f"加入语音频道失败：{exc}；准备释放残留连接后重试")

        await self._release_stale_voice(token, channel_id)
        await asyncio.sleep(STALE_VOICE_RELEASE_WAIT)
        return await self._api.join_voice(token, channel_id, password=password)

    async def _release_stale_voice(self, token: str, target_channel: str = "") -> None:
        """释放残留的语音连接，最多一条，优先目标频道。"""
        try:
            items = await self._api.voice_list(token)
        except Exception as exc:
            logger.debug(f"[KOOK TTS欢迎] 获取语音频道列表失败：{exc}")
            return

        candidates: list[str] = []
        for item in items:
            channel_id = to_text(item.get("id"))
            if channel_id and channel_id not in candidates:
                candidates.append(channel_id)
        if not candidates:
            self._trace("没有需要释放的残留语音连接")
            return

        channel_id = target_channel if target_channel in candidates else candidates[0]
        if len(candidates) > 1:
            self._trace(f"语音连接列表有多条记录，只释放 {channel_id}：{candidates}")

        with contextlib.suppress(Exception):
            await self._api.leave_voice(token, channel_id)
            logger.info(f"[KOOK TTS欢迎] 已释放残留语音连接：{channel_id}")
            self._trace(f"已释放残留语音连接：{channel_id}")

    async def _leave_voice(self, token: str, channel_id: str) -> bool:
        """离开语音频道，返回是否成功（失败时不抛异常）。"""
        ok = False
        try:
            await self._api.leave_voice(token, channel_id)
            ok = True
            logger.info(f"[KOOK TTS欢迎] 已离开语音频道 {channel_id}")
            self._trace(f"已离开语音频道：{channel_id}")
        except Exception as exc:
            logger.warning(f"[KOOK TTS欢迎] 离开语音频道失败：{exc}")
            self._trace(f"离开语音频道失败：{exc!r}")
        finally:
            if self._voice_channel == channel_id:
                self._voice_channel = ""
        return ok
