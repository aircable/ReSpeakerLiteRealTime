import asyncio
import base64
from fastapi import WebSocketDisconnect

from gateway.config import Settings
from gateway.db import Database
from gateway.planner import Planner
from gateway.protocol import FRAME_BYTES, DeviceState
from gateway.session import (
    MAX_QUEUED_INPUT_FRAMES,
    DeviceSession,
    OutputStream,
)


class FakeWebSocket:
    def __init__(self):
        self.messages = []
        self.disconnected = False

    async def send_json(self, value):
        if self.disconnected:
            raise WebSocketDisconnect(code=1006)
        self.messages.append(("json", value))

    async def send_bytes(self, value):
        self.messages.append(("bytes", value))


class FakeCloud:
    def __init__(self):
        self.audio = []
        self.cancelled = 0
        self.truncations = []
        self.closed = False

    async def append_audio(self, pcm):
        self.audio.append(pcm)

    async def cancel_response(self):
        self.cancelled += 1

    async def truncate(self, item_id, content_index, played_ms):
        self.truncations.append((item_id, content_index, played_ms))

    async def close(self):
        self.closed = True


class FakePlanner:
    def __init__(self):
        self.updates = []

    async def update_after_session(self, project_id, session_id):
        self.updates.append((project_id, session_id))
        return True


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
    session.accepting_audio = True
    session.cloud_ready = True
    return session, ws


async def test_frame_boundaries_are_enforced(tmp_path):
    session, ws = make_session(tmp_path)
    await session.receive_audio(b"bad")
    await session.receive_audio(bytes(FRAME_BYTES))
    assert ws.messages[0][1]["code"] == "bad_audio_frame"
    await wait_for(lambda: len(session.cloud.audio) == 1)
    assert session.cloud.audio == [bytes(FRAME_BYTES)]
    await session._stop_input_sender()


async def test_silent_audio_transport_does_not_reset_idle_timer(tmp_path):
    session, _ = make_session(tmp_path)
    previous_activity = session.last_activity

    await session.receive_audio(bytes(FRAME_BYTES))

    assert session.last_activity == previous_activity
    await session._stop_input_sender()


async def test_echo_guard_withholds_playback_audio_then_releases(tmp_path):
    session, _ = make_session(tmp_path)
    session.output = OutputStream("stream", "response", "item", 0)
    frame = bytes(FRAME_BYTES)

    await session.receive_audio(frame)
    assert session.cloud.audio == []
    assert session.echo_suppressed_frames == 1

    session.output = None
    session.echo_gate_until = 0
    await session.receive_audio(frame)
    await wait_for(lambda: len(session.cloud.audio) == 1)
    assert session.cloud.audio == [frame]
    assert not session.echo_gate_active
    await session._stop_input_sender()


async def test_audio_is_buffered_until_cloud_is_ready_and_keeps_order(tmp_path):
    session, _ = make_session(tmp_path)
    await session._stop_input_sender()
    cloud = session.cloud
    session.cloud_ready = False
    first = b"\x01" + bytes(FRAME_BYTES - 1)
    second = b"\x02" + bytes(FRAME_BYTES - 1)

    await session.receive_audio(first)
    await session.receive_audio(second)

    assert cloud.audio == []
    assert session.input_queue.qsize() == 2
    session.cloud_ready = True
    session._start_input_sender()
    await wait_for(lambda: len(cloud.audio) == 2)
    assert cloud.audio == [first, second]
    await session._stop_input_sender()


async def test_input_queue_is_bounded_and_retains_newest_audio(tmp_path):
    session, _ = make_session(tmp_path)
    session.cloud_ready = False

    for index in range(MAX_QUEUED_INPUT_FRAMES + 2):
        frame = bytes([index % 256]) + bytes(FRAME_BYTES - 1)
        await session.receive_audio(frame)

    assert session.input_queue.qsize() == MAX_QUEUED_INPUT_FRAMES
    assert session.input_dropped_frames == 2
    oldest_retained = session.input_queue.get_nowait()
    assert oldest_retained[0] == 2
    await session._stop_input_sender()


async def test_request_start_does_not_block_device_ingestion(tmp_path):
    session, _ = make_session(tmp_path)
    await session._stop_input_sender()
    session.cloud = None
    session.cloud_ready = False
    gate = asyncio.Event()

    async def delayed_start(_project_id):
        await gate.wait()

    session.start = delayed_start
    await session.request_start(None)
    frame = bytes(FRAME_BYTES)
    await session.receive_audio(frame)

    assert session.start_task is not None
    assert not session.start_task.done()
    assert session.input_queue.get_nowait() == frame
    await session.close()
    assert session.start_task is None


