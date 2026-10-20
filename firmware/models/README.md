# Ready-word model

Place the trained microWakeWord export here as:

- `ready.json`
- the `.tflite` file named by the manifest's `model` field

The manifest must use `"wake_word": "ready"`. For the Seeed XMOS image, compile
`respeaker-thinking-companion-ready.yaml` instead of the base firmware file.
The XVF3610 live build also includes this model and its ready-word switching.

The ready variant advertises the capability to the gateway. The gateway exposes its
`wait_for_ready` tool only to capable devices, pauses microphone transport after a procedural
step has finished playing, and resumes the Realtime response when the local model detects
“ready”. The USER button remains available to end the session during a wait.

The FFVA one-flash upgrade also embeds `ffva-int-adec-stock-1.1.0-upgrade-v2.bin`.
Its build provenance, SHA-256, supported starting version, and recovery notes are in
[FFVA_AUTO_UPGRADE.md](../FFVA_AUTO_UPGRADE.md). The binary is a derivative of
[`aircable/sln_voice_respeaker_lite`](https://github.com/aircable/sln_voice_respeaker_lite),
whose XMOS source is available under the [XMOS Public Licence](XMOS_PUBLIC_LICENCE.rst).
