import asyncio
import base64
import json
import logging
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
                            "turn_detection": {
                                "type": "semantic_vad",
                                "eagerness": "auto",
                                "create_response": True,
                                "interrupt_response": self.settings.barge_in_enabled,
                            },
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
                            "description": "End the device voice session after an explicit user request.",
                            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                        }
                    ],
                    "tool_choice": "auto",
                },
            }
        )
        logger.info(
            "OpenAI session.update sent: voice=%s reasoning=%s vad=semantic_vad transcription=%s barge_in=%s",
            self.settings.voice,
            self.settings.reasoning_effort,
            self.settings.transcription_model,
            self.settings.barge_in_enabled,
        )
        self.reader_task = asyncio.create_task(self._reader(), name="openai-realtime-reader")

    async def send(self, event: dict[str, Any]) -> None:
        if self.socket is None:
            raise RuntimeError("Realtime socket is not connected")
        async with self.send_lock:
            await self.socket.send(json.dumps(event))

    async def append_audio(self, pcm: bytes) -> None:
        await self.send(
            {"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode("ascii")}
        )

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
        self.socket = None
