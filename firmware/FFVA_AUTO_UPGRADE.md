# ReSpeaker Lite: stock 1.1.0 to Thinking Companion FFVA

The first release is tagged `v1.0.0`. Its supported firmware path is the
FFVA XMOS-clock-master image and the ESP32 auto-upgrade configuration below.
Earlier experimental XMOS control and diagnostic components are not part of
this release.

The [FFVA auto-upgrade configuration](respeaker-thinking-companion-ffva-auto-upgrade.yaml)
builds one ESP32-S3 firmware image containing the matching XMOS application. On first boot it
upgrades a **stock Seeed XMOS 1.1.0** application over I²C, then reboots the ESP32 into the
XMOS-clock-master voice configuration. This stock-board path was hardware-tested with wake word,
microphone, speaker, output volume and barge-in. It does not require XTAG or a connector on the
board for a normal upgrade.

It is not a general-purpose XMOS flasher. The updater reads the installed XMOS version before
starting ESP32 audio:

| XMOS version | Result |
| --- | --- |
| Seeed `1.1.0` | Write the embedded FFVA upgrade image once, verify it, reboot the ESP32. |
| FFVA `255.254.253` | Image already present; skip DFU and start audio normally. |
| Any other version, or no DFU response | Refuse the update and keep audio/gateway startup disabled. |

`255.254.253` is the **internal compatibility marker of this tested binary**, not a claim about
the upstream XMOS release version. Do not change it in the YAML without building and testing a
new XMOS image. Reflashing the same ESP32 configuration does not reflash an XMOS already at that
version. The stock Seeed image and FFVA image use different I²S clocking and cannot be mixed
with the wrong ESP32 configuration.

## Prepare

1. Update the gateway from `ghcr.io/aircable/respeakerliterealtime:main` and confirm its `/health`
   endpoint works. A gateway device token is required; this is **not** a Home Assistant API token.
2. Clone or update [ReSpeakerLiteRealTime](https://github.com/aircable/ReSpeakerLiteRealTime)
   on the machine used to flash the ESP32. Use a stable USB cable and power source. Connect the
   ESP32/XIAO USB port, not the XMOS USB port.
3. In `firmware/`, copy `secrets.example.yaml` to the ignored `secrets.yaml`. Set Wi-Fi SSID and
   password, `gateway_ws_url` (for example `ws://gateway-host:8080/ws/device`), `device_token`
   (the gateway's `DEVICE_TOKEN`), and an OTA password.
4. If upgrading several devices, give each build a unique `substitutions.name` and
   `substitutions.friendly_name`; the name becomes its gateway device ID. Keep a copy of each
   device's OTA password and build configuration.

Build and install from the `firmware/` directory:

```sh
uvx --from esphome==2026.6.0 esphome run respeaker-thinking-companion-ffva-auto-upgrade.yaml --device /dev/ttyACM0
```

The command compiles from source and flashes the ESP32. The embedded XMOS image is automatically
written **on the next boot**, only if the installed XMOS reports stock `1.1.0`. Do not disconnect
power while `XMOS update progress` is advancing. The upgrade can take around two minutes; the
device then reboots. An OTA ESP32 update may be possible on an already configured device, but
initial stock-board installation is best done over USB where the boot log is visible.

Do not upload or share the generated `firmware.factory.bin` or `firmware.bin`: ESPHome embeds
your Wi-Fi and gateway credentials. The repository publishes the YAML, components and XMOS
image, **not** a credential-bearing prebuilt ESP32 image.

## Verify

Watch the ESP32 boot log for the version check, update progress, `XMOS update complete`, and
reboot. On the following boot, it should report XMOS `255.254.253` and skip the write. I²C
should find the AIC3204 codec at `0x18` and XMOS at `0x42`; the gateway should show the device
connected. If the physical mute LED is red, press mute to unmute the microphones. Then check:

1. Wake word starts a session and plays the local acknowledgement sound.
2. A spoken request is heard, assistant audio plays, and web volume control changes loudness.
3. With barge-in enabled in the gateway, a spoken interruption works without the assistant
   interrupting itself at the chosen RMS threshold.
4. Ask “what time is it?” and set a short timer. The gateway time zone defaults to UTC; change
   **Time zone** in Voice Settings. The timer should ring locally with a flashing amber LED;
   USER or the wake word dismisses it. The timer feature requires the matching updated gateway.

Timers are persisted by the gateway in SQLite. If the device is disconnected when a timer is
due, it rings after reconnecting, not at the original deadline while offline. A gateway restart
does not discard the timer. Timer chime uses the device's output volume; 0% is silent.
If it is not dismissed, the local chime stops after 60 seconds.

## Recovery and scope

This upgrade was tested on a stock `1.1.0` board, not every ReSpeaker Lite revision. If the
version is unrecognized, stop rather than bypassing the version guard. If the XMOS update fails,
retain power, capture the full boot log, and check the XMOS USB DFU device. The stock board has
factory/upgrade/data-partition DFU slots; recovery may be possible through its XMOS USB port or
XTAG depending on which image still boots. Confirm the selected DFU slot before any manual
rewrite. Do not erase the XMOS factory slot as part of this process.

The embedded image is `models/ffva-int-adec-stock-1.1.0-upgrade-v2.bin` (278,528 bytes, SHA-256
`134ea0fc972803ccd2e1ebb772f384955e943436bacf0d95f71ec11127a32bdb`). It was built from
[`aircable/sln_voice_respeaker_lite`](https://github.com/aircable/sln_voice_respeaker_lite)
commit `93d0465111be6be4dd47a19ef9a59ef6ebb7745b`, based on the XMOS/Seeed FFVA example.
This derivative is subject to the [XMOS Public Licence](models/XMOS_PUBLIC_LICENCE.rst); use it
on compatible XMOS-designed hardware and retain the licence and source notice when redistributing.
