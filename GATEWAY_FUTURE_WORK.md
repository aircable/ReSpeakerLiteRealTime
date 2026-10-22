# Potential future work: gateway voice-provider adapters

Status: **proposal only**. The v1.0.0 gateway remains connected to OpenAI Realtime. This document does not change the working audio path, firmware, or deployment.

## Goal and boundary

Keep the ReSpeaker Lite-to-gateway WebSocket protocol and the gateway's device audio transport, playback pacing, barge-in level guards, project/session state, and tool execution independent of the voice model provider. Add a provider boundary between those gateway responsibilities and the model-specific session protocol:

```text
ReSpeaker Lite <-> gateway device transport / DeviceSession
                         |
                   VoiceProvider interface
                    /       |       \
          OpenAI Realtime  HF server  Moshi
```

Today, `gateway/realtime.py` wraps the OpenAI Realtime WebSocket, while `gateway/session.py` consumes OpenAI event names and calls OpenAI-specific methods directly. The proposed adapter must normalize both sides of that seam. Merely changing the WebSocket URL would leave session lifecycle, transcripts, tool calls, and interruption behavior coupled to OpenAI. The post-session planner is a separate model integration and is **not** made provider-neutral by this voice adapter.

## Proposed internal contract

Define a small asynchronous `VoiceProvider` interface for opening/closing a session, sending microphone PCM, updating supported session options, requesting a response, returning tool results, and cancelling output. It should emit gateway-owned events for session readiness/error, speech start/stop, user and assistant transcripts, assistant audio chunks and completion, tool requests, response completion/cancellation, and optional usage metrics. Keep provider wire events inside their adapters rather than passing raw OpenAI events into `DeviceSession`.

The adapter should also report capabilities and negotiated audio formats. At minimum, distinguish:

- whether the provider supports live input while speaking and what actually triggers an interruption;
- whether cancellation stops generation, clears queued audio, and/or updates conversation history;
- whether it supports tool calls and returning results during a live voice session;
- whether speech boundaries and transcripts are native, optional, or unavailable;
- input/output sample rates, channel count, encoding, and who performs conversion.

