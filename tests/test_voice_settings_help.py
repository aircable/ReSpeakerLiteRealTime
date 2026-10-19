from pathlib import Path


def test_voice_settings_help_is_in_packaged_gateway_page():
    page = (Path(__file__).resolve().parents[1] / "gateway/static/index.html").read_text()
    assert '<details id="voiceSettingsHelp"' in page
    assert "Help and starting values" in page
    assert "3000 worked in one test on this setup" in page
    assert "This is separate from the gateway RMS gates" in page
    assert "Save changes and start a new voice session" in page
    fields = (
        "rtModel", "voice", "effort", "vadMode", "vadThreshold", "vadSilence",
        "idle", "bargeInRms", "xvfGain", "listeningRms",
    )
    for field in fields:
        assert page.count(f'id="{field}"') == 1

