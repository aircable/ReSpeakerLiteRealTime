# Device protocol v1

The device opens a persistent WebSocket to `/ws/device`. Text messages are UTF-8 JSON and carry
`"v": 1`; binary messages are exactly 960 bytes of 24 kHz, mono, signed little-endian PCM16
(20 ms). The OpenAI key never crosses this connection.

The first device message must be `auth` with `token`, `device_id`, and a `capabilities` object.
It may also include a human-readable `name` for the web UI. `device_id` must be unique among
simultaneously connected boards; older firmware without `name` is displayed by ID. The gateway
replies with `auth.ok`. A wake word sends `session.start`; explicit stop sends
`session.stop`. The gateway creates the metered cloud session only after `session.start`.

Devices advertising `runtime_volume` report their normalized PCM output multiplier (0.0–1.0)
with `volume.changed`. The gateway sends `volume.set` with the same normalized `level` field.
The device applies and persists the setting, then confirms it with another `volume.changed`.

Devices with a second local “ready” model advertise `ready_keyword: true`. After the assistant
calls `wait_for_ready`, the gateway waits until the current step has physically finished playing,
sends `keyword.mode` with `mode: "ready"`, and stops accepting ordinary microphone audio. The
device enables only the ready model and sends `ready.detected` when it fires. The gateway replies
with `keyword.mode` set to `wake`, restores microphone transport, completes the pending tool call,
and asks Realtime for the next step. The physical USER button remains an out-of-band stop control.

Devices that can ring locally advertise `timer_alarm: true`. The gateway stores countdown timers
in SQLite, keyed by `device_id`, and sends `timer.alarm` with a positive `timer_id` and label when
one expires. It first ends any active voice session. The device plays a repeating local chime
and flashes amber until USER or the wake word dismisses it, then sends `timer.dismissed` with the
same ID. `timer.stop` cancels a ringing chime from the gateway. A pending alarm is resent on a
new authenticated device connection; timers continue counting while the gateway restarts.

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
