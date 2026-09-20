# Ready-word model

Place the trained microWakeWord export here as:

- `ready.json`
- the `.tflite` file named by the manifest's `model` field

The manifest must use `"wake_word": "ready"`. Then compile
`respeaker-thinking-companion-ready.yaml` instead of the base firmware file.

The ready variant advertises the capability to the gateway. The gateway exposes its
`wait_for_ready` tool only to capable devices, pauses microphone transport after a procedural
step has finished playing, and resumes the Realtime response when the local model detects
“ready”. The USER button remains available to end the session during a wait.
