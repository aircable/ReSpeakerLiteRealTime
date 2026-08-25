import json

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
                json={"voice": "cedar", "idle_timeout_seconds": 45},
            )
            assert saved.status_code == 200
            current = await client.get(
                "/api/settings", headers={"Authorization": "Bearer browser-secret"}
            )
            assert current.json()["voice"] == "cedar"
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
