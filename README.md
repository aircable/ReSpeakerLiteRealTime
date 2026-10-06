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

The gateway accepts device PCM into a bounded five-second queue while the billed OpenAI session
connects, then forwards it in order from a separate task. This preserves speech immediately after
the wake word and prevents OpenAI latency from backpressuring the device WebSocket. Assistant PCM
is paced at 20 ms per frame and held to at most 200 ms ahead of device-reported DAC progress, so
the ESP32's fixed playback queue cannot be overrun by clock drift or scheduler jitter. Generated
audio waiting behind real-time playback uses a bounded 120-second gateway queue by default;
`PLAYBACK_BUFFER_SECONDS` accepts 30–600 seconds. `REALTIME_MAX_OUTPUT_TOKENS` defaults to the
Realtime API's 4096-token per-response ceiling.

`IDLE_TIMEOUT_SECONDS` starts after a completed assistant reply while the device is listening; set
it to `0` to keep the session open until an explicit stop or the hard session limit. Raw microphone
frames, including room noise, do not reset it. Say “go to sleep”, “end session”, or “goodbye” to end a
session immediately. The firmware's `output_volume` scales direct Realtime PCM before the
speaker path (`0.125` is -18 dB relative to full scale). The AIC3204 remains at its proven default;
changing its logarithmic control in addition to PCM scaling compounds the attenuation.

Set `OPENAI_TRACE=true` (or enable **OpenAI trace** in the web UI) to log Realtime lifecycle,
audio-duration counters, first-audio latency, response status, token usage, and post-session planner
calls. Trace logging omits API keys, raw/base64 audio, instructions, and transcript text. UI changes
apply on the next voice session.

`ANNOUNCE_ACTIVE_PROJECT` defaults to `true` and can also be changed with **announce active
project** in the web UI. On wake, the assistant names the active project before asking what to work
on. “What projects do I have?” lists projects, and “Switch to PROJECT NAME” activates a project.
A switch closes the old project context and opens a clean Realtime and database session on the
same device connection, so transcripts and durable project memory remain separated.

## Voice commands

The gateway-wide command registry is [gateway/voice_commands.py](gateway/voice_commands.py).
Each entry records example spoken requests, when to use or avoid the command, its device effect,
its Realtime argument schema, any required device capability, and the session handler that runs
it. The gateway derives both the Realtime tool definitions and the voice-command instructions
from this registry; project instructions only add project-specific policies. Example phrases
guide the model's intent-based selection; they are not an exact-match speech parser.

| Example request | Function | Effect |
| --- | --- | --- |
| “Go to sleep” | `end_session` | Ends the session and resumes wake-word listening |
| “Which project is active?” | `list_projects` | Read-only lookup |
| “Switch to Cooking Companion” | `switch_project` | Changes the active project and Realtime context |
| “Set volume to 40 percent” | `control_volume` | Changes and persists device playback volume |
| “Search the web for today's weather” | `search_web` | Runs OpenAI web search and shows clickable citations in the Live device UI |
| “Pause until ready” | `wait_for_ready` | Stops microphone upload and arms local ready-word detection |

`wait_for_ready` is advertised only when the firmware reports the `ready_keyword` capability.
To add a new command, add a registry entry and a corresponding `_voice_*` handler on
`DeviceSession`, then test its capability gate and result. A project instruction cannot add a
function that is absent from the gateway's registry. `search_web` uses the existing API key and
configured planner model; each invocation is billed as a Responses web-search request. It needs no
firmware change. Sources appear in the live UI, but search results are not stored in the project plan.

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

### Optional local “ready” model

The standard firmware continues to use only “Okay Nabu”. For hands-free step-by-step workflows,
copy the trained `ready.json` and the TFLite file it references into `firmware/models/`, then compile
`firmware/respeaker-thinking-companion-ready.yaml`. The manifest's `wake_word` must be exactly
`ready`.

The ready variant enables only one classifier at a time: “Okay Nabu” while idle/normal, and
“ready” while a `wait_for_ready` tool call is pending. During that wait, normal microphone audio is
not sent to OpenAI. Saying “ready” completes the pending tool call and advances the procedure;
pressing USER still ends the session. The gateway exposes this tool only after firmware advertises
the `ready_keyword` capability, so the standard firmware cannot enter an unusable wait state.

The configuration pins the tested formatBCE component revisions. It retains XMOS DFU, the AIC3204
codec, 48 kHz 32-bit stereo I²S, hardware AEC, separate wake-word channel, mute/button, status LED,
OTA, and ESPHome's output resampler. The formatBCE microphone fork derives a 16 kHz PCM32 stereo
callback from the 48 kHz XMOS bus for microWakeWord. The custom component resamples AEC channel 0
to 24 kHz with ESPHome's sinc resampler, sends fixed 20 ms PCM16 frames from a six-frame static
FreeRTOS queue, and serializes audio plus fixed-size control messages through one WebSocket writer
task. Incoming mono PCM is expanded before the 24-to-48 kHz speaker resampler, and capture stays
active during playback. The wake phrase is **Okay Nabu**, using the pinned
`okay_nabu_20241226.3` MicroWakeWord model on channel 1 with the proven gain of 4.

The USR-to-D2 and MUTE-to-D3 rear-pad jumpers are required for the physical controls used by the
configuration. See [PROTOCOL.md](PROTOCOL.md) for the wire contract.

`output_volume` in the firmware YAML is the first-boot default. After flashing, the connected
device volume can be changed with the Live device slider or by saying, for example, “set volume
to 20 percent,” “a little louder,” or “quieter.” Runtime changes are stored on the ESP32 and
survive gateway reconnects and device reboots.

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
