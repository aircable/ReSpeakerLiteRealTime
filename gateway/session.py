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


@dataclass
class OutputStream:
    stream_id: str
    response_id: str
    item_id: str
    content_index: int
    played_ms: int = 0
    sent_ms: int = 0
    ended: bool = False


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

    async def start(self, requested_project_id: int | None) -> None:
        if self.cloud is not None:
            await self.send_json("session.active", session_id=self.session_id, project_id=self.project_id)
            return
        project = self.db.get_project(requested_project_id)
        self.project_id = project["id"]
        self.session_id = self.db.start_session(self.project_id, self.device_id, self.settings.realtime_model)
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
            await self.set_state(DeviceState.ERROR)
            raise
        self.started_monotonic = self.last_activity = time.monotonic()
        self.timer_task = asyncio.create_task(self._watch_timeouts(), name=f"session-timer-{self.device_id}")
        await self.set_state(DeviceState.LISTENING)
        await self.send_json("session.started", session_id=self.session_id, project_id=self.project_id)

    async def receive_audio(self, pcm: bytes) -> None:
        if self.cloud is None:
            return
        if len(pcm) != FRAME_BYTES:
            await self.send_json(
                "error", code="bad_audio_frame", detail=f"expected {FRAME_BYTES} bytes, got {len(pcm)}"
            )
            return
        self.last_activity = time.monotonic()
        if self.diagnostic_input is not None:
            self.diagnostic_input.write(pcm)
        await self.cloud.append_audio(pcm)

    async def playback_progress(self, stream_id: str, played_ms: int) -> None:
        if self.output and self.output.stream_id == stream_id:
            self.output.played_ms = min(played_ms, self.output.sent_ms)
            if self.output.ended and self.output.played_ms >= self.output.sent_ms:
                self.output = None
                self.last_activity = time.monotonic()
                await self.set_state(DeviceState.LISTENING)

    async def interrupt(self) -> None:
        output = self.output
        if output is None:
            return
        self.cancelled_response_ids.add(output.response_id)
        await self.send_json("playback.flush", stream_id=output.stream_id)
        cloud = self.cloud
        if cloud is not None:
            with contextlib.suppress(Exception):
                await cloud.cancel_response()
            with contextlib.suppress(Exception):
                await cloud.truncate(output.item_id, output.content_index, output.played_ms)
        self.output = None
        self.output_buffer.clear()
        await self.set_state(DeviceState.LISTENING)

    async def handle_openai_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type", "")
        if kind == "input_audio_buffer.speech_started":
            self.last_activity = time.monotonic()
            await self.interrupt()
            return
        if kind == "input_audio_buffer.speech_stopped":
            await self.set_state(DeviceState.THINKING)
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
            await self.send_bytes(frame)
            if self.diagnostic_output is not None:
                self.diagnostic_output.write(frame)
            self.output.sent_ms += 20

    async def _finish_audio_frame(self) -> None:
        if self.output is None:
            return
        if self.output_buffer:
            self.output_buffer.extend(b"\x00" * (FRAME_BYTES - len(self.output_buffer)))
            await self.send_bytes(bytes(self.output_buffer))
            if self.diagnostic_output is not None:
                self.diagnostic_output.write(self.output_buffer)
            self.output.sent_ms += 20
            self.output_buffer.clear()
        await self.send_json(
            "playback.end", stream_id=self.output.stream_id, duration_ms=self.output.sent_ms
        )
        self.output.ended = True

    async def stop(self, reason: str, notify_device: bool = True) -> None:
        if self.stopping or self.cloud is None:
            return
        self.stopping = True
        session_id, project_id = self.session_id, self.project_id
        try:
            if self.output:
                notify_device = await self.send_optional(
                    "playback.flush", notify_device, stream_id=self.output.stream_id
                )
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
            if current - self.started_monotonic >= self.settings.hard_session_limit_seconds:
                await self.stop("hard_limit")
                return
            if (
                self.state == DeviceState.LISTENING
                and current - self.last_activity >= self.settings.idle_timeout_seconds
            ):
                await self.stop("idle_timeout")
                return

    async def close(self) -> None:
        if self.cloud is not None:
            await self.stop("device_disconnect", notify_device=False)
