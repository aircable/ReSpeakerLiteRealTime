import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .db import Database
from .planner import Planner
from .protocol import (
    Authenticate,
    Heartbeat,
    PlaybackProgress,
    SessionStart,
    SessionStop,
    StateReport,
    parse_device_message,
    server_message,
)
from .session import DeviceSession

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


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
    planner_model: str | None = Field(default=None, min_length=1, max_length=100)
    voice: str | None = Field(default=None, min_length=1, max_length=50)
    reasoning_effort: str | None = Field(default=None, pattern="^(low|medium|high)$")
    idle_timeout_seconds: int | None = Field(default=None, ge=5, le=900)
    hard_session_limit_seconds: int | None = Field(default=None, ge=60, le=7200)
    diagnostic_audio: bool | None = None
    transcript_retention_days: int | None = Field(default=None, ge=0, le=3650)


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
    settings = get_settings()
    Database(settings.database_path).initialize()
    yield


app = FastAPI(title="ReSpeaker Thinking Companion", version="0.1.0", lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


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
    db.update_settings(body.model_dump(exclude_none=True))
    return {"saved": True}


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
    project_id: int, db: Annotated[Database, Depends(database)], limit: int = 100
) -> list[dict[str, Any]]:
    return db.project_turns(project_id, limit)


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
            websocket, auth.device_id, effective_settings, db, planner, live_hub.publish
        )
        logger.info("Device authenticated device=%s", auth.device_id)
        await session.send_json(
            "auth.ok",
            device_id=auth.device_id,
            audio={"format": "pcm_s16le", "sample_rate": 24000, "channels": 1, "frame_ms": 20},
        )
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
                await session.start(message.project_id)
            elif isinstance(message, SessionStop):
                await session.stop(message.reason)
            elif isinstance(message, PlaybackProgress):
                await session.playback_progress(message.stream_id, message.played_ms)
            elif isinstance(message, StateReport) and message.muted:
                await session.set_state(message.state)
            elif isinstance(message, Heartbeat):
                await session.send_json("heartbeat.ack", monotonic_ms=message.monotonic_ms)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception as exc:
        logger.exception("device connection failed")
        try:
            await websocket.send_json(server_message("error", code="protocol_error", detail=str(exc)))
        except Exception:
            pass
    finally:
        if session is not None:
            logger.info("Device WebSocket closing device=%s", session.device_id)
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