Do not invent a universal `truncate` operation. [OpenAI's WebSocket flow](https://developers.openai.com/api/docs/guides/realtime-conversations) requires the client to stop playback and truncate *unplayed* assistant audio in conversation state; another provider may have different semantics or no equivalent. The gateway should use the device's playback-progress clock to make the best supported decision, and surface unsupported capabilities honestly to the UI and command registry. Retain a provider-specific escape hatch for features that cannot be expressed by the common contract without distorting it. Keep the existing OpenAI adapter and its tests as the behavioral baseline.

## Candidate landscape (October 2026)

This is a shortlist for future evaluation, not a claim that every service can replace the current Realtime connection. It deliberately separates **voice-model APIs** from **orchestration platforms**: the latter may duplicate parts of our gateway rather than plug into a provider adapter. Product availability, protocol details, licensing, and host requirements should be checked again before implementation. In particular, an “OpenAI-compatible” endpoint may support only a subset of the session, interruption, and tool events we rely on.

### Hosted services and platforms

| Candidate | Role and possible fit | Gateway question |
| --- | --- | --- |
| [OpenAI](https://developers.openai.com/api/docs/guides/voice-agents) | Current Realtime baseline for a general assistant; GPT-Live is a separate full-duplex option. | Treat Realtime and GPT-Live as distinct protocols, not a model-name swap. |
| [Google Gemini Live](https://ai.google.dev/gemini-api/docs/live-api) | Native streaming audio and multimodal reasoning. | Build an adapter for its session, tool, audio, and interruption events. |
| [ElevenLabs](https://elevenlabs.io/docs/eleven-agents/customization/realtime-models) | Distinctive voices and Eleven Realtime voice agents; also offers a configurable STT–LLM–TTS agent path. | Check workspace availability and how its Realtime-compatible events handle our tools and barge-in. |
| [Inworld AI](https://docs.inworld.ai/realtime/overview) | Configurable STT–LLM–TTS voice agent behind a Realtime-style API. | Test compatibility rather than assuming identical OpenAI event semantics. |
| [Deepgram](https://developers.deepgram.com/reference/voice-agent/voice-agent) | Streaming voice-agent API with configurable speech and language components. | Map its own WebSocket events, function calls, and interruption behavior. |
| [Vapi](https://docs.vapi.ai/quickstart) | Hosted orchestration of transcriber, model, and voice providers; useful for rapid comparison. | Is replacing part of our gateway worth the loss of direct session control? |
| [LiveKit](https://docs.livekit.io/agents/models/pipelines/) | Realtime transport and agent framework for cascaded or speech-to-speech models. | Evaluate as an infrastructure alternative, not a single voice-model adapter. |
| [Ultravox hosted](https://github.com/fixie-ai/ultravox) | Hosted realtime deployment of an audio-understanding model. | Verify its current hosting/API path, speech-output layer, and tool support. |
| [Hume EVI](https://dev.hume.ai/voice/reference/speech-to-speech-evi) | Emotion-aware voice interaction; historical comparison only. | **Do not start a new integration:** Hume says EVI API access ends November 13, 2026. |

### Open-weight and self-hostable experiments

| Candidate | Architecture | Integration and expectation |
| --- | --- | --- |
| [Hugging Face Speech-to-Speech](https://github.com/huggingface/speech-to-speech) | Swappable VAD → STT → LLM → TTS pipeline. | **First experiment:** local server with a core OpenAI Realtime-compatible interface; measure feature parity and host resource use. |
| [Kyutai Moshi](https://github.com/kyutai-labs/moshi) | Native full-duplex speech model. | **Second experiment:** dedicated adapter for its streaming protocol; evaluate simultaneous speech and hardware requirements. |
| [Ultravox open weights](https://github.com/fixie-ai/ultravox) | Audio input → speech-aware LLM → streaming text; a separate TTS layer supplies spoken output. | Later experiment for direct audio understanding, not a drop-in speech-to-speech model; check each model's weights and license. |

Use the same room, ReSpeaker, prompts, speaker level, and recorded interruptions for every comparison. Score first-audio delay, self-interruptions versus successful barge-ins, playback continuity, tool and project-command support, privacy, operating cost, and local compute needs. This keeps an appealing demo from obscuring regressions in the existing working assistant.

## Stage 1: Hugging Face Speech-to-Speech

Connect the [Hugging Face Speech-to-Speech server](https://github.com/huggingface/speech-to-speech) first. Its `/v1/realtime` WebSocket implements a tested **core subset** of OpenAI Realtime events, including audio input/output, transcripts, cancellation, truncation, and tool calls; it does not claim full API equivalence. This makes it a useful compatibility experiment, not proof that a URL swap will work. Map its events into the internal contract, then test wake-to-first-audio time, barge-in, playback completion, tool results, project switches, disconnect/reconnect, and transcript persistence against the OpenAI baseline. Decide explicitly whether the existing gateway planner stays on OpenAI or also needs a separate local backend.

Run the server on suitable host hardware, not on the ESP32 or XMOS. The project's published default local setup plans for more memory than our 8 GB M1 MacBook Air, so a smaller model, different backend, or other host would need its own evaluation. The server binds to loopback by default; if it is exposed beyond that, add gateway-side access controls rather than assuming the Realtime wire protocol provides them.

## Stage 2: Moshi

Then prototype a separate adapter for [Kyutai Moshi](https://github.com/kyutai-labs/moshi). Moshi models simultaneous user and assistant audio streams and uses its own streaming audio/token interface. Do **not** assume it speaks the OpenAI Realtime protocol or offers equivalent tool calls, transcripts, or cancellation. First establish its native transport, codec and sample-rate conversion, session lifecycle, and how playback should stop or continue when the user speaks. Keep gateway project/tool behavior behind capability checks instead of pretending unsupported features exist.

Compare Moshi with the modular Speech-to-Speech pipeline using the same ReSpeaker, room, speaker level, and recorded test script: intelligibility during simultaneous speech, self-interruption rate, barge-in delay, first-audio delay, playback continuity, and host resource use. Its full-duplex model may offer different turn-taking behavior; that is a hypothesis to test, not an assumed upgrade. The ReSpeaker's XMOS AEC remains valuable because neither provider removes physical speaker leakage at the microphone.

## Acceptance criteria for any provider

- Switching providers does not require reflashing the ReSpeaker or changing its device WebSocket protocol.
- Unsupported capabilities are explicit in configuration, the UI, and available voice tools; session startup fails clearly when a required capability is missing.
- Playback-progress, flush, and conversation-state behavior are correct under interruption, including when a provider cannot truncate history.
- Project/session records and tool execution remain isolated from provider wire formats; OpenAI behavior does not regress.
- Local-provider tests include security, resource use, audio quality, and recovery after a dropped connection, not just a successful first response.

Sources: [official OpenAI Realtime conversation guide](https://developers.openai.com/api/docs/guides/realtime-conversations), [Hugging Face Realtime engine](https://github.com/huggingface/speech-to-speech/blob/main/src/speech_to_speech/api/openai_realtime/README.md), and [Moshi source and server guidance](https://github.com/kyutai-labs/moshi).
