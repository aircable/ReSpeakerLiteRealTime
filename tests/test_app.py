import json
import logging
import wave

import httpx
from fastapi import WebSocketDisconnect

from gateway.app import app, device_sessions, device_socket
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
                    "input_noise_reduction": "far_field",
                    "vad_threshold": 0.55,
                    "vad_silence_duration_ms": 600,
                    "barge_in_rms_threshold": 8500,
                    "xvf_agc_ch0_gain": 25.0,
                    "listening_rms_threshold": 800,
                },
            )
            assert saved.status_code == 200
            current = await client.get(
                "/api/settings", headers={"Authorization": "Bearer browser-secret"}
            )
            assert current.json()["voice"] == "cedar"
            assert current.json()["openai_trace"] is True
            assert current.json()["vad_mode"] == "server_vad"
            assert current.json()["input_noise_reduction"] == "far_field"
            assert current.json()["vad_threshold"] == 0.55
            assert current.json()["vad_silence_duration_ms"] == 600
            assert current.json()["barge_in_rms_threshold"] == 8500
            assert current.json()["xvf_agc_ch0_gain"] == 25.0
            assert current.json()["listening_rms_threshold"] == 800
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


async def test_ffva_mic_capture_saves_authenticated_wav_without_realtime_session(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    sample = (1234).to_bytes(2, "little", signed=True)
    pcm = sample * 16_000
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Content-Type": "application/octet-stream", "X-Device-Token": "device-secret"}
            assert (await client.post("/api/diagnostics/ffva-mic", headers=headers, content=pcm)).status_code == 403
            saved = await client.patch(
                "/api/settings", headers={"Authorization": "Bearer browser-secret"},
                json={"diagnostic_audio": True},
            )
            assert saved.status_code == 200
            assert (await client.post("/api/diagnostics/ffva-mic", content=pcm)).status_code == 401
            assert (
                await client.post(
                    "/api/diagnostics/ffva-mic", headers={**headers, "X-Device-Token": "wrong"},
                    content=pcm,
                )
            ).status_code == 401
            response = await client.post("/api/diagnostics/ffva-mic", headers=headers, content=pcm)
            assert response.status_code == 200
            assert response.json()["seconds"] == 1.0
            assert response.json()["file"].startswith("ffva-mic-")
            assert not device_sessions

    path = tmp_path / "diagnostic-audio" / response.json()["file"]
    with wave.open(str(path), "rb") as recording:
        assert recording.getnchannels() == 1
        assert recording.getsampwidth() == 2
        assert recording.getframerate() == 16_000
        assert recording.readframes(16_000) == pcm
    get_settings.cache_clear()


async def test_ffva_mic_capture_rejects_bad_and_oversized_audio(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    headers = {"Content-Type": "application/octet-stream", "X-Device-Token": "device-secret"}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.patch(
                "/api/settings", headers={"Authorization": "Bearer browser-secret"},
                json={"diagnostic_audio": True},
            )
            assert (
                await client.post("/api/diagnostics/ffva-mic", headers=headers, content=b"\0")
            ).status_code == 400
            assert (
                await client.post(
                    "/api/diagnostics/ffva-mic", headers=headers, content=b"\0" * 320_002
                )
            ).status_code == 413
            assert (
                await client.post(
                    "/api/diagnostics/ffva-mic", headers={"X-Device-Token": "device-secret"},
                    content=b"\0" * 32_000,
                )
            ).status_code == 415
    assert not (tmp_path / "diagnostic-audio").exists()
    get_settings.cache_clear()


class FakeDeviceSocket:
    def __init__(self, capabilities=None, device_id="test-unit", name=None):
        self.sent = []
        self.received = False
        self.capabilities = capabilities or {"aec": True}
        self.device_id = device_id
        self.name = name

    async def accept(self):
        pass

    async def receive_json(self):
        return {
            "v": 1,
            "type": "auth",
            "token": "device-secret",
            "device_id": self.device_id,
            "name": self.name,
            "capabilities": self.capabilities,
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


async def test_connected_devices_show_names_and_transcripts_filter_by_device(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)

    class ConnectedDevice:
        def __init__(self, device_id, device_name):
            self.device_id = device_id
            self.device_name = device_name
            self.state = type("State", (), {"value": "listening"})()
            self.device_volume = 0.25

    device_sessions["kitchen"] = ConnectedDevice("kitchen", "Kitchen Companion")
    device_sessions["office"] = ConnectedDevice("office", "Office Companion")
    try:
        async with app.router.lifespan_context(app):
            from gateway.db import Database

            db = Database(tmp_path / "app.db")
            project_id = db.get_project()["id"]
            kitchen = db.start_session(project_id, "kitchen", "test-model")
            office = db.start_session(project_id, "office", "test-model")
            db.add_turn(kitchen, "user", "Kitchen question")
            db.add_turn(office, "user", "Office question")
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {"Authorization": "Bearer browser-secret"}
                devices = (await client.get("/api/devices", headers=headers)).json()
                turns = (
                    await client.get(
                        f"/api/projects/{project_id}/turns",
                        headers=headers,
                        params={"device_id": "office"},
                    )
                ).json()
        assert {(device["device_id"], device["name"]) for device in devices} == {
            ("kitchen", "Kitchen Companion"),
            ("office", "Office Companion"),
        }
        assert [turn["text"] for turn in turns] == ["Office question"]
    finally:
        device_sessions.pop("kitchen", None)
        device_sessions.pop("office", None)
        get_settings.cache_clear()


async def test_xvf_device_gets_gain_on_authentication(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    async with app.router.lifespan_context(app):
        socket = FakeDeviceSocket({"aec": True, "xvf_agc_ch0": True})
        await device_socket(socket)
    assert [message["type"] for message in socket.sent] == [
        "auth.ok", "mic_gain.set", "heartbeat.ack"
    ]
    assert socket.sent[1]["ch0_gain"] == 25.0
    assert socket.sent[2]["monotonic_ms"] == 123
    get_settings.cache_clear()


async def test_gain_setting_reaches_connected_xvf_without_new_session(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)

    class GainSession:
        xvf_agc_ch0_supported = True

        def __init__(self):
            self.gains = []

        async def request_xvf_gain(self, gain):
            self.gains.append(gain)

    device = GainSession()
    device_sessions["xvf-test"] = device
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.patch(
                    "/api/settings",
                    headers={"Authorization": "Bearer browser-secret"},
                    json={"xvf_agc_ch0_gain": 12.5},
                )
        assert response.status_code == 200
        assert device.gains == [12.5]
    finally:
        del device_sessions["xvf-test"]
        get_settings.cache_clear()
