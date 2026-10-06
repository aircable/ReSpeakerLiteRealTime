import asyncio
import base64
import contextlib
import json
import logging
import math
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, BinaryIO

from fastapi import WebSocket, WebSocketDisconnect

from .config import Settings
from .context import build_instructions
from .db import Database
from .planner import Planner
from .protocol import FRAME_BYTES, DeviceState, server_message
from .realtime import RealtimeConnection
from .voice_commands import COMMANDS_BY_NAME
from .web_search import WebSearchError, search_web

logger = logging.getLogger(__name__)
PLAYBACK_FRAME_SECONDS = 0.020
PLAYBACK_COMPLETION_TOLERANCE_MS = round(PLAYBACK_FRAME_SECONDS * 1000)
MAX_DEVICE_PLAYBACK_LEAD_MS = 200
MAX_QUEUED_INPUT_FRAMES = 250  # Five seconds of 20 ms startup/jitter buffering.
PLAYBACK_FRAMES_PER_SECOND = round(1 / PLAYBACK_FRAME_SECONDS)
BARGE_IN_WARMUP_MS = 1000  # Gate speaker onset using reported DAC playback, not generation time.
BARGE_IN_LEVEL_WINDOW_FRAMES = 20  # 400 ms of 20 ms microphone frames.
LISTENING_LEVEL_WINDOW_FRAMES = 8  # 160 ms; a click must not start a turn.
LISTENING_LEVEL_MIN_ACTIVE_FRAMES = 6  # At least 120 ms above the configured RMS.
LISTENING_LEVEL_RELEASE_FRAMES = 50  # Return to gated listening after 1 s quiet.
SILENT_INPUT_FRAME = bytes(FRAME_BYTES)


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
        capabilities: dict[str, Any] | None = None,
        device_name: str | None = None,
    ):
        self.websocket = websocket
        self.device_id = device_id
        self.device_name = device_name or device_id
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
            maxsize=settings.playback_buffer_seconds * PLAYBACK_FRAMES_PER_SECOND
        )
        self.playback_task: asyncio.Task[None] | None = None
        self.playback_progress_event = asyncio.Event()
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
        self.warmup_vad_items: set[str] = set()
        self.barge_gate_stream_id: str | None = None
        self.barge_gate_frames: deque[tuple[bytes, int]] = deque()
        self.barge_gate_square_sum = 0
        self.barge_gate_max_full_rms = 0
        self.barge_gate_max_partial_rms = 0
        self.barge_gate_full_windows = 0
        self.barge_gate_last_played_ms = 0
        self.barge_gate_open = False
        self.listening_gate_frames: deque[tuple[bytes, int]] = deque()
        self.listening_gate_active_frames = 0
        self.listening_gate_open = False
        self.listening_gate_quiet_frames = 0
        self.listening_gate_max_frame_rms = 0
        self.listening_gate_max_decision_rms = 0
        self.listening_gate_max_active_frames = 0
        self.listening_gate_full_windows = 0
        self.announcement_echo_guard = False
        self.device_volume: float | None = None
        self.ready_keyword_enabled = bool((capabilities or {}).get("ready_keyword"))
        self.xvf_agc_ch0_supported = bool((capabilities or {}).get("xvf_agc_ch0"))
        self.ready_wait_requested = False
        self.waiting_for_ready = False
        self.pending_ready_call_id: str | None = None
        self.search_tasks: set[asyncio.Task[None]] = set()

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

    async def report_volume(self, level: float) -> None:
        self.device_volume = max(0.0, min(1.0, level))
        await self.publish_json(
            "volume.changed",
            level=self.device_volume,
            level_percent=round(self.device_volume * 100),
        )

    async def request_xvf_gain(self, gain: float) -> None:
        if not self.xvf_agc_ch0_supported:
            return
        await self.send_json("mic_gain.set", ch0_gain=gain)
        logger.info(
            "XMOS channel-0 AGC requested device=%s gain=%.1f (0=adaptive)",
            self.device_id,
            gain,
        )

    async def control_volume(
        self,
        action: str,
        level_percent: float | None = None,
        change_percent: float | None = None,
    ) -> dict[str, Any]:
        if self.device_volume is None:
            return {"ok": False, "error": "The device has not reported its volume yet."}
        current_percent = self.device_volume * 100.0
        if action == "get":
            target_percent = current_percent
        elif action == "set":
            if level_percent is None:
                return {"ok": False, "error": "An exact percentage is required."}
            target_percent = level_percent
        elif action in {"increase", "decrease"}:
            step = 5.0 if change_percent is None else change_percent
            target_percent = current_percent + (step if action == "increase" else -step)
        else:
            return {"ok": False, "error": "Unknown volume action."}
        target_percent = max(0.0, min(100.0, target_percent))
        if action != "get":
            self.device_volume = target_percent / 100.0
            await self.send_json("volume.set", level=self.device_volume)
            logger.info(
                "Device volume requested device=%s level=%.1f%% source=%s",
                self.device_id,
                target_percent,
                action,
            )
        return {"ok": True, "level_percent": round(target_percent)}

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
        # The device WebSocket is persistent, so reload UI overrides for every
        # billed voice session rather than only when the device authenticates.
        self.settings = self.settings.model_copy(update=self.db.setting_overrides())
        self.planner.settings = self.settings
        self.accepting_audio = True
        self.warmup_vad_items.clear()
        self._reset_barge_in_level_gate()
        self._reset_listening_level_gate()
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
            input_path = diagnostic_dir / f"session-{self.session_id}-input.pcm"
            output_path = diagnostic_dir / f"session-{self.session_id}-output.pcm"
            try:
                diagnostic_dir.mkdir(parents=True, exist_ok=True)
                self.diagnostic_input = input_path.open("wb")
                self.diagnostic_output = output_path.open("wb")
                logger.info(
                    "Diagnostic audio recording enabled device=%s session=%s input=%s output=%s",
                    self.device_id,
                    self.session_id,
                    input_path,
                    output_path,
                )
            except OSError:
                logger.exception(
                    "Diagnostic audio recording unavailable device=%s session=%s directory=%s",
                    self.device_id,
                    self.session_id,
                    diagnostic_dir,
                )
                for recording in (self.diagnostic_input, self.diagnostic_output):
                    if recording is not None:
                        recording.close()
                self.diagnostic_input = self.diagnostic_output = None
        context = build_instructions(
            project,
            self.db.recent_turns(self.project_id, 12),
            ready_keyword_enabled=self.ready_keyword_enabled,
        )
        self.cloud = RealtimeConnection(
            self.settings,
            context,
            self.handle_openai_event,
            ready_keyword_enabled=self.ready_keyword_enabled,
        )
        await self.set_state(DeviceState.CONNECTING)
        try:
            await self.request_xvf_gain(self.settings.xvf_agc_ch0_gain)
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
        self.announcement_echo_guard = self.settings.announce_active_project
        if self.announcement_echo_guard:
            self._clear_input_queue()
        self.cloud_ready = True
        self._start_input_sender()
        self._start_playback_sender()
        self.timer_task = asyncio.create_task(self._watch_timeouts(), name=f"session-timer-{self.device_id}")
        await self.set_state(DeviceState.LISTENING)
        await self.send_json("session.started", session_id=self.session_id, project_id=self.project_id)
        if self.ready_keyword_enabled:
            await self.send_json("keyword.mode", mode="wake")
        logger.info("Session ready for device audio device=%s session=%s", self.device_id, self.session_id)
        if self.settings.announce_active_project:
            await self._announce_project(project["name"])

    async def receive_audio(self, pcm: bytes) -> None:
        if len(pcm) != FRAME_BYTES:
            await self.send_json(
                "error", code="bad_audio_frame", detail=f"expected {FRAME_BYTES} bytes, got {len(pcm)}"
            )
            return
        samples = memoryview(pcm).cast("h")
        frame_peak = max(abs(sample) for sample in samples)
        frame_square_sum = sum(int(sample) * int(sample) for sample in samples)
        output = self.output
        if output is None:
            if self.barge_gate_stream_id is not None:
                self._reset_barge_in_level_gate()
        else:
            if self.barge_gate_stream_id != output.stream_id:
                self._reset_barge_in_level_gate()
                self.barge_gate_stream_id = output.stream_id
            self._reset_listening_level_gate()
        self.input_frames_total += 1
        self.input_frames_interval += 1
        self.input_samples_interval += len(samples)
        self.input_square_sum += frame_square_sum
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
            if (
                output is not None
                and self.settings.barge_in_enabled
                and self.settings.barge_in_rms_threshold > 0
                and not self.barge_gate_open
                and self.barge_gate_frames
            ):
                self._log_barge_in_level_guard("periodic")
            if (
                output is None
                and self.settings.listening_rms_threshold > 0
                and not self.listening_gate_open
            ):
                self._log_listening_level_guard("periodic")
        if self.diagnostic_input is not None:
            self.diagnostic_input.write(pcm)
        warmup_guarded = self._barge_in_warmup_active()
        fully_guarded = (
            self.announcement_echo_guard
            or time.monotonic() < self.echo_gate_until
            or (not self.settings.barge_in_enabled and output is not None)
        )
        pre_roll: list[bytes] | None = None
        level_guarded = False
        if (
            not fully_guarded
            and not warmup_guarded
            and output is not None
            and self.settings.barge_in_enabled
            and self.settings.barge_in_rms_threshold > 0
            and not self.barge_gate_open
        ):
            pre_roll = self._check_barge_in_level(pcm, frame_square_sum)
            level_guarded = pre_roll is None
        if fully_guarded or warmup_guarded or level_guarded:
            self.echo_suppressed_frames += 1
            if not self.echo_gate_active:
                self.echo_gate_active = True
                if fully_guarded:
                    guard_reason = "playback"
                elif warmup_guarded:
                    guard_reason = "barge_in_warmup"
                else:
                    guard_reason = "barge_in_level"
                logger.info(
                    "Assistant echo guard active device=%s reason=%s played_ms=%s; microphone capture continues locally",
                    self.device_id,
                    guard_reason,
                    output.played_ms if output is not None else "-",
                )
            if fully_guarded:
                return
            # Keep the OpenAI input timeline moving without forwarding speaker
            # pickup until the physical-playback and level gates both release.
            pcm = SILENT_INPUT_FRAME
        elif self.echo_gate_active:
            logger.info(
                "Assistant echo guard released device=%s suppressed_frames=%d played_ms=%s",
                self.device_id,
                self.echo_suppressed_frames,
                self.output.played_ms if self.output is not None else "-",
            )
            self.echo_gate_active = False
            self.echo_suppressed_frames = 0
        if not self.accepting_audio:
            return
        if (
            output is None
            and not fully_guarded
            and self.settings.listening_rms_threshold > 0
        ):
            listening_frames = self._check_listening_level(pcm, frame_square_sum)
            if listening_frames is None:
                pcm = SILENT_INPUT_FRAME
            else:
                pre_roll = listening_frames
        if self.cloud_ready:
            self._start_input_sender()
        for input_frame in pre_roll if pre_roll is not None else (pcm,):
            try:
                self.input_queue.put_nowait(input_frame)
            except asyncio.QueueFull:
                # Preserve the most recent speech if OpenAI startup or the LAN stalls beyond
                # the five-second budget. Never backpressure the device receive loop.
                self.input_queue.get_nowait()
                self.input_queue.put_nowait(input_frame)
                self.input_dropped_frames += 1
                if self.input_dropped_frames == 1 or self.input_dropped_frames % 50 == 0:
                    logger.warning(
                        "OpenAI input queue full device=%s dropped_frames=%d",
                        self.device_id,
                        self.input_dropped_frames,
                    )

    def _barge_in_warmup_active(self) -> bool:
        return (
            self.settings.barge_in_enabled
            and self.output is not None
            and self.output.played_ms < BARGE_IN_WARMUP_MS
        )

    def _reset_barge_in_level_gate(self) -> None:
        if not self.barge_gate_open:
            self._log_barge_in_level_guard("stream_end")
        self.barge_gate_stream_id = None
        self.barge_gate_frames.clear()
        self.barge_gate_square_sum = 0
        self.barge_gate_max_full_rms = 0
        self.barge_gate_max_partial_rms = 0
        self.barge_gate_full_windows = 0
        self.barge_gate_last_played_ms = 0
        self.barge_gate_open = False

    def _log_barge_in_level_guard(self, reason: str) -> None:
        if not self.barge_gate_full_windows and not self.barge_gate_max_partial_rms:
            return
        threshold = self.settings.barge_in_rms_threshold
        full_rms = (
            str(self.barge_gate_max_full_rms) if self.barge_gate_full_windows else "n/a"
        )
        margin = (
            f"{self.barge_gate_max_full_rms - threshold:+d}"
            if self.barge_gate_full_windows else "n/a"
        )
        logger.info(
            "Barge-in level guard device=%s stream=%s max_full_window_rms=%s "
            "threshold=%d margin=%s full_windows=%d window_ms=%d "
            "max_partial_rms=%d played_ms=%d reason=%s",
            self.device_id, self.barge_gate_stream_id, full_rms, threshold, margin,
            self.barge_gate_full_windows, BARGE_IN_LEVEL_WINDOW_FRAMES * 20,
            self.barge_gate_max_partial_rms, self.barge_gate_last_played_ms, reason,
        )
        self.barge_gate_max_full_rms = 0
        self.barge_gate_max_partial_rms = 0
        self.barge_gate_full_windows = 0

    def _check_barge_in_level(self, pcm: bytes, frame_square_sum: int) -> list[bytes] | None:
        if len(self.barge_gate_frames) == BARGE_IN_LEVEL_WINDOW_FRAMES:
            _, old_square_sum = self.barge_gate_frames.popleft()
            self.barge_gate_square_sum -= old_square_sum
        self.barge_gate_frames.append((pcm, frame_square_sum))
        self.barge_gate_square_sum += frame_square_sum
        sample_count = len(self.barge_gate_frames) * (FRAME_BYTES // 2)
        rolling_rms = math.isqrt(self.barge_gate_square_sum // sample_count)
        self.barge_gate_last_played_ms = self.output.played_ms if self.output is not None else 0
        if len(self.barge_gate_frames) < BARGE_IN_LEVEL_WINDOW_FRAMES:
            self.barge_gate_max_partial_rms = max(self.barge_gate_max_partial_rms, rolling_rms)
            return None
        self.barge_gate_full_windows += 1
        self.barge_gate_max_full_rms = max(self.barge_gate_max_full_rms, rolling_rms)
        threshold = self.settings.barge_in_rms_threshold
        if self.barge_gate_square_sum < threshold * threshold * sample_count:
            return None
        pre_roll = [frame for frame, _ in self.barge_gate_frames]
        self.barge_gate_frames.clear()
        self.barge_gate_square_sum = 0
        self.barge_gate_open = True
        logger.info(
            "Barge-in level qualified device=%s stream=%s rolling_rms=%d "
            "threshold=%d margin=%+d window_ms=%d pre_roll_ms=%d played_ms=%d",
            self.device_id,
            self.barge_gate_stream_id,
            rolling_rms,
            threshold,
            rolling_rms - threshold,
            BARGE_IN_LEVEL_WINDOW_FRAMES * 20,
            len(pre_roll) * 20,
            self.output.played_ms if self.output is not None else 0,
        )
        self.barge_gate_max_full_rms = 0
        self.barge_gate_max_partial_rms = 0
        self.barge_gate_full_windows = 0
        return pre_roll

    def _reset_listening_level_gate(self) -> None:
        if not self.listening_gate_open:
            self._log_listening_level_guard("gate_reset")
        self.listening_gate_frames.clear()
        self.listening_gate_active_frames = 0
        self.listening_gate_open = False
        self.listening_gate_quiet_frames = 0
        self.listening_gate_max_frame_rms = 0
        self.listening_gate_max_decision_rms = 0
        self.listening_gate_max_active_frames = 0
        self.listening_gate_full_windows = 0

    def _log_listening_level_guard(self, reason: str) -> None:
        if not self.listening_gate_full_windows and not self.listening_gate_max_frame_rms:
            return
        threshold = self.settings.listening_rms_threshold
        decision_rms = (
            str(self.listening_gate_max_decision_rms)
            if self.listening_gate_full_windows else "n/a"
        )
        margin = (
            f"{self.listening_gate_max_decision_rms - threshold:+d}"
            if self.listening_gate_full_windows else "n/a"
        )
        logger.info(
            "Listening level guard device=%s max_decision_rms=%s threshold=%d "
            "margin=%s max_active_frames=%d required_active_frames=%d "
            "window_ms=%d max_frame_rms=%d full_windows=%d reason=%s",
            self.device_id, decision_rms, threshold, margin,
            self.listening_gate_max_active_frames, LISTENING_LEVEL_MIN_ACTIVE_FRAMES,
            LISTENING_LEVEL_WINDOW_FRAMES * 20, self.listening_gate_max_frame_rms,
            self.listening_gate_full_windows, reason,
        )
        self.listening_gate_max_frame_rms = 0
        self.listening_gate_max_decision_rms = 0
        self.listening_gate_max_active_frames = 0
        self.listening_gate_full_windows = 0

    def _check_listening_level(self, pcm: bytes, frame_square_sum: int) -> list[bytes] | None:
        frame_rms = math.isqrt(frame_square_sum // (FRAME_BYTES // 2))
        threshold = self.settings.listening_rms_threshold
        if self.listening_gate_open:
            self.listening_gate_quiet_frames = (
                0 if frame_rms >= threshold else self.listening_gate_quiet_frames + 1
            )
            if self.listening_gate_quiet_frames >= LISTENING_LEVEL_RELEASE_FRAMES:
                self._reset_listening_level_gate()
                return None
            return [pcm]
        self.listening_gate_max_frame_rms = max(self.listening_gate_max_frame_rms, frame_rms)
        if len(self.listening_gate_frames) == LISTENING_LEVEL_WINDOW_FRAMES:
            _, old_rms = self.listening_gate_frames.popleft()
            self.listening_gate_active_frames -= int(old_rms >= threshold)
        self.listening_gate_frames.append((pcm, frame_rms))
        self.listening_gate_active_frames += int(frame_rms >= threshold)
        if len(self.listening_gate_frames) < LISTENING_LEVEL_WINDOW_FRAMES:
            return None
        # The sixth-highest frame level is the threshold-independent value
        # which decides this six-of-eight gate; a lone click cannot inflate it.
        decision_rms = sorted((level for _, level in self.listening_gate_frames), reverse=True)[
            LISTENING_LEVEL_MIN_ACTIVE_FRAMES - 1
        ]
        self.listening_gate_full_windows += 1
        self.listening_gate_max_decision_rms = max(self.listening_gate_max_decision_rms, decision_rms)
        self.listening_gate_max_active_frames = max(
            self.listening_gate_max_active_frames, self.listening_gate_active_frames
        )
        if self.listening_gate_active_frames < LISTENING_LEVEL_MIN_ACTIVE_FRAMES:
            return None
        pre_roll = [frame for frame, _ in self.listening_gate_frames]
        active_frames = self.listening_gate_active_frames
        self.listening_gate_frames.clear()
        self.listening_gate_active_frames = 0
        self.listening_gate_open = True
        self.listening_gate_quiet_frames = 0
        logger.info(
            "Listening level qualified device=%s decision_rms=%d threshold=%d "
            "margin=%+d active_frames=%d required_active_frames=%d window_ms=%d pre_roll_ms=%d",
            self.device_id,
            decision_rms,
            threshold,
            decision_rms - threshold,
            active_frames,
            LISTENING_LEVEL_MIN_ACTIVE_FRAMES,
            LISTENING_LEVEL_WINDOW_FRAMES * 20,
            len(pre_roll) * 20,
        )
        return pre_roll

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
            self.playback_progress_event.set()
            if (
                self.output.ended
                and self.output.played_ms + PLAYBACK_COMPLETION_TOLERANCE_MS
                >= self.output.sent_ms
            ):
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
        self._reset_barge_in_level_gate()
        self._reset_listening_level_gate()
        # The server has already recognized this barge-in; do not hide the
        # remainder of the same utterance behind the idle listening gate.
        self.listening_gate_open = True
        self.playback_progress_event.set()
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
            if self.announcement_echo_guard:
                logger.info("Ignoring project-announcement echo VAD start device=%s", self.device_id)
                return
            if self._barge_in_warmup_active():
                item_id = event.get("item_id")
                if item_id:
                    self.warmup_vad_items.add(item_id)
                logger.info(
                    "Ignoring VAD start during barge-in warmup device=%s played_ms=%d",
                    self.device_id,
                    self.output.played_ms,
                )
                return
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
            if event.get("item_id") in self.warmup_vad_items:
                self.warmup_vad_items.discard(event["item_id"])
                logger.info("Ignoring VAD stop from barge-in warmup device=%s", self.device_id)
                return
            self._reset_listening_level_gate()
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
                await self.send_json(
                    "transcript.committed", role="user", text=transcript,
                    project_id=self.project_id,
                )
            return
        if kind in {"response.output_audio.delta", "response.audio.delta"}:
            await self._audio_delta(event)
            return
        if kind in {"response.output_audio_transcript.delta", "response.audio_transcript.delta"}:
            self.assistant_text += event.get("delta", "")
            await self.send_json(
                "transcript.delta", role="assistant", text=event.get("delta", ""),
                project_id=self.project_id,
            )
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
                if self.announcement_echo_guard:
                    self.announcement_echo_guard = False
                    self.echo_gate_until = time.monotonic() + 0.3
                await self.set_state(DeviceState.LISTENING)
                await self._enter_ready_wait_if_possible()
            return
        if kind == "response.function_call_arguments.done":
            await self._handle_tool_call(event)
            return
        if kind in {"error", "gateway.transport_error"}:
            await self.send_json("error", code="openai", detail=event.get("error", event))
            await self.set_state(DeviceState.ERROR)

    async def _handle_tool_call(self, event: dict[str, Any]) -> None:
        name = event.get("name")
        command = COMMANDS_BY_NAME.get(name) if isinstance(name, str) else None
        cloud = self.cloud
        call_id = event.get("call_id")
        if command is None:
            logger.warning("Ignoring unknown voice tool device=%s name=%s", self.device_id, name)
            if cloud is not None and call_id:
                await cloud.submit_tool_output(call_id, {"ok": False, "error": "Unknown command."})
                await cloud.request_response()
            return
        if command.call_id_required and (cloud is None or not call_id):
            return
        if not command.available({"ready_keyword": self.ready_keyword_enabled}):
            if cloud is not None and call_id:
                await cloud.submit_tool_output(
                    call_id,
                    command.unavailable_result
                    or {"ok": False, "error": "This device does not support that command."},
                )
                await cloud.request_response()
            return
        try:
            arguments = json.loads(event.get("arguments") or "{}")
        except (TypeError, json.JSONDecodeError):
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        logger.info("Voice command requested device=%s command=%s", self.device_id, command.name)
        await getattr(self, command.handler)(cloud, call_id, arguments)

    async def _voice_end_session(
        self, _cloud: RealtimeConnection | None, _call_id: str | None, _arguments: dict[str, Any]
    ) -> None:
        await self.stop("spoken_stop")

    async def _voice_wait_for_ready(
        self, cloud: RealtimeConnection, call_id: str, _arguments: dict[str, Any]
    ) -> None:
        self.pending_ready_call_id = call_id
        self.ready_wait_requested = True
        logger.info(
            "Ready-word wait requested device=%s session=%s",
            self.device_id,
            self.session_id,
        )
        await self._enter_ready_wait_if_possible()

    async def _voice_list_projects(
        self, cloud: RealtimeConnection, call_id: str, _arguments: dict[str, Any]
    ) -> None:
        projects = self.db.list_projects()
        await cloud.submit_tool_output(
            call_id,
            {
                "active_project": next(
                    (project["name"] for project in projects if project["active"]), None
                ),
                "projects": [project["name"] for project in projects],
            },
        )
        await cloud.request_response()

    async def _voice_control_volume(
        self, cloud: RealtimeConnection, call_id: str, arguments: dict[str, Any]
    ) -> None:
        result = await self.control_volume(
            str(arguments.get("action") or ""),
            arguments.get("level_percent"),
            arguments.get("change_percent"),
        )
        await cloud.submit_tool_output(call_id, result)
        await cloud.request_response()

    async def _voice_search_web(
        self, cloud: RealtimeConnection, call_id: str, arguments: dict[str, Any]
    ) -> None:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            await cloud.submit_tool_output(call_id, {"ok": False, "error": "A search query is required."})
            await cloud.request_response()
            return
        if self.search_tasks:
            await cloud.submit_tool_output(call_id, {"ok": False, "error": "A web search is already running."})
            await cloud.request_response()
            return
        task = asyncio.create_task(
            self._run_web_search(cloud, call_id, query.strip()[:500], self.session_id, self.project_id),
            name=f"web-search-{self.device_id}",
        )
        self.search_tasks.add(task)
        task.add_done_callback(self.search_tasks.discard)

    async def _run_web_search(
        self, cloud: RealtimeConnection, call_id: str, query: str,
        session_id: int | None, project_id: int | None,
    ) -> None:
        logger.info("Web search started device=%s session=%s", self.device_id, session_id)
        try:
            result = await search_web(self.settings, query)
            output = {"ok": True, "answer": result["answer"], "sources": [
                {"title": citation["title"], "url": citation["url"]}
                for citation in result["citations"]
            ]}
        except asyncio.CancelledError:
            raise
        except WebSearchError as exc:
            result = None
            output = {"ok": False, "error": str(exc)}
        except Exception:
            logger.exception("Web search failed device=%s session=%s", self.device_id, session_id)
            result = None
            output = {"ok": False, "error": "Web search failed. Please try again."}
        if self.cloud is not cloud or self.session_id != session_id or self.stopping:
            return
        if result is not None:
            await self.publish_json(
                "search.result", project_id=project_id, query=query,
                answer=result["answer"], citations=result["citations"],
            )
        try:
            await cloud.submit_tool_output(call_id, output)
            await cloud.request_response()
        except Exception:
            logger.exception("Web search result delivery failed device=%s session=%s", self.device_id, session_id)
            return
        logger.info("Web search finished device=%s session=%s ok=%s", self.device_id, session_id, output["ok"])

    async def _cancel_search_tasks(self) -> None:
        tasks = tuple(self.search_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.search_tasks.clear()

    async def _voice_switch_project(
        self, cloud: RealtimeConnection, call_id: str, arguments: dict[str, Any]
    ) -> None:
        requested_name = str(arguments.get("project_name") or "").strip()
        project = self.db.find_project(requested_name)
        if project is None:
            await cloud.submit_tool_output(
                call_id,
                {
                    "ok": False,
                    "error": "No unique project matched that name.",
                    "projects": [p["name"] for p in self.db.list_projects()],
                },
            )
            await cloud.request_response()
            return
        if project["id"] == self.project_id:
            self.db.activate_project(project["id"])
            await cloud.submit_tool_output(
                call_id, {"ok": True, "active_project": project["name"], "already_active": True}
            )
            await cloud.request_response()
            return
        await self._switch_project(project)

    async def _enter_ready_wait_if_possible(self) -> None:
        if (
            not self.ready_wait_requested
            or self.waiting_for_ready
            or self.pending_ready_call_id is None
            or self.output is not None
        ):
            return
        self.ready_wait_requested = False
        self.waiting_for_ready = True
        self.accepting_audio = False
        self._clear_input_queue()
        self.last_activity = time.monotonic()
        await self.set_state(DeviceState.LISTENING)
        await self.send_json("keyword.mode", mode="ready")
        logger.info(
            "Waiting for local ready word device=%s session=%s",
            self.device_id,
            self.session_id,
        )

    async def ready_detected(self) -> None:
        if not self.waiting_for_ready or self.pending_ready_call_id is None:
            logger.info(
                "Ignoring unexpected ready word device=%s session=%s",
                self.device_id,
                self.session_id,
            )
            return
        call_id = self.pending_ready_call_id
        self.pending_ready_call_id = None
        self.waiting_for_ready = False
        self.ready_wait_requested = False
        self.accepting_audio = True
        self.last_activity = time.monotonic()
        await self.send_json("keyword.mode", mode="wake")
        cloud = self.cloud
        if cloud is None:
            return
        logger.info(
            "Local ready word detected device=%s session=%s",
            self.device_id,
            self.session_id,
        )
        await cloud.submit_tool_output(call_id, {"ready": True})
        await cloud.request_response()

    async def _announce_project(self, project_name: str) -> None:
        cloud = self.cloud
        if cloud is not None:
            # The project announcement is generated immediately after wake/session setup.
            # It must not be allowed to barge into itself, even when conversational
            # responses have barge-in enabled. Keep microphone capture running locally,
            # but withhold it from OpenAI through playback and its short acoustic tail.
            self.announcement_echo_guard = True
            self._clear_input_queue()
            try:
                await cloud.request_response(
                    f"Say only: {project_name} is active. Then stop speaking and wait. "
                    "Do not ask a question or suggest activities."
                )
            except Exception:
                self.announcement_echo_guard = False
                raise

    def _open_switched_session_recordings(self) -> None:
        if not self.settings.diagnostic_audio or self.session_id is None:
            return
        diagnostic_dir = self.settings.database_path.parent / "diagnostic-audio"
        input_path = diagnostic_dir / f"session-{self.session_id}-input.pcm"
        output_path = diagnostic_dir / f"session-{self.session_id}-output.pcm"
        try:
            diagnostic_dir.mkdir(parents=True, exist_ok=True)
            self.diagnostic_input = input_path.open("wb")
            self.diagnostic_output = output_path.open("wb")
            logger.info(
                "Diagnostic audio recording enabled device=%s session=%s input=%s output=%s",
                self.device_id,
                self.session_id,
                input_path,
                output_path,
            )
        except OSError:
            logger.exception(
                "Diagnostic audio recording unavailable device=%s session=%s directory=%s",
                self.device_id,
                self.session_id,
                diagnostic_dir,
            )
            self.diagnostic_input = self.diagnostic_output = None

    async def _switch_project(self, project: dict[str, Any]) -> None:
        """Open a clean cloud and transcript context without dropping the device socket."""
        old_cloud = self.cloud
        old_session_id, old_project_id = self.session_id, self.project_id
        logger.info(
            "Switching project device=%s from_project=%s to_project=%s",
            self.device_id,
            old_project_id,
            project["id"],
        )
        self.accepting_audio = False
        await self._cancel_search_tasks()
        self.cloud_ready = False
        self.ready_wait_requested = False
        self.waiting_for_ready = False
        self.pending_ready_call_id = None
        self.warmup_vad_items.clear()
        if self.ready_keyword_enabled:
            await self.send_json("keyword.mode", mode="wake")
        await self.set_state(DeviceState.CONNECTING)
        if self.timer_task is not None and self.timer_task is not asyncio.current_task():
            self.timer_task.cancel()
            await asyncio.gather(self.timer_task, return_exceptions=True)
            self.timer_task = None
        await self._stop_input_sender()
        if self.output is not None:
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                await self.send_json("playback.flush", stream_id=self.output.stream_id)
        await self._stop_playback_sender()
        self.output = None
        self.output_buffer.clear()
        self.playback_progress_event.set()
        self.cloud = None
        if old_cloud is not None:
            await old_cloud.close()
        if old_session_id is not None:
            self.db.end_session(old_session_id, "project_switch", self.usage)
        if old_session_id is not None and old_project_id is not None:
            asyncio.create_task(self.planner.update_after_session(old_project_id, old_session_id))
        for recording in (self.diagnostic_input, self.diagnostic_output):
            if recording is not None:
                recording.close()
        self.diagnostic_input = self.diagnostic_output = None
        self.db.activate_project(project["id"])
        self.project_id = project["id"]
        self.session_id = self.db.start_session(
            self.project_id, self.device_id, self.settings.realtime_model
        )
        self.usage = {}
        self.assistant_text = ""
        self.cancelled_response_ids.clear()
        self._open_switched_session_recordings()
        context = build_instructions(
            project,
            self.db.recent_turns(self.project_id, 12),
            ready_keyword_enabled=self.ready_keyword_enabled,
        )
        self.cloud = RealtimeConnection(
            self.settings,
            context,
            self.handle_openai_event,
            ready_keyword_enabled=self.ready_keyword_enabled,
        )
        try:
            await self.cloud.connect()
        except Exception:
            self.db.end_session(self.session_id, "connect_error", {})
            self.cloud = None
            await self.set_state(DeviceState.ERROR)
            raise
        self.started_monotonic = self.last_activity = time.monotonic()
        self.announcement_echo_guard = True
        self._clear_input_queue()
        self.accepting_audio = True
        self.cloud_ready = True
        self._start_input_sender()
        self._start_playback_sender()
        self.timer_task = asyncio.create_task(
            self._watch_timeouts(), name=f"session-timer-{self.device_id}"
        )
        await self.set_state(DeviceState.LISTENING)
        await self.send_json(
            "session.started", session_id=self.session_id, project_id=self.project_id
        )
        logger.info(
            "Project switch complete device=%s session=%s project=%s",
            self.device_id,
            self.session_id,
            self.project_id,
        )
        await self._announce_project(project["name"])

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
            raise RuntimeError(
                "assistant playback exceeded the bounded "
                f"{self.settings.playback_buffer_seconds}-second queue"
            ) from exc

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
        """Pace audio and bound how far transmission can lead physical DAC playback."""
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
            await self._wait_for_playback_capacity(packet.stream_id)
            output = self.output
            if output is None or output.stream_id != packet.stream_id:
                continue
            await self.send_bytes(packet.data)
            if self.diagnostic_output is not None:
                self.diagnostic_output.write(packet.data)
            output.sent_ms += 20
            next_send = max(next_send, loop.time()) + PLAYBACK_FRAME_SECONDS

    async def _wait_for_playback_capacity(self, stream_id: str) -> None:
        """Use device DAC progress as flow control for its small fixed playback queue."""
        while True:
            output = self.output
            if output is None or output.stream_id != stream_id:
                return
            if output.sent_ms - output.played_ms < MAX_DEVICE_PLAYBACK_LEAD_MS:
                return
            self.playback_progress_event.clear()
            await self.playback_progress_event.wait()

    async def stop(self, reason: str, notify_device: bool = True) -> None:
        if self.stopping or self.cloud is None:
            return
        self.stopping = True
        session_id, project_id = self.session_id, self.project_id
        logger.info(
            "Ending session device=%s session=%s reason=%s state=%s",
            self.device_id,
            session_id,
            reason,
            self.state.value,
        )
        try:
            await self._cancel_search_tasks()
            self.accepting_audio = False
            self.cloud_ready = False
            self.ready_wait_requested = False
            self.waiting_for_ready = False
            self.pending_ready_call_id = None
            self.warmup_vad_items.clear()
            self._reset_barge_in_level_gate()
            self._reset_listening_level_gate()
            if self.ready_keyword_enabled:
                notify_device = await self.send_optional(
                    "keyword.mode", notify_device, mode="wake"
                )
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
            self.playback_progress_event.set()
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
            if self._idle_timeout_expired(current):
                await self.stop("idle_timeout")
                return

    def _idle_timeout_expired(self, current: float) -> bool:
        return (
            self.settings.idle_timeout_seconds > 0
            and self.state == DeviceState.LISTENING
            and current - self.last_activity >= self.settings.idle_timeout_seconds
        )

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
        self._reset_barge_in_level_gate()
        self._reset_listening_level_gate()
        self.playback_progress_event.set()
        if self.announcement_echo_guard:
            self.announcement_echo_guard = False
            self.echo_gate_until = time.monotonic() + 0.3
        elif not self.settings.barge_in_enabled:
            self.echo_gate_until = time.monotonic() + 0.3
        self.last_activity = time.monotonic()
        await self.set_state(DeviceState.LISTENING)
        await self._enter_ready_wait_if_possible()

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