async def wait_for(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition was not reached")


async def test_barge_in_flushes_truncates_and_rejects_late_audio(tmp_path):
    session, ws = make_session(tmp_path)
    session.settings = session.settings.model_copy(update={"barge_in_enabled": True})
    session._start_playback_sender()
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
    await wait_for(lambda: session.output.sent_ms == 20)
    await session.playback_progress(stream_id, 20)
    await session.handle_openai_event({"type": "input_audio_buffer.speech_started"})

    assert any(message[1]["type"] == "playback.flush" for message in ws.messages if message[0] == "json")
    assert session.cloud.cancelled == 0
    assert session.cloud.truncations == [("item-1", 0, 20)]
    assert session.state == DeviceState.LISTENING

    binary_count = sum(kind == "bytes" for kind, _ in ws.messages)
    await session.handle_openai_event(event)  # late server event after cancellation
    assert sum(kind == "bytes" for kind, _ in ws.messages) == binary_count
    assert session.output is None
    await session._stop_playback_sender()


async def test_output_is_chunked_and_padded_to_twenty_ms(tmp_path):
    session, ws = make_session(tmp_path)
    session._start_playback_sender()
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
    await wait_for(lambda: session.output.ended)
    frames = [value for kind, value in ws.messages if kind == "bytes"]
    assert len(frames) == 1
    assert len(frames[0]) == FRAME_BYTES
    await session._stop_playback_sender()


async def test_response_done_finishes_audio_without_audio_done_event(tmp_path):
    session, ws = make_session(tmp_path)
    session._start_playback_sender()
    await session.handle_openai_event(
        {
            "type": "response.output_audio.delta",
            "response_id": "r",
            "item_id": "i",
            "content_index": 0,
            "delta": base64.b64encode(bytes(100)).decode(),
        }
    )

    await session.handle_openai_event({"type": "response.done", "response": {}})
    await wait_for(lambda: session.output.ended)

    assert session.output.end_queued
    assert any(
        kind == "json" and value["type"] == "playback.end"
        for kind, value in ws.messages
    )
    await session._stop_playback_sender()


async def test_stalled_playback_completion_recovers_listening_state(tmp_path):
    session, ws = make_session(tmp_path)
    session.state = DeviceState.SPEAKING
    session.output = OutputStream(
        "stream", "response", "item", 0, sent_ms=1000, played_ms=900, ended=True
    )

    await session._complete_playback("watchdog")

    assert session.output is None
    assert session.state == DeviceState.LISTENING
    assert ws.messages[-1][1]["state"] == "listening"


async def test_output_frames_are_paced_at_media_rate(tmp_path):
    session, ws = make_session(tmp_path)
    session._start_playback_sender()
    audio = bytes(FRAME_BYTES * 3)
    started = asyncio.get_running_loop().time()
    await session.handle_openai_event(
        {
            "type": "response.output_audio.delta",
            "response_id": "r",
            "item_id": "i",
            "content_index": 0,
            "delta": base64.b64encode(audio).decode(),
        }
    )
    await wait_for(lambda: session.output.sent_ms == 60)
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed >= 0.035
    assert len([value for kind, value in ws.messages if kind == "bytes"]) == 3
    await session._stop_playback_sender()


async def test_cancel_not_active_race_is_not_fatal(tmp_path):
    session, _ = make_session(tmp_path)
    session.state = DeviceState.SPEAKING
    await session.handle_openai_event(
        {
            "type": "error",
            "error": {"code": "response_cancel_not_active", "message": "already stopped"},
        }
    )
    assert session.state == DeviceState.SPEAKING


async def test_disconnect_cleanup_does_not_write_closed_device(tmp_path):
    session, ws = make_session(tmp_path)
    cloud = session.cloud
    session_id, project_id = session.session_id, session.project_id
    planner = FakePlanner()
    published = []

    async def observe(message):
        published.append(message)

    session.planner = planner
    session.observer = observe
    ws.disconnected = True

    await session.close()
    await asyncio.sleep(0)

    assert ws.messages == []
    assert cloud.closed
    assert session.cloud is None
    assert session.state == DeviceState.IDLE
    assert [message["type"] for message in published] == ["state", "session.ended"]
    assert planner.updates == [(project_id, session_id)]
    with session.db.connect() as connection:
        ended = connection.execute(
            "SELECT end_reason, ended_at FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
    assert ended["end_reason"] == "device_disconnect"
    assert ended["ended_at"] is not None
