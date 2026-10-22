# Potential future work: Home Assistant app

Status: **proposal only**. The gateway currently runs as an independent Docker
service; this document does not change its deployment or audio path.

## Goal and boundary

Package the existing gateway as a Home Assistant OS app (formerly an add-on),
managed by Supervisor. Keep the ReSpeaker-to-gateway WebSocket and the
gateway-to-OpenAI Realtime connection direct. Home Assistant Assist must not
become an extra hop in live audio, playback, barge-in, or project switching.
The current Docker Compose installation should remain supported.

The existing container workflow already publishes amd64 and arm64 images to
`ghcr.io/aircable/respeakerliterealtime`. An app repository could reference a
versioned multi-architecture image rather than rebuilding the gateway on the
Home Assistant host. Pin app and image versions together; do not use the
mutable `main` tag for a release.

## Proposed implementation

1. Add an app manifest, configuration schema, documentation, and release
   metadata to a Home Assistant app repository (or a dedicated app directory
   in this repository). Expose the gateway's LAN TCP port for the ESP32;
   Home Assistant Ingress is an optional, separate route for its browser UI.
   The ESP32 must not depend on an Ingress URL or Home Assistant login.
2. Map app options and secrets (`OPENAI_API_KEY`, `DEVICE_TOKEN`, `UI_TOKEN`,
   gateway settings) into the gateway configuration. The current gateway reads
   environment variables, while app options are provided through the app
   environment/filesystem; this needs an explicit, tested adapter. Retain
   device-token authentication on the WebSocket and UI-token protection on any
   directly exposed UI/API port, even if Ingress authenticates its own route.
3. Persist the SQLite database and diagnostic recordings under app-managed
   storage. Test write permissions: the current container runs as UID 10001,
   so a Supervisor-provided `/data` mount may need a permission strategy.
   Include a documented, reversible migration of the existing database and
   secrets. Do not start old and new gateways against the same database, port,
   or device identity at the same time.
4. Test installation, restart, upgrade, backup/restore, browser UI, ESP32
   WebSocket connection, and a complete conversation on Home Assistant OS.
   Confirm that startup and playback behavior match the standalone deployment.

Home Assistant entity/service access is **a separate optional feature**. If
added, expose a narrowly scoped gateway tool and request Home Assistant API
access for the app. Packaging alone must not silently grant the voice agent
control of Home Assistant.

## Acceptance criteria

- The same firmware and gateway voice features work with either deployment.
- The device reaches a stable LAN WebSocket endpoint independently of Ingress.
- Existing projects, plans, voice settings, and transcripts survive migration
  and upgrades; backups can restore them.
- App configuration never logs secrets and does not weaken direct-port auth.
- The Docker Compose installation remains an available fallback.

References: [Home Assistant app development](https://developers.home-assistant.io/docs/apps),
[prebuilt image publishing](https://developers.home-assistant.io/docs/apps/publishing/),
[Ingress](https://developers.home-assistant.io/docs/apps/presentation/), and
[Home Assistant API access](https://developers.home-assistant.io/docs/apps/communication/).
