import asyncio
import base64
import io
import logging
import math
from fastapi import WebSocketDisconnect

from gateway.config import Settings
from gateway.db import Database
from gateway.planner import Planner
from gateway.protocol import FRAME_BYTES, DeviceState
from gateway.session import (
    BARGE_IN_LEVEL_WINDOW_FRAMES,
    BARGE_IN_WARMUP_MS,
    LISTENING_LEVEL_MIN_ACTIVE_FRAMES,
    LISTENING_LEVEL_RELEASE_FRAMES,
    LISTENING_LEVEL_WINDOW_FRAMES,
    MAX_DEVICE_PLAYBACK_LEAD_MS,
    MAX_QUEUED_INPUT_FRAMES,
    PLAYBACK_COMPLETION_TOLERANCE_MS,
    PLAYBACK_FRAMES_PER_SECOND,
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
        self.tool_outputs = []
        self.response_requests = []

    async def append_audio(self, pcm):
        self.audio.append(pcm)

    async def cancel_response(self):
        self.cancelled += 1

    async def truncate(self, item_id, content_index, played_ms):
        self.truncations.append((item_id, content_index, played_ms))

    async def close(self):
        self.closed = True

    async def submit_tool_output(self, call_id, output):
        self.tool_outputs.append((call_id, output))

    async def request_response(self, instructions=None):
        self.response_requests.append(instructions)


class FakeRealtime(FakeCloud):
    def __init__(self, settings, instructions, on_event, ready_keyword_enabled=False):
        super().__init__()
        self.settings = settings
        self.instructions = instructions
        self.on_event = on_event
        self.ready_keyword_enabled = ready_keyword_enabled

    async def connect(self):
        pass


class FakePlanner:
    def __init__(self):
        self.updates = []

    async def update_after_session(self, project_id, session_id):
        self.updates.append((project_id, session_id))
        return True


def make_session(tmp_path, **setting_overrides):
    setting_values = {
        "device_token": "device-secret",
        "ui_token": "browser-secret",
        "database_path": tmp_path / "test.db",
        "idle_timeout_seconds": 30,
        "listening_rms_threshold": 0,
        **setting_overrides,
    }
    settings = Settings(**setting_values)
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


async def test_ready_word_wait_gates_audio_until_local_detection(tmp_path):
    settings = Settings(
        device_token="device-secret",
        ui_token="browser-secret",
        database_path=tmp_path / "test.db",
    )
    db = Database(settings.database_path)
    db.initialize()
    ws = FakeWebSocket()
    session = DeviceSession(
        ws,
        "device",
        settings,
        db,
        Planner(settings, db),
        capabilities={"ready_keyword": True},
    )
    session.session_id = db.start_session(db.get_project()["id"], "device", "test")
    session.project_id = db.get_project()["id"]
    cloud = FakeCloud()
    session.cloud = cloud
    session.accepting_audio = True
    session.cloud_ready = True
    session.output = OutputStream("stream", "response", "item", 0)

    await session._handle_tool_call(
        {"name": "wait_for_ready", "call_id": "ready-call", "arguments": "{}"}
    )

    assert session.ready_wait_requested
    assert not session.waiting_for_ready
    assert not any(value["type"] == "keyword.mode" for kind, value in ws.messages if kind == "json")

    await session._complete_playback("test")

    assert session.waiting_for_ready
    assert not session.accepting_audio
    assert ws.messages[-1][1] == {
        "v": 1,
        "type": "keyword.mode",
        "device_id": "device",
        "mode": "ready",
    }

    await session.ready_detected()

    assert not session.waiting_for_ready
    assert session.accepting_audio
    assert cloud.tool_outputs == [("ready-call", {"ready": True})]
    assert cloud.response_requests == [None]
    assert ws.messages[-1][1]["mode"] == "wake"


async def test_unavailable_ready_command_returns_device_capability_error(tmp_path):
    session, _ = make_session(tmp_path)

    await session._handle_tool_call(
        {"name": "wait_for_ready", "call_id": "ready-call", "arguments": "{}"}
    )

    assert session.cloud.tool_outputs == [
        ("ready-call", {"ready": False, "error": "The device has no ready-word model."})
    ]
    assert session.cloud.response_requests == [None]
    assert not session.waiting_for_ready


async def test_unknown_voice_command_is_rejected_without_device_action(tmp_path):
    session, ws = make_session(tmp_path)

    await session._handle_tool_call(
        {"name": "nonexistent_command", "call_id": "bad-call", "arguments": "{}"}
    )

    assert session.cloud.tool_outputs == [
        ("bad-call", {"ok": False, "error": "Unknown command."})
    ]
    assert session.cloud.response_requests == [None]
    assert ws.messages == []


async def test_end_session_voice_command_uses_spoken_stop(tmp_path):
    session, _ = make_session(tmp_path)
    reasons = []

    async def record_stop(reason):
        reasons.append(reason)

    session.stop = record_stop
    await session._handle_tool_call(
        {"name": "end_session", "call_id": "stop-call", "arguments": "{}"}
    )

    assert reasons == ["spoken_stop"]


def test_playback_queue_uses_configured_bounded_duration(tmp_path):
    session, _ = make_session(tmp_path, playback_buffer_seconds=45)

    assert session.playback_queue.maxsize == 45 * PLAYBACK_FRAMES_PER_SECOND


async def test_frame_boundaries_are_enforced(tmp_path):
    session, ws = make_session(tmp_path)
    await session.receive_audio(b"bad")
    await session.receive_audio(bytes(FRAME_BYTES))
    assert ws.messages[0][1]["code"] == "bad_audio_frame"
    await wait_for(lambda: len(session.cloud.audio) == 1)
    assert session.cloud.audio == [bytes(FRAME_BYTES)]
    await session._stop_input_sender()


async def test_runtime_volume_is_reported_and_controlled(tmp_path):
    session, ws = make_session(tmp_path)
    await session.report_volume(0.125)

    result = await session.control_volume("increase")

    assert result == {"ok": True, "level_percent": 18}
    assert session.device_volume == 0.175
    assert ws.messages[-1][1]["type"] == "volume.set"
    assert ws.messages[-1][1]["level"] == 0.175


async def test_voice_volume_tool_returns_result_to_realtime(tmp_path):
    session, _ = make_session(tmp_path)
    await session.report_volume(0.2)
    cloud = session.cloud

    await session._handle_tool_call(
        {
            "name": "control_volume",
            "call_id": "volume-call",
            "arguments": '{"action":"set","level_percent":30}',
        }
    )

    assert cloud.tool_outputs == [("volume-call", {"ok": True, "level_percent": 30})]
    assert cloud.response_requests == [None]
    assert session.device_volume == 0.3


async def test_web_search_runs_off_realtime_reader_and_publishes_sources(monkeypatch, tmp_path):
    session, ws = make_session(tmp_path)
    published = []

    async def observer(message):
        published.append(message)

    async def fake_search(_settings, query):
        assert query == "weather today"
        await asyncio.sleep(0)
        return {"answer": "Sunny [1]", "citations": [
            {"start": 6, "end": 9, "url": "https://weather.example", "title": "Forecast"}
        ]}

    monkeypatch.setattr("gateway.session.search_web", fake_search)
    session.observer = observer
    cloud = session.cloud
    await session._handle_tool_call({
        "name": "search_web", "call_id": "search-call",
        "arguments": '{"query":"weather today"}',
    })
    assert not cloud.tool_outputs  # The reader has already returned to receiving events.
    await asyncio.gather(*tuple(session.search_tasks))

    assert cloud.tool_outputs == [("search-call", {
        "ok": True, "answer": "Sunny [1]",
        "sources": [{"url": "https://weather.example", "title": "Forecast"}],
    })]
    assert cloud.response_requests == [None]
    assert ws.messages == []
    assert published[-1]["type"] == "search.result"
    assert published[-1]["citations"][0]["start"] == 6


async def test_web_search_is_cancelled_on_session_stop(monkeypatch, tmp_path):
    session, _ = make_session(tmp_path)
    started = asyncio.Event()

    async def slow_search(_settings, _query):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("gateway.session.search_web", slow_search)
    cloud = session.cloud
    await session._handle_tool_call({
        "name": "search_web", "call_id": "search-call", "arguments": '{"query":"latest news"}',
    })
    await started.wait()
    await session.stop("button")

    assert not session.search_tasks
    assert cloud.closed
    assert cloud.tool_outputs == []


async def test_web_search_error_returns_to_realtime_without_ui_result(monkeypatch, tmp_path):
    from gateway.web_search import WebSearchError

    session, _ = make_session(tmp_path)
    published = []

    async def observer(message):
        published.append(message)

    async def failed_search(_settings, _query):
        raise WebSearchError("No sourced answer was found.")

    monkeypatch.setattr("gateway.session.search_web", failed_search)
    session.observer = observer
    await session._handle_tool_call({
        "name": "search_web", "call_id": "search-call", "arguments": '{"query":"obscure query"}',
    })
    await asyncio.gather(*tuple(session.search_tasks))

    assert session.cloud.tool_outputs == [
        ("search-call", {"ok": False, "error": "No sourced answer was found."})
    ]
    assert session.cloud.response_requests == [None]
    assert published == []


async def test_web_search_rejects_missing_query_without_network(tmp_path):
    session, _ = make_session(tmp_path)
    await session._handle_tool_call({
        "name": "search_web", "call_id": "search-call", "arguments": '{}',
    })
    assert not session.search_tasks
    assert session.cloud.tool_outputs == [
        ("search-call", {"ok": False, "error": "A search query is required."})
    ]


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


async def test_barge_in_warmup_sends_silence_until_one_second_played(tmp_path):
    session, _ = make_session(
        tmp_path, barge_in_enabled=True, barge_in_rms_threshold=0
    )
    session.output = OutputStream("stream", "response", "item", 0, sent_ms=1200)
    session.diagnostic_input = io.BytesIO()
    frame = b"\x01\x02" * (FRAME_BYTES // 2)

    await session.receive_audio(frame)
    await wait_for(lambda: len(session.cloud.audio) == 1)
    assert session.cloud.audio == [bytes(FRAME_BYTES)]
    assert session.diagnostic_input.getvalue() == frame

    await session.playback_progress("stream", BARGE_IN_WARMUP_MS - 20)
    await session.receive_audio(frame)
    await wait_for(lambda: len(session.cloud.audio) == 2)
    assert session.cloud.audio[-1] == bytes(FRAME_BYTES)

    await session.playback_progress("stream", BARGE_IN_WARMUP_MS)
    await session.receive_audio(frame)
    await wait_for(lambda: len(session.cloud.audio) == 3)
    assert session.cloud.audio[-1] == frame
    assert session.diagnostic_input.getvalue() == frame * 3
    await session._stop_input_sender()


async def test_barge_in_level_gate_rejects_echo_and_preserves_speech_preroll(tmp_path):
    session, _ = make_session(tmp_path, barge_in_enabled=True)
    session.output = OutputStream("stream", "response", "item", 0, sent_ms=2000)
    await session.playback_progress("stream", BARGE_IN_WARMUP_MS)
    session.diagnostic_input = io.BytesIO()
    echo = (2000).to_bytes(2, "little", signed=True) * (FRAME_BYTES // 2)
    speech = (10000).to_bytes(2, "little", signed=True) * (FRAME_BYTES // 2)

    for _ in range(BARGE_IN_LEVEL_WINDOW_FRAMES):
        await session.receive_audio(echo)
    await wait_for(lambda: len(session.cloud.audio) == BARGE_IN_LEVEL_WINDOW_FRAMES)
    assert session.cloud.audio == [bytes(FRAME_BYTES)] * BARGE_IN_LEVEL_WINDOW_FRAMES
    assert not session.barge_gate_open

    for _ in range(BARGE_IN_LEVEL_WINDOW_FRAMES):
        await session.receive_audio(speech)
    await wait_for(lambda: len(session.cloud.audio) == 3 * BARGE_IN_LEVEL_WINDOW_FRAMES - 1)
    assert session.cloud.audio[-BARGE_IN_LEVEL_WINDOW_FRAMES:] == [speech] * BARGE_IN_LEVEL_WINDOW_FRAMES
    assert session.barge_gate_open
    assert session.diagnostic_input.getvalue() == (
        echo * BARGE_IN_LEVEL_WINDOW_FRAMES
        + speech * BARGE_IN_LEVEL_WINDOW_FRAMES
    )

    await session.receive_audio(echo)
    await wait_for(lambda: len(session.cloud.audio) == 3 * BARGE_IN_LEVEL_WINDOW_FRAMES)
    assert session.cloud.audio[-1] == echo  # The gate stays open for this reply.

    await session._complete_playback("test")
    assert not session.barge_gate_open
    assert not session.barge_gate_frames
    await session.receive_audio(echo)
    await wait_for(lambda: len(session.cloud.audio) == 3 * BARGE_IN_LEVEL_WINDOW_FRAMES + 1)
    assert session.cloud.audio[-1] == echo  # Normal listening has no level gate.
    await session._stop_input_sender()


async def test_barge_in_level_gate_ignores_a_single_full_scale_spike(tmp_path):
    session, _ = make_session(tmp_path, barge_in_enabled=True)
    session.output = OutputStream("stream", "response", "item", 0, sent_ms=2000)
    await session.playback_progress("stream", BARGE_IN_WARMUP_MS)
    spike = (32767).to_bytes(2, "little", signed=True) * (FRAME_BYTES // 2)
    for _ in range(BARGE_IN_LEVEL_WINDOW_FRAMES - 1):
        await session.receive_audio(bytes(FRAME_BYTES))
    await session.receive_audio(spike)

    await wait_for(lambda: len(session.cloud.audio) == BARGE_IN_LEVEL_WINDOW_FRAMES)
    assert not session.barge_gate_open
    assert session.cloud.audio == [bytes(FRAME_BYTES)] * BARGE_IN_LEVEL_WINDOW_FRAMES
    await session._stop_input_sender()


async def test_barge_in_guard_log_distinguishes_partial_peak_from_full_window(tmp_path, caplog):
    session, _ = make_session(
        tmp_path, barge_in_enabled=True, barge_in_rms_threshold=12000
    )
    session.output = OutputStream("stream", "response", "item", 0, sent_ms=2000)
    await session.playback_progress("stream", BARGE_IN_WARMUP_MS)
    spike = (32767).to_bytes(2, "little", signed=True) * (FRAME_BYTES // 2)
    with caplog.at_level(logging.INFO, logger="gateway.session"):
        await session.receive_audio(spike)
        for _ in range(BARGE_IN_LEVEL_WINDOW_FRAMES - 1):
            await session.receive_audio(bytes(FRAME_BYTES))
        session._reset_barge_in_level_gate()

    full_window_rms = math.isqrt(32767**2 // BARGE_IN_LEVEL_WINDOW_FRAMES)
    assert f"max_full_window_rms={full_window_rms}" in caplog.text
    assert f"margin={full_window_rms - 12000:+d}" in caplog.text
    assert "full_windows=1 window_ms=400 max_partial_rms=32767" in caplog.text
    assert "Barge-in level qualified" not in caplog.text
    await session._stop_input_sender()


async def test_barge_in_qualification_log_shows_threshold_margin(tmp_path, caplog):
    session, _ = make_session(
        tmp_path, barge_in_enabled=True, barge_in_rms_threshold=12000
    )
    session.output = OutputStream("stream", "response", "item", 0, sent_ms=2000)
    await session.playback_progress("stream", BARGE_IN_WARMUP_MS)
    speech = (13000).to_bytes(2, "little", signed=True) * (FRAME_BYTES // 2)
    with caplog.at_level(logging.INFO, logger="gateway.session"):
        for _ in range(BARGE_IN_LEVEL_WINDOW_FRAMES):
            await session.receive_audio(speech)

    assert session.barge_gate_open
    assert "rolling_rms=13000 threshold=12000 margin=+1000 window_ms=400" in caplog.text
    await session._stop_input_sender()


async def test_listening_gate_rejects_quiet_voice_and_click_then_passes_loud_speech(tmp_path):
    session, _ = make_session(tmp_path, listening_rms_threshold=800)
    session.diagnostic_input = io.BytesIO()
    quiet = (300).to_bytes(2, "little", signed=True) * (FRAME_BYTES // 2)
    click = (32767).to_bytes(2, "little", signed=True) * (FRAME_BYTES // 2)
    speech = (2000).to_bytes(2, "little", signed=True) * (FRAME_BYTES // 2)

    for _ in range(LISTENING_LEVEL_WINDOW_FRAMES):
        await session.receive_audio(quiet)
    await session.receive_audio(click)
    for _ in range(LISTENING_LEVEL_WINDOW_FRAMES):
        await session.receive_audio(quiet)
    await wait_for(lambda: len(session.cloud.audio) == 2 * LISTENING_LEVEL_WINDOW_FRAMES + 1)
    assert not session.listening_gate_open
    assert session.cloud.audio == [bytes(FRAME_BYTES)] * len(session.cloud.audio)

    for _ in range(LISTENING_LEVEL_WINDOW_FRAMES):
        await session.receive_audio(speech)
    await wait_for(lambda: len(session.cloud.audio) == 3 * LISTENING_LEVEL_WINDOW_FRAMES + 8)
    assert session.listening_gate_open
    assert session.cloud.audio[-LISTENING_LEVEL_WINDOW_FRAMES:] == [speech] * LISTENING_LEVEL_WINDOW_FRAMES
    assert session.diagnostic_input.getvalue() == (
        quiet * LISTENING_LEVEL_WINDOW_FRAMES
        + click
        + quiet * LISTENING_LEVEL_WINDOW_FRAMES
        + speech * LISTENING_LEVEL_WINDOW_FRAMES
    )

    for _ in range(LISTENING_LEVEL_RELEASE_FRAMES):
        await session.receive_audio(quiet)
    assert not session.listening_gate_open
    await session.receive_audio(quiet)
    await wait_for(lambda: session.cloud.audio[-1] == bytes(FRAME_BYTES))
    await session._stop_input_sender()


async def test_listening_gate_requires_sustained_level_not_peak(tmp_path):
    session, _ = make_session(tmp_path, listening_rms_threshold=800)
    loud = (2000).to_bytes(2, "little", signed=True) * (FRAME_BYTES // 2)
    for _ in range(LISTENING_LEVEL_MIN_ACTIVE_FRAMES - 1):
        await session.receive_audio(loud)
    for _ in range(LISTENING_LEVEL_WINDOW_FRAMES):
        await session.receive_audio(bytes(FRAME_BYTES))
    assert not session.listening_gate_open
    assert all(frame == bytes(FRAME_BYTES) for frame in session.cloud.audio)
    await session._stop_input_sender()


async def test_listening_guard_log_uses_sixth_highest_frame_not_click_peak(tmp_path, caplog):
    session, _ = make_session(tmp_path, listening_rms_threshold=800)
    click = (32767).to_bytes(2, "little", signed=True) * (FRAME_BYTES // 2)
    quiet = (300).to_bytes(2, "little", signed=True) * (FRAME_BYTES // 2)
    with caplog.at_level(logging.INFO, logger="gateway.session"):
        await session.receive_audio(click)
        for _ in range(LISTENING_LEVEL_WINDOW_FRAMES - 1):
            await session.receive_audio(quiet)
        session._reset_listening_level_gate()

    assert "max_decision_rms=300 threshold=800 margin=-500" in caplog.text
    assert "max_active_frames=1 required_active_frames=6" in caplog.text
    assert "max_frame_rms=32767 full_windows=1" in caplog.text
    assert "Listening level qualified" not in caplog.text
    await session._stop_input_sender()


async def test_listening_qualification_log_shows_decision_margin(tmp_path, caplog):
    session, _ = make_session(tmp_path, listening_rms_threshold=800)
    loud = (900).to_bytes(2, "little", signed=True) * (FRAME_BYTES // 2)
    quiet = (300).to_bytes(2, "little", signed=True) * (FRAME_BYTES // 2)
    with caplog.at_level(logging.INFO, logger="gateway.session"):
        for _ in range(LISTENING_LEVEL_MIN_ACTIVE_FRAMES):
            await session.receive_audio(loud)
        for _ in range(LISTENING_LEVEL_WINDOW_FRAMES - LISTENING_LEVEL_MIN_ACTIVE_FRAMES):
            await session.receive_audio(quiet)

    assert session.listening_gate_open
    assert (
        "decision_rms=900 threshold=800 margin=+100 active_frames=6 "
        "required_active_frames=6 window_ms=160"
    ) in caplog.text
    await session._stop_input_sender()


async def test_barge_in_warmup_ignores_vad_start_and_matching_stop(tmp_path):
    session, ws = make_session(tmp_path, barge_in_enabled=True)
    session.output = OutputStream("stream", "response", "item", 0, sent_ms=1200)

    await session.handle_openai_event(
        {"type": "input_audio_buffer.speech_started", "item_id": "warmup-item"}
    )
    assert session.output is not None
    assert session.cloud.truncations == []
    await session.handle_openai_event(
        {"type": "input_audio_buffer.speech_stopped", "item_id": "warmup-item"}
    )
    assert session.state == DeviceState.IDLE
    assert "warmup-item" not in session.warmup_vad_items

    await session.playback_progress("stream", BARGE_IN_WARMUP_MS)
    await session.handle_openai_event(
        {"type": "input_audio_buffer.speech_started", "item_id": "later-item"}
    )
    assert session.output is None
    assert session.cloud.truncations == [("item", 0, BARGE_IN_WARMUP_MS)]
    assert any(
        message[1]["type"] == "playback.flush" for message in ws.messages if message[0] == "json"
    )


async def test_project_announcement_cannot_barge_into_itself(tmp_path):
    session, _ = make_session(tmp_path, barge_in_enabled=True)
    frame = bytes(FRAME_BYTES)

    await session._announce_project("First project")

    assert session.announcement_echo_guard
    assert session.cloud.response_requests == [
        "Say only: First project is active. Then stop speaking and wait. "
        "Do not ask a question or suggest activities."
    ]
    await session.receive_audio(frame)
    assert session.cloud.audio == []

    session.output = OutputStream("stream", "response", "item", 0)
    await session.handle_openai_event({"type": "input_audio_buffer.speech_started"})
    assert session.output is not None

    await session._complete_playback("test")
    assert not session.announcement_echo_guard
    await session.receive_audio(frame)
    assert session.cloud.audio == []

    session.echo_gate_until = 0
    await session.receive_audio(frame)
    await wait_for(lambda: len(session.cloud.audio) == 1)
    assert session.cloud.audio == [frame]
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


async def test_session_reloads_diagnostic_setting_and_creates_recordings(
    tmp_path, monkeypatch
):
    settings = Settings(
        device_token="device-secret",
        ui_token="browser-secret",
        database_path=tmp_path / "test.db",
        diagnostic_audio=False,
    )
    db = Database(settings.database_path)
    db.initialize()
    db.update_settings({"diagnostic_audio": True})
    ws = FakeWebSocket()
    planner = FakePlanner()
    session = DeviceSession(ws, "device", settings, db, planner)
    monkeypatch.setattr("gateway.session.RealtimeConnection", FakeRealtime)

    await session.start(None)

    assert session.settings.diagnostic_audio is True
    assert session.diagnostic_input is not None
    assert session.diagnostic_output is not None
    session_id = session.session_id
    await session.stop("test")
    await asyncio.sleep(0)
    diagnostic_dir = settings.database_path.parent / "diagnostic-audio"
    assert (diagnostic_dir / f"session-{session_id}-input.pcm").exists()
    assert (diagnostic_dir / f"session-{session_id}-output.pcm").exists()


async def test_xvf_gain_is_sent_at_session_start_only_to_supported_device(
    tmp_path, monkeypatch
):
    settings = Settings(
        device_token="device-secret",
        ui_token="browser-secret",
        database_path=tmp_path / "test.db",
        announce_active_project=False,
    )
    db = Database(settings.database_path)
    db.initialize()
    db.update_settings({"xvf_agc_ch0_gain": 18.0})
    monkeypatch.setattr("gateway.session.RealtimeConnection", FakeRealtime)

    supported_socket = FakeWebSocket()
    supported = DeviceSession(
        supported_socket, "xvf", settings, db, FakePlanner(),
        capabilities={"xvf_agc_ch0": True},
    )
    await supported.start(None)
    assert any(
        value["type"] == "mic_gain.set" and value["ch0_gain"] == 18.0
        for kind, value in supported_socket.messages if kind == "json"
    )
    await supported.stop("test")

    unsupported_socket = FakeWebSocket()
    unsupported = DeviceSession(unsupported_socket, "seeed", settings, db, FakePlanner())
    await unsupported.start(None)
    assert not any(
        value["type"] == "mic_gain.set"
        for kind, value in unsupported_socket.messages if kind == "json"
    )
    await unsupported.stop("test")


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
    session.output.sent_ms = BARGE_IN_WARMUP_MS
    await session.playback_progress(stream_id, BARGE_IN_WARMUP_MS)
    await session.handle_openai_event({"type": "input_audio_buffer.speech_started"})

    assert any(message[1]["type"] == "playback.flush" for message in ws.messages if message[0] == "json")
    assert session.cloud.cancelled == 0
    assert session.cloud.truncations == [("item-1", 0, BARGE_IN_WARMUP_MS)]
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


def test_zero_idle_timeout_keeps_listening_session_open(tmp_path):
    session, _ = make_session(tmp_path, idle_timeout_seconds=0)
    session.state = DeviceState.LISTENING
    session.last_activity = 0

    assert not session._idle_timeout_expired(10_000)


def test_nonzero_idle_timeout_expires_listening_session(tmp_path):
    session, _ = make_session(tmp_path, idle_timeout_seconds=30)
    session.state = DeviceState.LISTENING
    session.last_activity = 100

    assert session._idle_timeout_expired(130)


async def test_output_flow_control_waits_for_device_playback_progress(tmp_path):
    session, _ = make_session(tmp_path)
    session.output = OutputStream(
        "stream", "response", "item", 0, sent_ms=MAX_DEVICE_PLAYBACK_LEAD_MS
    )

    waiter = asyncio.create_task(session._wait_for_playback_capacity("stream"))
    await asyncio.sleep(0)
    assert not waiter.done()

    await session.playback_progress("stream", 20)
    await waiter


async def test_playback_completes_within_one_transport_frame_tolerance(tmp_path):
    session, _ = make_session(tmp_path)
    session.output = OutputStream(
        "stream",
        "response",
        "item",
        0,
        sent_ms=2060,
        ended=True,
    )

    await session.playback_progress(
        "stream", 2060 - PLAYBACK_COMPLETION_TOLERANCE_MS
    )

    assert session.output is None
    assert session.state == DeviceState.LISTENING


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


async def test_list_projects_tool_returns_names_and_active_project(tmp_path):
    session, _ = make_session(tmp_path)
    session.db.create_project("Second project")

    await session.handle_openai_event(
        {
            "type": "response.function_call_arguments.done",
            "name": "list_projects",
            "call_id": "call-1",
            "arguments": "{}",
        }
    )

    call_id, output = session.cloud.tool_outputs[0]
    assert call_id == "call-1"
    assert output["active_project"] == "First project"
    assert set(output["projects"]) == {"First project", "Second project"}
    assert session.cloud.response_requests == [None]


async def test_switch_project_opens_clean_context_on_same_device_socket(
    tmp_path, monkeypatch
):
    session, ws = make_session(tmp_path)
    planner = FakePlanner()
    session.planner = planner
    old_cloud = session.cloud
    old_session_id = session.session_id
    old_project_id = session.project_id
    second = session.db.create_project("Second project", "A separate goal")
    monkeypatch.setattr("gateway.session.RealtimeConnection", FakeRealtime)

    await session.handle_openai_event(
        {
            "type": "response.function_call_arguments.done",
            "name": "switch_project",
            "call_id": "call-2",
            "arguments": '{"project_name":"second project"}',
        }
    )
    await asyncio.sleep(0)

    assert old_cloud.closed
    assert session.db.get_project()["id"] == second["id"]
    assert session.project_id == second["id"]
    assert session.session_id != old_session_id
    assert "Project: Second project" in session.cloud.instructions
    announcement = session.cloud.response_requests[0]
    assert announcement == (
        "Say only: Second project is active. Then stop speaking and wait. "
        "Do not ask a question or suggest activities."
    )
    assert planner.updates == [(old_project_id, old_session_id)]
    assert any(
        kind == "json" and value["type"] == "session.started"
        and value["project_id"] == second["id"]
        for kind, value in ws.messages
    )
    with session.db.connect() as connection:
        old = connection.execute(
            "SELECT end_reason FROM sessions WHERE id=?", (old_session_id,)
        ).fetchone()
    assert old["end_reason"] == "project_switch"
    session.timer_task.cancel()
    await asyncio.gather(session.timer_task, return_exceptions=True)
    await session._stop_input_sender()
    await session._stop_playback_sender()


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
