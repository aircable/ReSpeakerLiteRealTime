from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

PROTOCOL_VERSION = 1
SAMPLE_RATE = 24_000
FRAME_DURATION_MS = 20
FRAME_BYTES = SAMPLE_RATE * FRAME_DURATION_MS // 1000 * 2


class DeviceState(StrEnum):
    IDLE = "idle"
    CONNECTING = "connecting"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    MUTED = "muted"
    ERROR = "error"


class Envelope(BaseModel):
    type: str
    v: Literal[1] = PROTOCOL_VERSION


class Authenticate(Envelope):
    type: Literal["auth"] = "auth"
    token: str
    device_id: str = Field(min_length=1, max_length=80)
    capabilities: dict[str, Any] = Field(default_factory=dict)


class SessionStart(Envelope):
    type: Literal["session.start"] = "session.start"
    project_id: int | None = None


class SessionStop(Envelope):
    type: Literal["session.stop"] = "session.stop"
    reason: str = "device"


class PlaybackProgress(Envelope):
    type: Literal["playback.progress"] = "playback.progress"
    stream_id: str
    played_ms: int = Field(ge=0)


class StateReport(Envelope):
    type: Literal["state"] = "state"
    state: DeviceState
    muted: bool = False


class Heartbeat(Envelope):
    type: Literal["heartbeat"] = "heartbeat"
    monotonic_ms: int = Field(ge=0)


def parse_device_message(data: dict[str, Any]) -> Envelope:
    message_type = data.get("type")
    models: dict[str, type[Envelope]] = {
        "auth": Authenticate,
        "session.start": SessionStart,
        "session.stop": SessionStop,
        "playback.progress": PlaybackProgress,
        "state": StateReport,
        "heartbeat": Heartbeat,
    }
    model = models.get(message_type)
    if model is None:
        raise ValueError(f"unknown message type: {message_type!r}")
    return model.model_validate(data)


def server_message(message_type: str, **payload: Any) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "type": message_type, **payload}

