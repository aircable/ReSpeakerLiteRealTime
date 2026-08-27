# ReSpeaker Realtime Thinking Companion

A dedicated ReSpeaker Lite voice device and LAN gateway connected directly to OpenAI Realtime.
Home Assistant, Hermes, OpenWebUI, and Pipecat are not in the audio path.

## Run the gateway

1. Copy `.env.example` to `.env`, set a separately billed `OPENAI_API_KEY`, and replace both
   tokens with long random values.
2. Run `docker compose up --build -d`.
3. Open `http://gateway-host:8080/` and enter `UI_TOKEN`.

## Run the published container

Images for both amd64 and arm64 are published to GitHub Container Registry after tests pass on
the `main` branch. Docker repository names are lowercase, so pull
`ghcr.io/aircable/respeakerliterealtime:main`.

Create a persistent data directory and an environment file based on `.env.example`, then run:

```sh
mkdir -p /data/respeaker-realtime
docker run -d \
  --name respeaker-realtime \
  --restart unless-stopped \
  --env-file /data/respeaker-realtime/.env \
  -p 8080:8080 \
  -v /data/respeaker-realtime/data:/data \
  ghcr.io/aircable/respeakerliterealtime:main
```

The container needs no Home Assistant token. `OPENAI_API_KEY` is the OpenAI API key,
`DEVICE_TOKEN` must match the token compiled into the ReSpeaker firmware, and `UI_TOKEN` protects
the browser interface. `BARGE_IN_ENABLED` defaults to `false`, gating microphone frames at the
gateway during assistant playback to prevent an acoustic echo loop. Enable it after confirming
that the hardware AEC sufficiently suppresses playback at the microphone.

`IDLE_TIMEOUT_SECONDS` starts after a completed assistant reply while the device is listening; raw
microphone frames, including room noise, do not reset it. Say “go to sleep”, “stop”, or “goodbye”
to end a session immediately. The firmware's `output_volume` scales direct Realtime PCM before the
speaker path (`0.125` is -18 dB relative to full scale), while `dac_volume` explicitly constrains
the AIC3204 codec to the formatBCE media-player range.

Set `OPENAI_TRACE=true` (or enable **OpenAI trace** in the web UI) to log Realtime lifecycle,
audio-duration counters, first-audio latency, response status, token usage, and post-session planner
calls. Trace logging omits API keys, raw/base64 audio, instructions, and transcript text. UI changes
apply on the next device connection.

Home Assistant OS normally manages containers as Apps (formerly add-ons). Running this command
directly requires host-level SSH access and is not managed by Supervisor; packaging the image as a
Home Assistant App is the supported long-term HAOS installation path.

The SQLite database lives in the `companion-data` volume. Raw audio is not stored. Completed user
transcriptions and assistant transcripts are retained, and the post-session planner writes the
project summary and Markdown plan together with an immutable revision in one transaction.

## Flash the device

The XMOS chip must first have the formatBCE/Seeed 48 kHz I²S firmware v1.1.0 or newer. Copy
`firmware/secrets.example.yaml` to `firmware/secrets.yaml`, point `gateway_ws_url` at the gateway,
then compile `firmware/respeaker-thinking-companion.yaml` with ESPHome 2026.6 or newer.

The configuration pins the tested formatBCE component revisions. It retains XMOS DFU, the AIC3204
codec, 48 kHz 32-bit stereo I²S, hardware AEC, separate wake-word channel, mute/button, status LED,
OTA, and ESPHome's output resampler. The formatBCE microphone fork derives a 16 kHz PCM32 stereo
callback from the 48 kHz XMOS bus for microWakeWord. The custom component resamples AEC channel 0
to 24 kHz with ESPHome's sinc resampler, sends fixed 20 ms PCM16 frames from a six-frame static
FreeRTOS queue, and expands incoming mono PCM before the 24-to-48 kHz speaker resampler. Capture
stays active during playback.

The USR-to-D2 and MUTE-to-D3 rear-pad jumpers are required for the physical controls used by the
configuration. See [PROTOCOL.md](PROTOCOL.md) for the wire contract.

## Development

```sh
uv sync --extra test
uv run pytest
```

Gateway tests cover authentication models, exact frame boundaries, project activation, transcript
ordering, transactional plan revisions, output framing, cancellation/truncation, and rejection of
late audio. Hardware acceptance still requires the actual ReSpeaker: wake/follow-up/mute/stop,
Wi-Fi loss, one-hour soak, echo rejection, p95 interruption latency, and end-to-first-audio latency.

The cloud transport and event names follow the official [OpenAI Realtime WebSocket guide](https://developers.openai.com/api/docs/guides/realtime-websocket)
and [Realtime VAD guide](https://developers.openai.com/api/docs/guides/realtime-vad).
