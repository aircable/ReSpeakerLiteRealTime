import json
import logging

import httpx
from fastapi import WebSocketDisconnect

from gateway.app import app, device_socket
from gateway.config import get_settings


def configure(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DEVICE_TOKEN", "device-secret")
    monkeypatch.setenv("UI_TOKEN", "browser-secret")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()


async def test_ui_api_uses_separate_bearer_token(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/api/projects")).status_code == 401
            response = await client.get(
                "/api/projects", headers={"Authorization": "Bearer browser-secret"}
            )
            assert response.status_code == 200
            assert response.json()[0]["active"] == 1
            saved = await client.patch(
                "/api/settings",
                headers={"Authorization": "Bearer browser-secret"},
                json={
                    "voice": "cedar",
                    "idle_timeout_seconds": 45,
                    "openai_trace": True,
                    "vad_mode": "server_vad",
                    "vad_threshold": 0.55,
                    "vad_silence_duration_ms": 600,
                },
            )
            assert saved.status_code == 200
            current = await client.get(
                "/api/settings", headers={"Authorization": "Bearer browser-secret"}
            )
            assert current.json()["voice"] == "cedar"
            assert current.json()["openai_trace"] is True
            assert current.json()["vad_mode"] == "server_vad"
            assert current.json()["vad_threshold"] == 0.55
            assert current.json()["vad_silence_duration_ms"] == 600
    get_settings.cache_clear()


async def test_startup_logs_and_health_identify_build(monkeypatch, tmp_path, caplog):
    configure(monkeypatch, tmp_path)
    with caplog.at_level(logging.INFO, logger="gateway.app"):
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                payload = (await client.get("/health")).json()

    assert payload["status"] == "ok"
    assert payload["version"]
    assert payload["commit"]
    assert "Starting ReSpeaker Thinking Companion gateway version=" in caplog.text
    assert " commit=" in caplog.text
    get_settings.cache_clear()


class FakeDeviceSocket:
    def __init__(self):
        self.sent = []
        self.received = False

    async def accept(self):
        pass

    async def receive_json(self):
        return {
            "v": 1,
            "type": "auth",
            "token": "device-secret",
            "device_id": "test-unit",
            "capabilities": {"aec": True},
        }

    async def receive(self):
        if not self.received:
            self.received = True
            return {"text": json.dumps({"v": 1, "type": "heartbeat", "monotonic_ms": 123})}
        raise WebSocketDisconnect()

    async def send_json(self, value):
        self.sent.append(value)

    async def close(self, code=1000):
        pass


async def test_device_websocket_auth_and_heartbeat(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    async with app.router.lifespan_context(app):
        socket = FakeDeviceSocket()
        await device_socket(socket)
    assert [message["type"] for message in socket.sent] == ["auth.ok", "heartbeat.ack"]
    assert socket.sent[1]["monotonic_ms"] == 123
    get_settings.cache_clear()
