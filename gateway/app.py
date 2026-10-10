import asyncio
import logging
import os
import secrets
import wave
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as distribution_version
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .db import Database
from .planner import Planner
from .protocol import (
    Authenticate,
    Heartbeat,
    PlaybackProgress,
    ReadyDetected,
    SessionStart,
    SessionStop,
    StateReport,
    VolumeChanged,
    parse_device_message,
    server_message,
)
from .session import DeviceSession

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

try:
    GATEWAY_VERSION = distribution_version("respeaker-thinking-companion") or "0.1.0-dev"
except (PackageNotFoundError, KeyError):
    GATEWAY_VERSION = "0.1.0-dev"
GATEWAY_COMMIT = os.environ.get("GATEWAY_COMMIT", "development")


class LiveHub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def publish(self, message: dict[str, Any]) -> None:
        stale: list[WebSocket] = []
        for client in tuple(self.clients):
            try:
                await client.send_json(message)
            except Exception:
                stale.append(client)
        for client in stale:
            self.clients.discard(client)


live_hub = LiveHub()
device_sessions: dict[str, DeviceSession] = {}


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    goal: str = ""


class ProjectUpdate(BaseModel):
    name: str | None = None
    goal: str | None = None
    instructions: str | None = None
    pinned_notes: str | None = None
    summary: str | None = None
    plan_markdown: str | None = None


class GatewaySettingsUpdate(BaseModel):
    realtime_model: str | None = Field(default=None, min_length=1, max_length=100)
    realtime_max_output_tokens: int | None = Field(default=None, ge=1, le=4096)
    planner_model: str | None = Field(default=None, min_length=1, max_length=100)
    voice: str | None = Field(default=None, min_length=1, max_length=50)
    reasoning_effort: str | None = Field(default=None, pattern="^(low|medium|high)$")
    vad_mode: str | None = Field(default=None, pattern="^(semantic_vad|server_vad)$")
    vad_eagerness: str | None = Field(default=None, pattern="^(low|medium|high|auto)$")
    vad_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    vad_prefix_padding_ms: int | None = Field(default=None, ge=0, le=5000)
    vad_silence_duration_ms: int | None = Field(default=None, ge=100, le=5000)
    input_noise_reduction: str | None = Field(
        default=None, pattern="^(far_field|near_field|off)$"
    )
    idle_timeout_seconds: int | None = Field(default=None, ge=0, le=900)
    hard_session_limit_seconds: int | None = Field(default=None, ge=60, le=7200)
    playback_buffer_seconds: int | None = Field(default=None, ge=30, le=600)
    diagnostic_audio: bool | None = None
    openai_trace: bool | None = None
    barge_in_enabled: bool | None = None
    barge_in_rms_threshold: int | None = Field(default=None, ge=0, le=32768)
    xvf_agc_ch0_gain: float | None = Field(default=None, ge=0.0, le=1000.0)
    listening_rms_threshold: int | None = Field(default=None, ge=0, le=32768)
    announce_active_project: bool | None = None
    transcript_retention_days: int | None = Field(default=None, ge=0, le=3650)


class DeviceVolumeUpdate(BaseModel):
    level_percent: float = Field(ge=0.0, le=100.0)


async def settings_dependency() -> Settings:
    return get_settings()


async def database(settings: Annotated[Settings, Depends(settings_dependency)]) -> Database:
    return Database(settings.database_path)


async def require_ui_token(
    settings: Annotated[Settings, Depends(settings_dependency)],
    authorization: Annotated[str | None, Header()] = None,
    token: Annotated[str | None, Query()] = None,
) -> None:
    candidate = token or (authorization.removeprefix("Bearer ") if authorization else "")
    if not secrets.compare_digest(candidate, settings.ui_token):
        raise HTTPException(status_code=401, detail="invalid UI token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Starting ReSpeaker Thinking Companion gateway version=%s commit=%s",
        GATEWAY_VERSION,
        GATEWAY_COMMIT,
    )
    settings = get_settings()
    Database(settings.database_path).initialize()
    yield


app = FastAPI(title="ReSpeaker Thinking Companion", version=GATEWAY_VERSION, lifespan=lifespan)

FFVA_CAPTURE_RATE = 16_000
FFVA_CAPTURE_MAX_BYTES = FFVA_CAPTURE_RATE * 2 * 10  # Ten seconds of mono PCM16.


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "version": GATEWAY_VERSION,
        "commit": GATEWAY_COMMIT,
    }


