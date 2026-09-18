import pytest
from pydantic import ValidationError

from gateway.protocol import FRAME_BYTES, Authenticate, PlaybackProgress, VolumeChanged, parse_device_message


def test_audio_frame_is_exactly_twenty_ms_pcm16():
    assert FRAME_BYTES == 960


def test_parse_versioned_authentication():
    message = parse_device_message(
        {"v": 1, "type": "auth", "token": "secret", "device_id": "desk", "capabilities": {}}
    )
    assert isinstance(message, Authenticate)
    assert message.device_id == "desk"


def test_rejects_wrong_protocol_version():
    with pytest.raises(ValidationError):
        parse_device_message({"v": 2, "type": "playback.progress", "stream_id": "x", "played_ms": 0})


def test_rejects_negative_playback_progress():
    with pytest.raises(ValidationError):
        PlaybackProgress(type="playback.progress", stream_id="x", played_ms=-1)


def test_parse_runtime_volume_report():
    message = parse_device_message({"v": 1, "type": "volume.changed", "level": 0.125})
    assert isinstance(message, VolumeChanged)
    assert message.level == 0.125
