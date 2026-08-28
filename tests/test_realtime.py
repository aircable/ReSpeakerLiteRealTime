import base64
import logging

from gateway.config import Settings
from gateway.realtime import RealtimeConnection


async def ignore_event(_event):
    pass


def make_connection(tmp_path):
    settings = Settings(
        device_token="device-secret",
        ui_token="browser-secret",
        database_path=tmp_path / "test.db",
        openai_trace=True,
    )
    return RealtimeConnection(settings, "private instructions", ignore_event)


def test_trace_metadata_excludes_content_and_credentials(tmp_path):
    connection = make_connection(tmp_path)
    event = {
        "type": "session.update",
        "event_id": "event-1",
        "audio": "private base64 audio",
        "transcript": "private transcript",
        "instructions": "private instructions",
        "api_key": "private key",
    }

    assert connection._safe_event(event) == {
        "type": "session.update",
        "event_id": "event-1",
    }


def test_server_vad_configuration(tmp_path):
    connection = make_connection(tmp_path)
    connection.settings = connection.settings.model_copy(
        update={
            "vad_mode": "server_vad",
            "vad_threshold": 0.55,
            "vad_prefix_padding_ms": 250,
            "vad_silence_duration_ms": 600,
            "barge_in_enabled": True,
        }
    )

    assert connection._turn_detection_config() == {
        "type": "server_vad",
        "threshold": 0.55,
        "prefix_padding_ms": 250,
        "silence_duration_ms": 600,
        "create_response": True,
        "interrupt_response": True,
    }


def test_trace_counts_audio_and_response_usage_without_logging_audio(tmp_path, caplog):
    connection = make_connection(tmp_path)
    audio = base64.b64encode(bytes(960)).decode()
    caplog.set_level(logging.INFO, logger="gateway.realtime")

    connection._trace_incoming(
        {
            "type": "response.output_audio.delta",
            "response_id": "response-1",
            "delta": audio,
        }
    )
    connection._trace_incoming(
        {
            "type": "response.done",
            "response": {
                "id": "response-1",
                "status": "completed",
                "usage": {"input_tokens": 12, "output_tokens": 7},
            },
        }
    )

    assert connection.output_audio_bytes == 960
    assert connection.total_input_tokens == 12
    assert connection.total_output_tokens == 7
    assert audio not in caplog.text
