import asyncio
import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import websockets

from .config import Settings

logger = logging.getLogger(__name__)
EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class RealtimeConnection:
    """Thin, injectable transport around the OpenAI Realtime WebSocket."""

    def __init__(self, settings: Settings, instructions: str, on_event: EventHandler):
        self.settings = settings
        self.instructions = instructions
        self.on_event = on_event
        self.socket: Any = None
        self.reader_task: asyncio.Task[None] | None = None
        self.send_lock = asyncio.Lock()
        self.connected_monotonic = 0.0
        self.input_audio_bytes = 0
        self.output_audio_bytes = 0
        self.input_audio_frames = 0
        self.last_audio_trace = 0.0
        self.response_started: dict[str, float] = {}
        self.response_first_audio: set[str] = set()
        self.response_count = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    def _turn_detection_config(self) -> dict[str, Any]:
        config: dict[str, Any] = {
            "type": self.settings.vad_mode,
            "create_response": True,
            "interrupt_response": self.settings.barge_in_enabled,
        }
        if self.settings.vad_mode == "server_vad":
            config.update(
                threshold=self.settings.vad_threshold,
                prefix_padding_ms=self.settings.vad_prefix_padding_ms,
                silence_duration_ms=self.settings.vad_silence_duration_ms,
            )
        else:
            config["eagerness"] = self.settings.vad_eagerness
        return config

    async def connect(self) -> None:
        if not self.settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        url = f"wss://api.openai.com/v1/realtime?model={quote(self.settings.realtime_model)}"
        logger.info("Connecting to OpenAI Realtime model=%s", self.settings.realtime_model)
        self.socket = await websockets.connect(
            url,
            additional_headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
            max_size=4 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        )
        logger.info("OpenAI Realtime WebSocket connected")
        self.connected_monotonic = time.monotonic()
        self.last_audio_trace = self.connected_monotonic
        await self.send(
            {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "instructions": self.instructions,
                    "output_modalities": ["audio"],
                    "reasoning": {"effort": self.settings.reasoning_effort},
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcm", "rate": 24000},
                            "transcription": {"model": self.settings.transcription_model},
                            "turn_detection": self._turn_detection_config(),
                        },
                        "output": {
                            "format": {"type": "audio/pcm", "rate": 24000},
                            "voice": self.settings.voice,
                        },
                    },
                    "tools": [
                        {
                            "type": "function",
                            "name": "end_session",
                            "description": "End the device voice session after an explicit request such as go to sleep, stop, goodbye, good night, end session, or that's all.",
                            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                        }
                    ],
                    "tool_choice": "auto",
                },
            }
        )
        logger.info(
            "OpenAI session.update sent: voice=%s reasoning=%s vad=%s transcription=%s barge_in=%s",
            self.settings.voice,
            self.settings.reasoning_effort,
            self.settings.vad_mode,
            self.settings.transcription_model,
            self.settings.barge_in_enabled,
        )
        self.reader_task = asyncio.create_task(self._reader(), name="openai-realtime-reader")

    async def send(self, event: dict[str, Any]) -> None:
        if self.socket is None:
            raise RuntimeError("Realtime socket is not connected")
        async with self.send_lock:
            await self.socket.send(json.dumps(event))
        if self.settings.openai_trace and event.get("type") != "input_audio_buffer.append":
            logger.info("OpenAI trace tx %s", self._safe_event(event))

    async def append_audio(self, pcm: bytes) -> None:
        await self.send(
            {"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode("ascii")}
        )
        self.input_audio_bytes += len(pcm)
        self.input_audio_frames += 1
        current = time.monotonic()
        if self.settings.openai_trace and current - self.last_audio_trace >= 5:
            logger.info(
                "OpenAI trace input_audio frames=%d seconds=%.2f bytes=%d",
                self.input_audio_frames,
                self.input_audio_bytes / 48000,
                self.input_audio_bytes,
            )
            self.last_audio_trace = current

    async def cancel_response(self) -> None:
        await self.send({"type": "response.cancel"})

    async def truncate(self, item_id: str, content_index: int, played_ms: int) -> None:
        await self.send(
            {
                "type": "conversation.item.truncate",
                "item_id": item_id,
                "content_index": content_index,
                "audio_end_ms": played_ms,
            }
        )

    async def _reader(self) -> None:
        try:
            async for raw in self.socket:
                event = json.loads(raw)
                kind = event.get("type", "unknown")
                if kind in {"session.created", "session.updated"}:
                    logger.info("OpenAI Realtime event: %s", kind)
                elif kind == "error":
                    logger.error("OpenAI Realtime error event: %s", event.get("error", event))
                if self.settings.openai_trace:
                    self._trace_incoming(event)
                await self.on_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("OpenAI Realtime reader stopped")
            await self.on_event({"type": "gateway.transport_error", "error": str(exc)})

    async def close(self) -> None:
        current = asyncio.current_task()
        if self.reader_task and self.reader_task is not current:
            self.reader_task.cancel()
            await asyncio.gather(self.reader_task, return_exceptions=True)
        if self.socket is not None:
            await self.socket.close()
        if self.settings.openai_trace and self.connected_monotonic:
            logger.info(
                "OpenAI trace session_closed duration_s=%.2f input_audio_s=%.2f output_audio_s=%.2f responses=%d input_tokens=%d output_tokens=%d",
                time.monotonic() - self.connected_monotonic,
                self.input_audio_bytes / 48000,
                self.output_audio_bytes / 48000,
                self.response_count,
                self.total_input_tokens,
                self.total_output_tokens,
            )
        self.socket = None

    @staticmethod
    def _safe_event(event: dict[str, Any]) -> dict[str, Any]:
        """Return lifecycle metadata only; never include audio, prompts, or transcript text."""
        safe_keys = (
            "type",
            "event_id",
            "response_id",
            "item_id",
            "call_id",
            "content_index",
            "audio_start_ms",
            "audio_end_ms",
            "name",
        )
        return {key: event[key] for key in safe_keys if key in event}

    def _trace_incoming(self, event: dict[str, Any]) -> None:
        kind = event.get("type", "unknown")
        if kind in {"session.created", "session.updated"}:
            session = event.get("session", {})
            logger.info(
                "OpenAI trace %s session_id=%s model=%s expires_at=%s",
                kind.replace(".", "_"),
                session.get("id"),
                session.get("model"),
                session.get("expires_at"),
            )
            return
        if kind in {"response.output_audio.delta", "response.audio.delta"}:
            response_id = event.get("response_id", "unknown")
            audio_bytes = len(base64.b64decode(event.get("delta", "")))
            self.output_audio_bytes += audio_bytes
            if response_id not in self.response_first_audio:
                self.response_first_audio.add(response_id)
                started = self.response_started.get(response_id)
                latency_ms = None if started is None else round((time.monotonic() - started) * 1000)
                logger.info(
                    "OpenAI trace first_audio response_id=%s latency_ms=%s",
                    response_id,
                    latency_ms,
                )
            return
        if kind in {
            "response.output_audio_transcript.delta",
            "response.audio_transcript.delta",
        }:
            return
        if kind == "response.created":
            response = event.get("response", {})
            response_id = response.get("id", event.get("response_id", "unknown"))
            self.response_started[response_id] = time.monotonic()
            self.response_count += 1
            logger.info("OpenAI trace response_created response_id=%s", response_id)
            return
        if kind == "response.done":
            response = event.get("response", {})
            response_id = response.get("id", event.get("response_id", "unknown"))
            usage = response.get("usage") or {}
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens
            started = self.response_started.pop(response_id, None)
            elapsed_ms = None if started is None else round((time.monotonic() - started) * 1000)
            logger.info(
                "OpenAI trace response_done response_id=%s status=%s elapsed_ms=%s usage=%s",
                response_id,
                response.get("status"),
                elapsed_ms,
                usage,
            )
            return
        summary = self._safe_event(event)
        if summary:
            logger.info("OpenAI trace rx %s", summary)
