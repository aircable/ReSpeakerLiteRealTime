# Device protocol v1

The device opens a persistent WebSocket to `/ws/device`. Text messages are UTF-8 JSON and carry
`"v": 1`; binary messages are exactly 960 bytes of 24 kHz, mono, signed little-endian PCM16
(20 ms). The OpenAI key never crosses this connection.

The first device message must be `auth` with `token`, `device_id`, and a `capabilities` object.
The gateway replies with `auth.ok`. A wake word sends `session.start`; explicit stop sends
`session.stop`. The gateway creates the metered cloud session only after `session.start`.

Devices advertising `runtime_volume` report their normalized PCM output multiplier (0.0–1.0)
with `volume.changed`. The gateway sends `volume.set` with the same normalized `level` field.
The device applies and persists the setting, then confirms it with another `volume.changed`.

For each assistant output the gateway sends `playback.start` containing `stream_id`,
`response_id`, and `item_id`, followed by binary frames, then `playback.end`. The device accepts
binary audio only for the current stream and reports `playback.progress` with the duration that
actually reached its output. `playback.flush` invalidates that stream immediately.

On barge-in the gateway sends `playback.flush`, cancels the cloud response, and truncates its
conversation item at the last reported `played_ms`. Late cloud audio for the cancelled response
is discarded. Microphone binary frames continue throughout playback.

Other messages are `state`, `heartbeat`/`heartbeat.ack`, `transcript.delta`, and `error`. The UI
can change a connected device through `/api/devices/{device_id}/volume`; an active Realtime
session exposes the same operation as the `control_volume` voice tool. Unknown or malformed
messages are protocol errors. Tokens for `/ws/device` and `/api/*` are deliberately different.
