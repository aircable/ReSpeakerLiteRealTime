# First XVF3610-INT diagnostic

Use `respeaker-thinking-companion-xvf3610-diag.yaml` with the stock XVF3610-INT
5.7.3 image and the 48 kHz test data partition already installed on the XMOS.
This ESP32 image generates the I2S clocks and checks XVF control and microphone
capture. It does not connect to Wi-Fi or the gateway, play audio, write the
codec, or update XMOS flash.

From `firmware/`, flash and watch the ESP32 USB serial port:

```sh
uvx --from esphome==2026.6.0 esphome run \
  respeaker-thinking-companion-xvf3610-diag.yaml --device /dev/ttyACM0
```

Capture at least 20 seconds of logs. The useful lines are the I2C scan,
`xvf3610_diag`, and `i2s_audio.microphone` messages. Speak near the microphones
for part of the capture.

Expected observations:

| Log field | Healthy first result |
| --- | --- |
| I2C scan | `0x2C` present; `0x42` need not be present |
| `Mic/2s` | About 32,000 decimated frames every 2 seconds; `empty=0` |
| `peak` | Changes with speech; a constant zero means no useful XMOS output |
| `XVF control` | Responds; `i2s_rate=48000`, `i2s_start=1`, `mic_start=1` if the data partition ran |
| `AEC` | A read-only report of bypass and X/Y channel settings |
| Microphone warnings | No repeated `ESP_ERR_TIMEOUT` |

The microphone frame count alone proves that ESP32 I2S DMA is clocking; the
varying peak and XVF status together show whether XMOS is producing audio.
If `0x2C` appears in the scan but control reads fail, first inspect the clock
and microphone lines. XMOS documents that control commands sent before I2S
clocks are active may not be processed.

The diagnostic deliberately omits the wake word model, speaker, and gateway.
Once these observations are stable, the next experiment can add codec reset and
playback while preserving this known first test.

The prior Seeed configuration is in `respeaker-thinking-companion.yaml` and
`respeaker-thinking-companion-ready.yaml`. The published Git baseline is on
`firmware/seeed-v1.1-stable`. Returning to that system also requires restoring
the Seeed XMOS 1.1.0 image; changing the ESP32 image alone does not change the
XMOS firmware. See `XMOS_PROGRAMMING.md` for XMOS recovery notes.
