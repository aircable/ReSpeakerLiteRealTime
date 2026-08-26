 # ReSpeaker Realtime Thinking Companion

  ## Summary

  Build a dedicated wake-word conversational device using the proven formatBCE ReSpeaker hardware components, a custom full-duplex ESPHome
  transport, and a standalone local gateway connected directly to OpenAI Realtime.

  The first version excludes Home Assistant, Hermes, OpenRouter, and OpenWebUI. It prioritizes natural dialogue, reliable barge-in,
  persistent project context, and automatic Markdown planning artifacts.

  ## Implementation

  ### Device firmware

  - Retain formatBCE’s XMOS DFU, 48 kHz I²S microphone/speaker, AEC, codec, mute/button, LED, microWakeWord, OTA, and speaker-resampler
    components.

  - Replace Home Assistant voice_assistant with a dedicated Realtime component.
  - Capture speech from XMOS channel 0; keep channel 1 for wake-word detection.
  - Convert the formatBCE 16 kHz callback (derived from the 48 kHz XMOS bus) to 24 kHz PCM16 mono using proper fixed-ratio sinc resampling.
  - Send 20 ms binary audio frames through fixed-size FreeRTOS buffers; avoid dynamic audio queues and blocking network calls.
  - Play incoming 24 kHz PCM16 through the existing ESPHome resampler into the 48 kHz ReSpeaker output.
  - Continue transmitting microphone audio during playback so XMOS AEC and OpenAI VAD can detect interruptions.
  - Implement states: idle, connecting, listening, thinking, speaking, muted, and error.
  - Wake word starts/resumes the active project. Button or spoken stop ends the session; physical mute always overrides capture.

  ### Local gateway and protocol

  - Implement a Python/FastAPI container using a direct server-side OpenAI Realtime WebSocket. WebSockets are the documented transport for
    server integrations. OpenAI Realtime WebSocket guide (https://developers.openai.com/api/docs/guides/realtime-websocket)

  - Default to gpt-realtime-2.1, configurable model/voice, low reasoning effort, semantic VAD with automatic response creation and
    interruption.

  - Keep the device-to-gateway connection persistent; create the billed OpenAI session only after wake activation.
  - Define a versioned /ws/device protocol:
      - JSON: authentication, capabilities, session start/stop, state, heartbeat, audio stream IDs, playback flush/progress, and errors.
      - Binary: 24 kHz PCM16 mono frames.

  - Tag output streams with response/item IDs so stale audio can be rejected after cancellation.
  - On user speech during playback:
      1. Immediately command device playback flush.
      2. Cancel the active response if still running.
      3. Use reported playback duration to truncate unheard assistant audio from conversation history.
      4. Continue forwarding microphone audio without restarting the session.

  - Default adaptive idle timeout: 30 seconds after a completed response; configurable. Add a 60-minute hard session limit.
  - Protect device and UI endpoints with separate tokens. Keep the OpenAI key exclusively on the gateway.
  - Package as portable Docker Compose with persistent data volume; an HA add-on wrapper is unnecessary for v1.

  ### Projects, transcript, and web UI

  - Store projects, sessions, turns, settings, usage, summaries, and plan revisions in SQLite.
  - Configure committed user-turn transcription with gpt-transcribe; retain assistant audio transcripts emitted by Realtime. OpenAI
    transcription model (https://developers.openai.com/api/docs/models/gpt-transcribe)

  - Reconstruct each cloud session from:
      - Socratic-collaborator instructions.
      - Active project goal and pinned notes.
      - Rolling summary and current Markdown plan.
      - The most recent 12 conversational turns.

  - After each live session, use configurable gpt-5.6-terra structured output to atomically update the summary, decisions, open questions,
    and Markdown plan while preserving the complete transcript.

  - Provide a token-protected LAN web UI for:
      - Creating/selecting the active project.
      - Live transcript and device/session status.
      - Editing goals, instructions, summaries, and plans.
      - Reviewing plan history and exporting Markdown.
      - Selecting model, voice, reasoning effort, timeout, and retention settings.

  - Do not store raw audio by default; expose an opt-in diagnostic recording mode.

  ## Reuse and Boundaries

  - Reuse formatBCE’s hardware-facing components, pinned to a tested upstream revision. ReSpeaker ESPHome integration
    (https://github.com/formatBCE/Respeaker-Lite-ESPHome-integration)

  - Use ha-openai-realtime only as reference for ESP32 transport and session concepts; do not retain its microphone suppression, linear
    resampling, Pipecat dependency, or interrupt implementation. Existing project (https://github.com/fjfricke/ha-openai-realtime)

  - Keep OpenWebUI/Hermes integration as a later optional transcript or tool handoff, outside the real-time audio path.

  ## Test and Acceptance Plan

  - Unit-test audio conversion, frame boundaries, bounded buffers, authentication, state transitions, cancellation, transcript ordering,
    and transactional plan updates.

  - Test the gateway against recorded OpenAI event sequences, including late audio after cancellation and reconnects during active
    responses.

  - Hardware-test:
      - Wake, follow-up dialogue, mute, explicit stop, and adaptive timeout.
      - Wi-Fi loss and gateway/OpenAI reconnection.
      - One-hour conversational soak without buffer growth or audio corruption.
      - Loud assistant playback without echo-induced false turns.

  - Acceptance targets:
      - Microphone remains active throughout assistant playback.
      - User speech reliably stops audible playback within 350 ms at p95.
      - Interrupted audio is neither replayed nor retained as if heard.
      - First assistant audio normally begins within 1.5 seconds after end-of-turn detection.
      - Every completed session produces a correct transcript and editable Markdown plan.

  ## Assumptions

  - The dedicated device uses the formatBCE ReSpeaker Lite hardware configuration and 48 kHz XMOS firmware.
  - The gateway runs on an always-on LAN Docker host.
  - API access is separately billed from the ChatGPT subscription, with project spend limits configured. OpenAI billing separation
    (https://help.openai.com/en/articles/8156019-is-api-usage-included-in-chatgpt-subscriptions-even-if-i-have-a-paid-chatgpt-account%23.pls
)

  - Initial deployment is single-user on a trusted LAN; remote multi-user access and Home Assistant control are out of scope.

