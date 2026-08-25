import base64
from dataclasses import replace

from gateway.config import Settings
from gateway.db import Database
from gateway.planner import Planner
from gateway.protocol import FRAME_BYTES, DeviceState
from gateway.session import DeviceSession


class FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send_json(self, value):
        self.messages.append(("json", value))

    async def send_bytes(self, value):
        self.messages.append(("bytes", value))


class FakeCloud:
    def __init__(self):
        self.audio = []
        self.cancelled = 0
        self.truncations = []

    async def append_audio(self, pcm):
        self.audio.append(pcm)

    async def cancel_response(self):
        self.cancelled += 1

    async def truncate(self, item_id, content_index, played_ms):
        self.truncations.append((item_id, content_index, played_ms))


def make_session(tmp_path):
    settings = Settings(
        device_token="device-secret",
        ui_token="browser-secret",
        database_path=tmp_path / "test.db",
        idle_timeout_seconds=30,
    )
    db = Database(settings.database_path)
    db.initialize()
    ws = FakeWebSocket()
    session = DeviceSession(ws, "device", settings, db, Planner(settings, db))
    session.session_id = db.start_session(db.get_project()["id"], "device", "test")
    session.project_id = db.get_project()["id"]
    session.cloud = FakeCloud()
    return session, ws


async def test_frame_boundaries_are_enforced(tmp_path):
    session, ws = make_session(tmp_path)
    await session.receive_audio(b"bad")
    await session.receive_audio(bytes(FRAME_BYTES))
    assert ws.messages[0][1]["code"] == "bad_audio_frame"
    assert session.cloud.audio == [bytes(FRAME_BYTES)]


async def test_barge_in_flushes_cancels_truncates_and_rejects_late_audio(tmp_path):
    session, ws = make_session(tmp_path)
    audio = bytes(FRAME_BYTES)
    event = {
        "type": "response.output_audio.delta",
        "response_id": "response-1",
        "item_id": "item-1",
        "content_index": 0,
        "delta": base64.b64encode(audio).decode(),
    }
    await session.handle_openai_event(event)
    stream_id = session.output.stream_id
    await session.playback_progress(stream_id, 20)
    await session.handle_openai_event({"type": "input_audio_buffer.speech_started"})

    assert any(message[1]["type"] == "playback.flush" for message in ws.messages if message[0] == "json")
    assert session.cloud.cancelled == 1
    assert session.cloud.truncations == [("item-1", 0, 20)]
    assert session.state == DeviceState.LISTENING

    binary_count = sum(kind == "bytes" for kind, _ in ws.messages)
    await session.handle_openai_event(event)  # late server event after cancellation
    assert sum(kind == "bytes" for kind, _ in ws.messages) == binary_count
    assert session.output is None


async def test_output_is_chunked_and_padded_to_twenty_ms(tmp_path):
    session, ws = make_session(tmp_path)
    short_audio = bytes(100)
    await session.handle_openai_event(
        {
            "type": "response.output_audio.delta",
            "response_id": "r",
            "item_id": "i",
            "content_index": 0,
            "delta": base64.b64encode(short_audio).decode(),
        }
    )
    assert not any(kind == "bytes" for kind, _ in ws.messages)
    await session.handle_openai_event({"type": "response.output_audio.done"})
    frames = [value for kind, value in ws.messages if kind == "bytes"]
    assert len(frames) == 1
    assert len(frames[0]) == FRAME_BYTES

