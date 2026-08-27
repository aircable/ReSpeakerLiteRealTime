import asyncio
import base64
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Any, BinaryIO

from fastapi import WebSocket, WebSocketDisconnect

from .config import Settings
from .context import build_instructions
from .db import Database
from .planner import Planner
from .protocol import FRAME_BYTES, DeviceState, server_message
from .realtime import RealtimeConnection

logger = logging.getLogger(__name__)
PLAYBACK_FRAME_SECONDS = 0.020
MAX_QUEUED_PLAYBACK_FRAMES = 1500
MAX_QUEUED_INPUT_FRAMES = 250  # Five seconds of 20 ms startup/jitter buffering.


@dataclass
class OutputStream:
    stream_id: str
    response_id: str
    item_id: str
    content_index: int
    played_ms: int = 0
    sent_ms: int = 0
    ended: bool = False
    end_queued: bool = False
    ended_monotonic: float = 0.0


@dataclass(frozen=True)
class PlaybackPacket:
    stream_id: str
    data: bytes | None


class DeviceSession:
    def __init__(
        self,
        websocket: WebSocket,
        device_id: str,
        settings: Settings,
        db: Database,
        planner: Planner,
        observer: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ):
        self.websocket = websocket
        self.device_id = device_id
        self.settings = settings
        self.db = db
        self.planner = planner
        self.cloud: RealtimeConnection | None = None
        self.session_id: int | None = None
        self.project_id: int | None = None
        self.state = DeviceState.IDLE
        self.output: OutputStream | None = None
        self.output_buffer = bytearray()
        self.playback_queue: asyncio.Queue[PlaybackPacket] = asyncio.Queue(
            maxsize=MAX_QUEUED_PLAYBACK_FRAMES
        )
        self.playback_task: asyncio.Task[None] | None = None
        self.input_queue: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=MAX_QUEUED_INPUT_FRAMES
        )
        self.input_task: asyncio.Task[None] | None = None
        self.start_task: asyncio.Task[None] | None = None
        self.accepting_audio = False
        self.cloud_ready = False
        self.input_dropped_frames = 0
        self.assistant_text = ""
        self.usage: dict[str, Any] = {}
        self.started_monotonic = 0.0
        self.last_activity = time.monotonic()
        self.timer_task: asyncio.Task[None] | None = None
        self.send_lock = asyncio.Lock()
        self.stopping = False
        self.cancelled_response_ids: set[str] = set()
        self.observer = observer
        self.diagnostic_input: BinaryIO | None = None
        self.diagnostic_output: BinaryIO | None = None
        self.input_frames_total = 0
        self.input_frames_interval = 0
        self.input_samples_interval = 0
        self.input_square_sum = 0
        self.input_peak = 0
        self.last_input_log = time.monotonic()
        self.echo_gate_active = False
        self.echo_gate_until = 0.0
        self.echo_suppressed_frames = 0

    async def send_json(self, message_type: str, **payload: Any) -> None:
        message = server_message(message_type, **{"device_id": self.device_id, **payload})
        async with self.send_lock:
            await self.websocket.send_json(message)
        if self.observer is not None:
            await self.observer(message)

    async def publish_json(self, message_type: str, **payload: Any) -> None:
        """Publish server state without writing to the device connection."""
        if self.observer is not None:
            await self.observer(
                server_message(message_type, **{"device_id": self.device_id, **payload})
            )

    async def send_optional(
        self, message_type: str, notify_device: bool, **payload: Any
    ) -> bool:
        """Send when connected, falling back to UI-only publication after disconnect."""
        if notify_device:
            try:
                await self.send_json(message_type, **payload)
                return True
            except (WebSocketDisconnect, RuntimeError):
                notify_device = False
        await self.publish_json(message_type, **payload)
        return notify_device

    async def send_bytes(self, data: bytes) -> None:
        async with self.send_lock:
            await self.websocket.send_bytes(data)

    async def set_state(self, state: DeviceState) -> None:
        self.state = state
        await self.send_json("state", state=state.value)

    async def request_start(self, requested_project_id: int | None) -> None:
        """Begin cloud startup without blocking the device WebSocket receive loop."""
        if self.cloud is not None or (
            self.start_task is not None and not self.start_task.done()
        ):
            await self.send_json(
                "session.active",
                session_id=self.session_id,
                project_id=self.project_id,
            )
            return
        self._clear_input_queue()
        self.input_dropped_frames = 0
        self.accepting_audio = True
        self.start_task = asyncio.create_task(
            self._run_start(requested_project_id),
            name=f"session-start-{self.device_id}",
        )

    async def _run_start(self, requested_project_id: int | None) -> None:
        try:
            await self.start(requested_project_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Session startup failed device=%s", self.device_id)
        finally:
            if self.start_task is asyncio.current_task():
                self.start_task = None

    async def start(self, requested_project_id: int | None) -> None:
        if self.cloud is not None:
            await self.send_json("session.active", session_id=self.session_id, project_id=self.project_id)
            return
        self.accepting_audio = True
        project = self.db.get_project(requested_project_id)
        self.project_id = project["id"]
        self.session_id = self.db.start_session(self.project_id, self.device_id, self.settings.realtime_model)
        logger.info(
            "Starting session device=%s session=%s project=%s model=%s",
            self.device_id,
            self.session_id,
            self.project_id,
            self.settings.realtime_model,
        )
        if self.settings.diagnostic_audio:
            diagnostic_dir = self.settings.database_path.parent / "diagnostic-audio"
            diagnostic_dir.mkdir(parents=True, exist_ok=True)
            self.diagnostic_input = (diagnostic_dir / f"session-{self.session_id}-input.pcm").open("wb")
            self.diagnostic_output = (diagnostic_dir / f"session-{self.session_id}-output.pcm").open("wb")
        context = build_instructions(project, self.db.recent_turns(self.project_id, 12))
        self.cloud = RealtimeConnection(self.settings, context, self.handle_openai_event)
        await self.set_state(DeviceState.CONNECTING)
        try:
            await self.cloud.connect()
        except Exception:
            self.db.end_session(self.session_id, "connect_error", {})
            for recording in (self.diagnostic_input, self.diagnostic_output):
                if recording is not None:
                    recording.close()
            self.diagnostic_input = self.diagnostic_output = None
            self.cloud = None
            self.accepting_audio = False
            self.cloud_ready = False
            self._clear_input_queue()
            await self.set_state(DeviceState.ERROR)
            raise
        self.started_monotonic = self.last_activity = time.monotonic()
        self.cloud_ready = True
        self._start_input_sender()
        self._start_playback_sender()
        self.timer_task = asyncio.create_task(self._watch_timeouts(), name=f"session-timer-{self.device_id}")
        await self.set_state(DeviceState.LISTENING)
        await self.send_json("session.started", session_id=self.session_id, project_id=self.project_id)
        logger.info("Session ready for device audio device=%s session=%s", self.device_id, self.session_id)

    async def receive_audio(self, pcm: bytes) -> None:
        if len(pcm) != FRAME_BYTES:
            await self.send_json(
                "error", code="bad_audio_frame", detail=f"expected {FRAME_BYTES} bytes, got {len(pcm)}"
            )
            return
        samples = memoryview(pcm).cast("h")
        frame_peak = max(abs(sample) for sample in samples)
        self.input_frames_total += 1
        self.input_frames_interval += 1
        self.input_samples_interval += len(samples)
        self.input_square_sum += sum(int(sample) * int(sample) for sample in samples)
        self.input_peak = max(self.input_peak, frame_peak)
        current = time.monotonic()
        if self.input_frames_total == 1 or current - self.last_input_log >= 2:
            rms = int((self.input_square_sum / max(1, self.input_samples_interval)) ** 0.5)
            logger.info(
                "Device audio device=%s total_frames=%d interval_frames=%d peak=%d rms=%d state=%s",
                self.device_id,
                self.input_frames_total,
                self.input_frames_interval,
                self.input_peak,
                rms,
                self.state.value,
            )
            self.input_frames_interval = 0
            self.input_samples_interval = 0
            self.input_square_sum = 0
            self.input_peak = 0
            self.last_input_log = current
        if self.diagnostic_input is not None:
            self.diagnostic_input.write(pcm)
        echo_guarded = not self.settings.barge_in_enabled and (
            self.output is not None or time.monotonic() < self.echo_gate_until
        )
        if echo_guarded:
            self.echo_suppressed_frames += 1
            if not self.echo_gate_active:
                self.echo_gate_active = True
                logger.info(
                    "Assistant echo guard active device=%s; microphone capture continues locally",
                    self.device_id,
                )
            return
        if self.echo_gate_active:
            logger.info(
                "Assistant echo guard released device=%s suppressed_frames=%d",
                self.device_id,
                self.echo_suppressed_frames,
            )
            self.echo_gate_active = False
            self.echo_suppressed_frames = 0
        if not self.accepting_audio:
            return
        if self.cloud_ready:
            self._start_input_sender()
        try:
            self.input_queue.put_nowait(pcm)
        except asyncio.QueueFull:
            # Preserve the most recent speech if OpenAI startup or the LAN stalls beyond
            # the five-second budget. Never backpressure the device receive loop.
            self.input_queue.get_nowait()
            self.input_queue.put_nowait(pcm)
            self.input_dropped_frames += 1
            if self.input_dropped_frames == 1 or self.input_dropped_frames % 50 == 0:
                logger.warning(
                    "OpenAI input queue full device=%s dropped_frames=%d",
                    self.device_id,
                    self.input_dropped_frames,
                )

    def _clear_input_queue(self) -> None:
        while True:
            try:
                self.input_queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    def _start_input_sender(self) -> None:
        if self.input_task is None or self.input_task.done():
            self.input_task = asyncio.create_task(
                self._input_sender(), name=f"input-sender-{self.device_id}"
            )

    async def _stop_input_sender(self) -> None:
        task, self.input_task = self.input_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._clear_input_queue()

    async def _input_sender(self) -> None:
        """Forward queued PCM without coupling device reads to OpenAI write latency."""
        try:
            while True:
                pcm = await self.input_queue.get()
                cloud = self.cloud
                if cloud is None or not self.cloud_ready:
                    continue
                await cloud.append_audio(pcm)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("OpenAI audio forwarding failed device=%s", self.device_id)
            await self.handle_openai_event(
                {"type": "gateway.transport_error", "error": str(exc)}
            )

    async def playback_progress(self, stream_id: str, played_ms: int) -> None:
        if self.output and self.output.stream_id == stream_id:
            self.output.played_ms = min(played_ms, self.output.sent_ms)
            if self.output.ended and self.output.played_ms >= self.output.sent_ms:
                await self._complete_playback("device_progress")

    async def interrupt(self) -> None:
        output = self.output
        if output is None:
            return
        self.cancelled_response_ids.add(output.response_id)
        await self.send_json("playback.flush", stream_id=output.stream_id)
        cloud = self.cloud
        if cloud is not None:
            # turn_detection.interrupt_response=true makes OpenAI cancel the active response.
            # Sending response.cancel here races that automatic cancellation.
            with contextlib.suppress(Exception):
                await cloud.truncate(output.item_id, output.content_index, output.played_ms)
        self.output = None
        self.output_buffer.clear()
        self._clear_playback_queue()
        await self.set_state(DeviceState.LISTENING)

    async def handle_openai_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type", "")
        if kind == "input_audio_buffer.speech_started":
            logger.info(
                "OpenAI VAD speech started device=%s audio_start_ms=%s item=%s",
                self.device_id,
                event.get("audio_start_ms"),
                event.get("item_id"),
            )
            if self.output is not None and not self.settings.barge_in_enabled:
                logger.info("Ignoring assistant-echo VAD start device=%s", self.device_id)
                return
            self.last_activity = time.monotonic()
            await self.interrupt()
            return
        if kind == "input_audio_buffer.speech_stopped":
            logger.info(
                "OpenAI VAD speech stopped device=%s audio_end_ms=%s item=%s",
                self.device_id,
                event.get("audio_end_ms"),
                event.get("item_id"),
            )
            await self.set_state(DeviceState.THINKING)
            return
        if kind == "error":
            error = event.get("error", event)
            if error.get("code") == "response_cancel_not_active":
                logger.info(
                    "Ignoring completed OpenAI cancellation race device=%s", self.device_id
                )
                return
            logger.error("OpenAI session error device=%s: %s", self.device_id, error)
            await self.set_state(DeviceState.ERROR)
            return
        if kind in {
            "conversation.item.input_audio_transcription.completed",
            "conversation.item.input_audio_transcription.done",
        }:
            if self.session_id is not None:
                transcript = event.get("transcript", "")
                self.db.add_turn(
                    self.session_id, "user", transcript, event.get("item_id")
                )
                await self.send_json("transcript.committed", role="user", text=transcript)
            return
        if kind in {"response.output_audio.delta", "response.audio.delta"}:
            await self._audio_delta(event)
            return
        if kind in {"response.output_audio_transcript.delta", "response.audio_transcript.delta"}:
            self.assistant_text += event.get("delta", "")
            await self.send_json("transcript.delta", role="assistant", text=event.get("delta", ""))
            return
        if kind in {"response.output_audio.done", "response.audio.done"}:
            await self._finish_audio_frame()
            return
        if kind == "response.done":
            response = event.get("response", {})
            # Some event sequences finish the response without a separate output_audio.done.
            # Queue the final partial frame/end marker exactly once in either case.
            await self._finish_audio_frame()
            self.usage = response.get("usage", self.usage)
            if self.session_id is not None and self.assistant_text:
                self.db.add_turn(
                    self.session_id,
                    "assistant",
                    self.assistant_text,
                    self.output.item_id if self.output else None,
                    response.get("status") == "cancelled",
                )
            self.assistant_text = ""
            self.last_activity = time.monotonic()
            if self.output is None:
                await self.set_state(DeviceState.LISTENING)
            return
        if kind == "response.function_call_arguments.done" and event.get("name") == "end_session":
            await self.stop("spoken_stop")
            return
        if kind in {"error", "gateway.transport_error"}:
            await self.send_json("error", code="openai", detail=event.get("error", event))
            await self.set_state(DeviceState.ERROR)

    async def _audio_delta(self, event: dict[str, Any]) -> None:
        response_id = event.get("response_id", "unknown-response")
        if response_id in self.cancelled_response_ids:
            return
        item_id = event.get("item_id", "unknown-item")
        content_index = int(event.get("content_index", 0))
        if self.output is None or self.output.response_id != response_id or self.output.item_id != item_id:
            stream_id = uuid.uuid4().hex
            self.output = OutputStream(stream_id, response_id, item_id, content_index)
            self.output_buffer.clear()
            await self.send_json(
                "playback.start",
                stream_id=stream_id,
                response_id=response_id,
                item_id=item_id,
                sample_rate=24000,
                format="pcm_s16le",
            )
            await self.set_state(DeviceState.SPEAKING)
        self.output_buffer.extend(base64.b64decode(event["delta"]))
        while len(self.output_buffer) >= FRAME_BYTES and self.output is not None:
            frame = bytes(self.output_buffer[:FRAME_BYTES])
            del self.output_buffer[:FRAME_BYTES]
            self._queue_playback(PlaybackPacket(self.output.stream_id, frame))

    async def _finish_audio_frame(self) -> None:
        if self.output is None or self.output.end_queued:
            return
        if self.output_buffer:
            self.output_buffer.extend(b"\x00" * (FRAME_BYTES - len(self.output_buffer)))
            self._queue_playback(
                PlaybackPacket(self.output.stream_id, bytes(self.output_buffer))
            )
            self.output_buffer.clear()
        self.output.end_queued = True
        self._queue_playback(PlaybackPacket(self.output.stream_id, None))

    def _queue_playback(self, packet: PlaybackPacket) -> None:
        try:
            self.playback_queue.put_nowait(packet)
        except asyncio.QueueFull as exc:
            raise RuntimeError("assistant playback exceeded the bounded 30-second queue") from exc

    def _clear_playback_queue(self) -> None:
        while True:
            try:
                self.playback_queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    def _start_playback_sender(self) -> None:
        if self.playback_task is None or self.playback_task.done():
            self.playback_task = asyncio.create_task(
                self._playback_sender(), name=f"playback-sender-{self.device_id}"
            )

    async def _stop_playback_sender(self) -> None:
        task, self.playback_task = self.playback_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._clear_playback_queue()

    async def _playback_sender(self) -> None:
        """Pace generated audio at its 20 ms media rate instead of bursting it at the device."""
        active_stream = ""
        next_send = 0.0
        loop = asyncio.get_running_loop()
        while True:
            packet = await self.playback_queue.get()
            output = self.output
            if output is None or output.stream_id != packet.stream_id:
                continue
            if packet.data is None:
                await self.send_json(
                    "playback.end", stream_id=output.stream_id, duration_ms=output.sent_ms
                )
                output.ended = True
                output.ended_monotonic = loop.time()
                logger.info(
                    "Assistant playback sent device=%s stream=%s duration_ms=%d played_ms=%d",
                    self.device_id,
                    output.stream_id,
                    output.sent_ms,
                    output.played_ms,
                )
                continue
            if active_stream != packet.stream_id:
                active_stream = packet.stream_id
                next_send = loop.time()
            delay = next_send - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            output = self.output
            if output is None or output.stream_id != packet.stream_id:
                continue
            await self.send_bytes(packet.data)
            if self.diagnostic_output is not None:
                self.diagnostic_output.write(packet.data)
            output.sent_ms += 20
            next_send = max(next_send, loop.time()) + PLAYBACK_FRAME_SECONDS

    async def stop(self, reason: str, notify_device: bool = True) -> None:
        if self.stopping or self.cloud is None:
            return
        self.stopping = True
        session_id, project_id = self.session_id, self.project_id
        try:
            self.accepting_audio = False
            self.cloud_ready = False
            await self._stop_input_sender()
            if self.output:
                notify_device = await self.send_optional(
                    "playback.flush", notify_device, stream_id=self.output.stream_id
                )
            await self._stop_playback_sender()
            cloud, self.cloud = self.cloud, None
            await cloud.close()
            if self.timer_task and self.timer_task is not asyncio.current_task():
                self.timer_task.cancel()
                await asyncio.gather(self.timer_task, return_exceptions=True)
            if session_id is not None:
                self.db.end_session(session_id, reason, self.usage)
            self.session_id = self.project_id = None
            self.output = None
            self.output_buffer.clear()
            for recording in (self.diagnostic_input, self.diagnostic_output):
                if recording is not None:
                    recording.close()
            self.diagnostic_input = self.diagnostic_output = None
            self.state = DeviceState.IDLE
            notify_device = await self.send_optional(
                "state", notify_device, state=DeviceState.IDLE.value
            )
            await self.send_optional("session.ended", notify_device, reason=reason)
        finally:
            self.stopping = False
            if session_id is not None and project_id is not None:
                asyncio.create_task(self.planner.update_after_session(project_id, session_id))

    async def _watch_timeouts(self) -> None:
        while self.cloud is not None:
            await asyncio.sleep(1)
            current = time.monotonic()
            if (
                self.output is not None
                and self.output.ended
                and current - self.output.ended_monotonic >= 2.0
            ):
                logger.warning(
                    "Playback completion timed out device=%s stream=%s sent_ms=%d played_ms=%d; recovering",
                    self.device_id,
                    self.output.stream_id,
                    self.output.sent_ms,
                    self.output.played_ms,
                )
                with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                    await self.send_json("playback.flush", stream_id=self.output.stream_id)
                await self._complete_playback("watchdog")
            if current - self.started_monotonic >= self.settings.hard_session_limit_seconds:
                await self.stop("hard_limit")
                return
            if (
                self.state == DeviceState.LISTENING
                and current - self.last_activity >= self.settings.idle_timeout_seconds
            ):
                await self.stop("idle_timeout")
                return

    async def _complete_playback(self, reason: str) -> None:
        output = self.output
        if output is None:
            return
        logger.info(
            "Assistant playback complete device=%s stream=%s reason=%s sent_ms=%d played_ms=%d",
            self.device_id,
            output.stream_id,
            reason,
            output.sent_ms,
            output.played_ms,
        )
        self.output = None
        if not self.settings.barge_in_enabled:
            self.echo_gate_until = time.monotonic() + 0.3
        self.last_activity = time.monotonic()
        await self.set_state(DeviceState.LISTENING)

    async def close(self) -> None:
        self.accepting_audio = False
        start_task, self.start_task = self.start_task, None
        if start_task is not None and start_task is not asyncio.current_task():
            start_task.cancel()
            await asyncio.gather(start_task, return_exceptions=True)
        if self.cloud is not None:
            await self.stop("device_disconnect", notify_device=False)
        else:
            await self._stop_input_sender()