@app.post("/api/diagnostics/ffva-mic")
async def ffva_mic_capture(
    request: Request,
    device_token: Annotated[str | None, Header(alias="X-Device-Token")] = None,
) -> dict[str, Any]:
    """Save a short, isolated FFVA ASR-channel test; no Realtime session is opened."""
    settings = get_settings()
    if not device_token or not secrets.compare_digest(device_token, settings.device_token):
        raise HTTPException(status_code=401, detail="invalid device token")
    settings = settings.model_copy(update=Database(settings.database_path).setting_overrides())
    if not settings.diagnostic_audio:
        raise HTTPException(status_code=403, detail="enable diagnostic audio in gateway settings")
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/octet-stream":
        raise HTTPException(status_code=415, detail="expected mono 16 kHz PCM16")

    pcm = bytearray()
    async for chunk in request.stream():
        if len(pcm) + len(chunk) > FFVA_CAPTURE_MAX_BYTES:
            raise HTTPException(status_code=413, detail="capture exceeds 10 seconds")
        pcm.extend(chunk)
    if len(pcm) < FFVA_CAPTURE_RATE * 2 or len(pcm) % 2:
        raise HTTPException(status_code=400, detail="capture must contain 1-10 seconds of PCM16")

    diagnostic_dir = settings.database_path.parent / "diagnostic-audio"
    diagnostic_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    filename = f"ffva-mic-{stamp}-{secrets.token_hex(4)}.wav"
    path = diagnostic_dir / filename
    with wave.open(str(path), "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(FFVA_CAPTURE_RATE)
        recording.writeframes(pcm)
    logger.info("FFVA mic diagnostic saved path=%s seconds=%.2f", path, len(pcm) / (FFVA_CAPTURE_RATE * 2))
    return {"file": filename, "seconds": len(pcm) / (FFVA_CAPTURE_RATE * 2)}


@app.get("/api/projects", dependencies=[Depends(require_ui_token)])
async def projects(db: Annotated[Database, Depends(database)]) -> list[dict[str, Any]]:
    return db.list_projects()


@app.get("/api/settings", dependencies=[Depends(require_ui_token)])
async def gateway_settings(
    settings: Annotated[Settings, Depends(settings_dependency)], db: Annotated[Database, Depends(database)]
) -> dict[str, Any]:
    keys = set(GatewaySettingsUpdate.model_fields)
    current = settings.model_dump(include=keys)
    current.update(db.setting_overrides())
    return current


@app.patch("/api/settings", dependencies=[Depends(require_ui_token)])
async def update_gateway_settings(
    body: GatewaySettingsUpdate, db: Annotated[Database, Depends(database)]
) -> dict[str, bool]:
    values = body.model_dump(exclude_none=True)
    db.update_settings(values)
    if "xvf_agc_ch0_gain" in values:
        for session in tuple(device_sessions.values()):
            if session.xvf_agc_ch0_supported:
                with suppress(WebSocketDisconnect, RuntimeError):
                    await session.request_xvf_gain(values["xvf_agc_ch0_gain"])
    return {"saved": True}


@app.get("/api/devices", dependencies=[Depends(require_ui_token)])
async def connected_devices() -> list[dict[str, Any]]:
    return [
        {
            "device_id": session.device_id,
            "name": session.device_name,
            "state": session.state.value,
            "volume_percent": (
                round(session.device_volume * 100) if session.device_volume is not None else None
            ),
        }
        for session in device_sessions.values()
    ]


@app.patch("/api/devices/{device_id}/volume", dependencies=[Depends(require_ui_token)])
async def update_device_volume(device_id: str, body: DeviceVolumeUpdate) -> dict[str, Any]:
    session = device_sessions.get(device_id)
    if session is None:
        raise HTTPException(404, "device is not connected")
    return await session.control_volume("set", body.level_percent)


@app.post("/api/projects", dependencies=[Depends(require_ui_token)])
async def create_project(
    body: ProjectCreate, db: Annotated[Database, Depends(database)]
) -> dict[str, Any]:
    return db.create_project(body.name, body.goal)


@app.get("/api/projects/{project_id}", dependencies=[Depends(require_ui_token)])
async def get_project(project_id: int, db: Annotated[Database, Depends(database)]) -> dict[str, Any]:
    try:
        return db.get_project(project_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.patch("/api/projects/{project_id}", dependencies=[Depends(require_ui_token)])
async def update_project(
    project_id: int, body: ProjectUpdate, db: Annotated[Database, Depends(database)]
) -> dict[str, Any]:
    return db.update_project(project_id, body.model_dump(exclude_none=True))


@app.post("/api/projects/{project_id}/activate", dependencies=[Depends(require_ui_token)])
async def activate_project(project_id: int, db: Annotated[Database, Depends(database)]) -> dict[str, bool]:
    try:
        db.activate_project(project_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"active": True}


@app.get("/api/projects/{project_id}/history", dependencies=[Depends(require_ui_token)])
async def project_history(
    project_id: int, db: Annotated[Database, Depends(database)]
) -> list[dict[str, Any]]:
    return db.plan_history(project_id)


@app.get("/api/projects/{project_id}/turns", dependencies=[Depends(require_ui_token)])
async def project_turns(
    project_id: int, db: Annotated[Database, Depends(database)], limit: int = 100,
    device_id: str | None = None,
) -> list[dict[str, Any]]:
    return db.project_turns(project_id, limit, device_id)


@app.get("/api/projects/{project_id}/export.md", dependencies=[Depends(require_ui_token)])
async def export_project(project_id: int, db: Annotated[Database, Depends(database)]) -> PlainTextResponse:
    project = db.get_project(project_id)
    body = f"# {project['name']}\n\n## Goal\n\n{project['goal']}\n\n{project['plan_markdown']}\n"
    return PlainTextResponse(body, headers={"Content-Disposition": f'attachment; filename="project-{project_id}.md"'})


@app.websocket("/ws/device")
async def device_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    settings = get_settings()
    session: DeviceSession | None = None
    try:
        raw = await asyncio.wait_for(websocket.receive_json(), timeout=5)
        auth = parse_device_message(raw)
        if not isinstance(auth, Authenticate) or not secrets.compare_digest(auth.token, settings.device_token):
            await websocket.send_json(server_message("error", code="authentication_failed"))
            await websocket.close(code=1008)
            return
        db = Database(settings.database_path)
        effective_settings = settings.model_copy(update=db.setting_overrides())
        planner = Planner(effective_settings, db)
        session = DeviceSession(
            websocket,
            auth.device_id,
            effective_settings,
            db,
            planner,
            live_hub.publish,
            capabilities=auth.capabilities,
            device_name=auth.name,
        )
        device_sessions[auth.device_id] = session
        logger.info(
            "Device authenticated device=%s ready_keyword=%s capabilities=%s",
            auth.device_id,
            bool(auth.capabilities.get("ready_keyword")),
            sorted(auth.capabilities),
        )
        await session.send_json(
            "auth.ok",
            device_id=auth.device_id,
            audio={"format": "pcm_s16le", "sample_rate": 24000, "channels": 1, "frame_ms": 20},
        )
        # Apply before the wake word whenever the device is already online.
        # start() resends current UI overrides for a persistent connection.
        await session.request_xvf_gain(effective_settings.xvf_agc_ch0_gain)
        while True:
            incoming = await websocket.receive()
            if incoming.get("bytes") is not None:
                await session.receive_audio(incoming["bytes"])
                continue
            text = incoming.get("text")
            if text is None:
                continue
            import json

            message = parse_device_message(json.loads(text))
            if isinstance(message, SessionStart):
                logger.info("Device requested session start device=%s project=%s", auth.device_id, message.project_id)
                # Opening a Realtime session can take seconds. Keep consuming device audio
                # while that happens so the ESP32 never backs up on its WebSocket writes.
                await session.request_start(message.project_id)
            elif isinstance(message, SessionStop):
                await session.stop(message.reason)
            elif isinstance(message, PlaybackProgress):
                await session.playback_progress(message.stream_id, message.played_ms)
            elif isinstance(message, StateReport) and message.muted:
                await session.set_state(message.state)
            elif isinstance(message, Heartbeat):
                await session.send_json("heartbeat.ack", monotonic_ms=message.monotonic_ms)
            elif isinstance(message, VolumeChanged):
                await session.report_volume(message.level)
            elif isinstance(message, ReadyDetected):
                await session.ready_detected()
    except WebSocketDisconnect as exc:
        logger.warning(
            "Device WebSocket disconnected device=%s code=%s reason=%s",
            session.device_id if session is not None else "unauthenticated",
            exc.code,
            exc.reason or "none",
        )
    except RuntimeError as exc:
        logger.warning(
            "Device WebSocket runtime close device=%s detail=%s",
            session.device_id if session is not None else "unauthenticated",
            exc,
        )
    except Exception as exc:
        logger.exception("device connection failed")
        try:
            await websocket.send_json(server_message("error", code="protocol_error", detail=str(exc)))
        except Exception:
            pass
    finally:
        if session is not None:
            logger.info("Device WebSocket closing device=%s", session.device_id)
            if device_sessions.get(session.device_id) is session:
                device_sessions.pop(session.device_id, None)
            await session.close()


@app.websocket("/ws/ui")
async def ui_socket(websocket: WebSocket, token: str = "") -> None:
    settings = get_settings()
    if not secrets.compare_digest(token, settings.ui_token):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    live_hub.clients.add(websocket)
    try:
        await websocket.send_json(server_message("ui.connected"))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        live_hub.clients.discard(websocket)
