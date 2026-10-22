# ReSpeaker Lite XMOS programming and recovery

This page records the hardware, adapters, firmware images, and host tools needed to inspect,
program, and recover the XMOS XU316 on the ReSpeaker Lite. It deliberately distinguishes
confirmed details from the remaining unknown: Seeed publishes a [partial schematic view](https://files.seeedstudio.com/wiki/SenseCAP/respeaker/respeaker_lite_v1.0_sch_1.png), but not a complete board schematic or an authoritative JTAG-pad ordering.

**Current release:** For a stock Seeed 1.1.0 board, use the tested [FFVA auto-upgrade guide](firmware/FFVA_AUTO_UPGRADE.md). That path needs no XTAG4. The XVF3610 5.7.3 commands and image names below document earlier experiments and are **not** the v1.0.0 installation procedure. This page links to third-party specifications and records our own board measurements; it does not reproduce Seeed/XMOS schematics or distribute the older XVF3610 package.

## System overview

The board contains three relevant processors/devices:

| Device | Function | Host connection |
| --- | --- | --- |
| XIAO ESP32-S3 | ESPHome, Wi-Fi, Realtime transport and UI controls | XIAO USB-C, OTA |
| XMOS XU316 | Microphone DSP, I2S routing and optional AEC/voice processing | XMOS USB-C, I2C DFU, or XTAG4/JTAG |
| TI TLV320AIC3204 | Speaker/headphone codec | Shared I2C bus, controlled by ESP32 |

The two USB-C paths are independent. Flashing ESPHome through the XIAO connector does not
program the XU316. USB DFU through the XMOS connector does not program the ESP32.

### ESP32-to-audio-board interfaces

The physical clock and control wiring is shared, but clock ownership and **I2S data
directions depend on the XMOS image**. Use the matching ESP32 YAML:

| Interface | ESP32 signal | Purpose |
| --- | --- | --- |
| I2C | GPIO5 SDA, GPIO6 SCL, 400 kHz | XMOS control/DFU and AIC3204 control |
| I2S clocks | GPIO7 LRCLK, GPIO8 BCLK, GPIO9 MCLK | Seeed/XVF experiments drive clocks from ESP32; released FFVA drives clocks from XMOS and does not drive ESP32 GPIO9 |
| I2S data, Seeed 1.1.0 | GPIO44 RX; GPIO43 TX | Stock `respeaker-thinking-companion.yaml` only |
| I2S data, XVF3610 5.7.3 | GPIO43 RX; GPIO44 TX | `respeaker-thinking-companion-xvf3610-live.yaml` only |
| I2S data, released FFVA | GPIO43 RX; GPIO44 TX | `respeaker-thinking-companion-ffva-auto-upgrade.yaml` |
| XMOS reset | GPIO2 | Resets the XU316 |
| Mute toggle | GPIO4, active low pulse | Toggles the Seeed firmware mute latch |
| User button | GPIO3, active low | Conversation start/stop in this project |

The stock Seeed I2S firmware exposes its command transport at I2C address `0x42`:

- DFU resource: `0xF0`
- Seeed control resource: `0xF1`
- AIC3204 address on the same bus: `0x18`

The experimental standard XVF3610 software normally uses address `0x2C`, DFU resource `0xD0`,
and AEC control resource `0x11`. The local probe checks both `0x42` and `0x2C` because a data
partition can alter the address.

### XCORE-VOICE ReSpeaker FFVA fork: historical compatibility checkpoint

The first locally cloned `XCORE_VOICE_RESPEAKER_LITE` FFVA INT example was a **third**
firmware family, not a drop-in XVF3610 5.7.3 or Seeed 1.1.0 image. Its ReSpeaker
board map uses the XVF-style I2S directions: XMOS microphone output to ESP32
GPIO43, ESP32 speaker/AEC-reference input on GPIO44, with external 48 kHz I2S
clocks. The fork also maps X0D32 to codec reset, X0D31 to amplifier enable, and
X0D30 to LED-line enable; those agree with the board-level traces. The codec
remains at I2C `0x18`.

| Function | Seeed 1.1.0 | Historical XVF3610 5.7.3 | Initial FFVA INT prototype |
| --- | --- | --- | --- |
| XMOS I2C slave | `0x42` | Usually `0x2C` (probe both) | `0x42` |
| I2C DFU | resource `0xF0` | resource `0xD0`; Seeed `0xF0` may also respond | resource `0xF0`, enabled in `ffva_int.cmake` |
| Application/DSP control | Seeed `0xF1` | standard control `0x00`, GPIO `0xE0`, AEC `0x11`, AGC `0x31`, stage A `0x10`, pipeline `0xF0` | **Not implemented**; only DFU `0xF0` is registered |
| ESP32 I2S data | RX GPIO44, TX GPIO43 | RX GPIO43, TX GPIO44 | RX GPIO43, TX GPIO44 |

This initial FFVA DFU command map matched the ESP32 updater's Seeed-style `GETVERSION`
(`0xD8`), `SETALTERNATE` (`0x40`), `DNLOAD` (`0x01`) and `GETSTATUS` (`0x83`)
framing. That supports retaining I2C DFU **after** this firmware is installed;
it does not establish that the stock Seeed 1.1.0 application accepts the FFVA
upgrade image or that the new image boots with the stock flash/data partition.
In particular, the fork's factory-flash target requests a 2 MiB boot partition,
and its default reported application version is `255.254.253`; choose a real
version and verify the existing flash layout before any in-place upgrade.
At that checkpoint, the experimental `xvf3610_audio` ESP32 component could not
control FFVA AEC, AGC, or GPIO. This is **not** a description of the released image.

The first FFVA INT RAM test produced loud acoustic feedback: its codec selected
SCLK/MFP3 as secondary DIN, but that pin carries the XMOS microphone output on
this board, and startup enabled the speaker amplifier. Do not run that earlier
image again. The revised FFVA INT source selects primary DIN/MFP1 (ESP32
GPIO44), sets the codec DAC clock to 48 kHz from the ESP32's 12.288 MHz MCLK
(NDAC=1, MDAC=2, DOSR=128), and builds with the amplifier **disabled**. The
early RAM-test image is superseded by the tested FFVA release; do not use it as
a programming target. The lesson remains: first verify microphone and codec
I2C behavior with the amplifier disabled, then test playback at low volume.

## XMOS USB DFU

When the XMOS USB-C port is in DFU mode, the device has been observed as USB VID:PID
`2886:0019` with these alternate settings:

| Alt | Name | Intended content |
| ---: | --- | --- |
| 0 | `DFU FACTORY` | Factory image; do not overwrite casually |
| 1 | `DFU UPGRADE` | Upgrade application image |
| 2 | `DFU DATAPARTITION` | Upgrade data partition |

Confirm enumeration before every write:

```sh
dfu-util --version
dfu-util -l -d 2886:0019
```

An experimental XVF3610 upgrade consists of the data partition followed by the application:

```sh
sudo dfu-util -d 2886:0019 -e -a 2 \
  -D XVF3610-INT/data-partition/output/data_partition_respeaker_lite_48k_test_upgrade_v5_7_3.bin

sudo dfu-util -d 2886:0019 -a 1 \
  -D XVF3610-INT/bin/dfu/int/app_xvf3610_int_upgrade_v5_7_3.bin -R
```

These commands write upgrade slots, not a complete factory image. A DFU upload which consists
only of `0xFF` is not a backup. Do not erase or program alt 0 until XTAG recovery is proven.

The legacy Seeed ESP32 configuration can restore Seeed's tested 48 kHz I2S image through I2C. That YAML pins
version `1.1.0` and restores it automatically when the reported version differs. This recovery
depends on a working application/DFU service; it cannot recover an XMOS that no longer boots or
answers over I2C.

## XTAG4 and xSYS2/JTAG

Use an **XA-XTAG4**, not an XTAG3. XU316 is an xcore.ai device and its JTAG bank uses a 1.8 V
reference. XTAG4 senses the target-supplied `VREF_JTAG`; it does not replace target power. Power
the ReSpeaker normally and never inject 3.3 V or 5 V into a JTAG signal.

The standard JTAG-only xSYS2 header is a 2x5 **1.27 mm (0.050 inch)** connector:

| Pin | Signal | Direction relative to XTAG4 |
| ---: | --- | --- |
| 1 | `VREF_JTAG` (nominally 1.8 V for XU316) | Target to XTAG4 |
| 2 | `TMS` | XTAG4 to target |
| 3 | GND | — |
| 4 | `TCK` | XTAG4 to target |
| 5 | GND | — |
| 6 | `TDO` | Target to XTAG4 |
| 7 | GND | — |
| 8 | `TDI` | XTAG4 to target |
| 9 | GND | — |
| 10 | `RST_N` | XTAG4 to target, open drain |

The full XTAG4 connector is 2x10. Pins 1–10 are the JTAG-only subset; pins 11–20 carry the
xCONNECT link/xSCOPE signals and are unnecessary for factory flashing.

### ReSpeaker pad warning

Seeed labels the ten front pads as JTAG, but its [published schematic image](https://files.seeedstudio.com/wiki/SenseCAP/respeaker/respeaker_lite_v1.0_sch_1.png) does not supply a complete, authoritative pad-to-XTAG4 cable map. Do **not** assume that physical pad 1 equals xSYS2 pin 1 merely because the footprint has ten positions. Before installing or connecting a header:

1. Measure the pitch. Four intervals from pin 1 to pin 9 should be approximately 5.08 mm for a
   1.27 mm header, or 10.16 mm for a 2.54 mm header.
2. Identify all grounds with an unpowered continuity test.
3. Identify `VREF_JTAG`; with the board powered it should be approximately 1.8 V.
   The mapping measured on our board uses TP11 for XTAG4 xSYS2 pin 1. The XTAG4 xSYS2 header is **20-pin**, not 24-pin.
4. Continuity-test the remaining pads to the XU316/package or accessible test pads when possible.
5. Mark the physical pad orientation and confirm every mapped signal before attaching XTAG4.

Our continuity-tested adapter used this **non-straight-through** map. It is a measurement from one board, not a Seeed-published pinout; repeat the checks for another hardware revision.

| ReSpeaker connection | Signal | XTAG4 xSYS2 pin |
| --- | --- | ---: |
| TP11 | `VREF_JTAG` | 1 |
| Front pad 1 | `TDI` | 8 |
| Front pad 3 | `TMS` | 2 |
| Front pad 5 | `TCK` | 4 |
| Front pad 7 | `TDO` | 6 |
| Front pad 9 | `RST_N` | 10 |
| Front pad 10 | GND | 3 |

Front pads 2, 4, 6, and 8 and XTAG4 pins 5, 7, 9, and 11–20 were not used in this adapter. Do not wire the ten board pads straight through to xSYS2.

## Hardware and adapter checklist

Required:

- XMOS `XA-XTAG4` debug adapter.
- USB cable for XTAG4.
- USB cable/power for the ReSpeaker target.
- 20-conductor, 1.27 mm XTAG4 cable, Harwin `M50-9101042`, Mouser
  [`855-M50-9101042`](https://www.mouser.com/ProductDetail/Harwin/M50-9101042).
- Fine-tip temperature-controlled soldering iron, flux, magnification, solder wick, and a DMM
  with continuity and DC-voltage modes.
- A way to positively mark pin 1 on the board, adapter, and cable.

Connector options after measuring the board:

- For a standard 2x5 1.27 mm footprint: Harwin `M50-3500542`, Mouser
  [`855-M50-3500542`](https://www.mouser.com/ProductDetail/Harwin/M50-3500542).
- For a 2x5 2.54 mm footprint only: Harwin `M20-9980545`, Mouser
  [`855-M20-9980545`](https://www.mouser.com/ProductDetail/Harwin/M20-9980545).
- For a custom adapter's 20-pin XTAG4 side: Harwin `M50-3551042`, Mouser
  [`855-M50-3551042`](https://www.mouser.com/ProductDetail/Harwin/M50-3551042).

The previously considered 2.54 mm header/socket pair must not be ordered solely from visual
inspection. XMOS's official half-size xSYS2 connector is 1.27 mm; the ReSpeaker footprint still
needs to be measured.

A simple adapter board needs no active circuitry. Route the **measured map above**, leave unused
pins open, provide obvious orientation markings on both sides, and add labeled test points for
VREF and GND.

## Programming tools

### XMOS XTC Tools

The locally installed toolkit is currently:

```text
../XTC_15.3.1
```

`SetEnv` derives paths from the current directory, so enter that directory before sourcing it:

```sh
cd ../XTC_15.3.1
source ./SetEnv
cd ../FullDuplexAssistant
```

On Linux, install XTAG permissions once:

```sh
cd ../XTC_15.3.1/scripts
sudo ./setup_xmos_devices.sh
./check_xmos_devices.sh
```

After connecting and powering both boards:

```sh
xrun -l
xflash -l
```

Expected detection is an `XMOS XTAG-4` row whose `Devices` field contains an XS3/xcore.ai
device. `Devices: None` means XTAG4 itself is visible but the target, wiring, reference voltage,
power, or reset path is not.

Important: the XVF3610 5.7.3 release notes explicitly say to use **XTC 15.1.4** to install that
release. XTC 15.3.1 recognizes XU316 and XTAG4, but factory programming the 5.7.3 image should
use 15.1.4 unless XMOS confirms compatibility or a sacrificial-board test proves otherwise.

### Factory programming through XTAG4

Use the `.xe` application and the **factory**, not upgrade, data partition:

```sh
xflash --factory XVF3610-INT/bin/xflash/int/app_xvf3610_int_v5_7_3.xe \
  --boot-partition-size 1048576 \
  --data XVF3610-INT/data-partition/output/data_partition_respeaker_lite_48k_test_factory_v5_7_3.bin
```

Do not run this until `xrun -l` identifies the XU316 and the JTAG mapping has been verified. Keep
the Seeed 1.1.0 recovery image available before attempting the XVF3610 factory write.

### Other useful tools

| Tool | Use |
| --- | --- |
| `xrun` | Detect/reset/run and inspect an XMOS target through XTAG |
| `xflash` | Construct and program factory SPI-flash images through XTAG |
| `xgdb` | Source-level debugging when matching symbols/source are available |
| `dfu-util` 0.11 | USB DFU enumeration and upgrade-slot writes |
| `vfctrl_json` | Read/write XVF controls through a supported host interface |
| `data_partition_generator` | Build XVF control/data partitions |
| `dfu_suffix_generator` | Add/inspect DFU metadata for upgrade images |
| Local `xmos_probe` | Read-only I2C discovery and AEC-control probing |
| Local `xmos_dfu` | Experimental dual-protocol I2C upgrade writer |

The XVF3610 package contains prebuilt host tools under `XVF3610-INT/host/<OS>/bin`. The Pi build
also contains `dfu_i2c` and `vfctrl_i2c`. The release package contains binaries and data-partition
sources, but not the complete XVF3610 application source needed to rebuild the DSP application.

## Historical local XVF3610 artifacts (not part of this repository)

These names and hashes identify files used in the earlier XVF3610 experiment. They were local
downloads or build-cache contents, **not** the released FFVA firmware and not a promise that
the files exist in a fresh clone. Do not copy the proprietary package into this repository.

| File | Size | Purpose |
| --- | ---: | --- |
| `XVF3610-INT/bin/xflash/int/app_xvf3610_int_v5_7_3.xe` | 7,739,956 B | XTAG/xflash factory application input |
| `XVF3610-INT/bin/dfu/int/app_xvf3610_int_upgrade_v5_7_3.bin` | 229,376 B | USB/I2C upgrade application |
| `XVF3610-INT/data-partition/output/data_partition_respeaker_lite_48k_test_factory_v5_7_3.bin` | 12,288 B | Factory data partition for xflash |
| `XVF3610-INT/data-partition/output/data_partition_respeaker_lite_48k_test_upgrade_v5_7_3.bin` | 4,096 B | USB/I2C upgrade data partition |
| `firmware/.esphome/external_components/443f344a/respeaker_lite_i2s_dfu_firmware_48k_v1.1.0.bin` | 274,432 B | Known-good Seeed 48 kHz I2S recovery image |

Before programming, verify that file sizes and hashes have not changed unexpectedly:

```text
0d9cef94539acb80588c30d2869944e46f096afca9d88e5b62526a11aa730847  app_xvf3610_int_v5_7_3.xe
0bfb3083a23b8e091d700d1ec5f02809199a7d4ab6a1884c6ac2743d4a948a95  app_xvf3610_int_upgrade_v5_7_3.bin
2e6cf36e26cb35b3df47bc2e59162127c9f8f47460ac7ddd8bc5a4c14b8a6380  data_partition_respeaker_lite_48k_test_factory_v5_7_3.bin
e9ed7266e6869b6f127ebdbef7547753e9fd8ae9d977a04f983d188888633729  data_partition_respeaker_lite_48k_test_upgrade_v5_7_3.bin
```

## Historical XTAG4 arrival and XVF3610 bring-up sequence

This checklist records the older experimental path; for the current FFVA release, follow the
[stock-board upgrade guide](firmware/FFVA_AUTO_UPGRADE.md) instead.

1. Inventory XTAG4, its USB cable, the 20-pin cable, headers/adapter, and soldering supplies.
2. Install XTC 15.1.4 alongside 15.3.1; do not replace the working installation.
3. Run the Linux XTAG permission setup and confirm XTAG4 appears with no target attached.
4. Measure the ReSpeaker JTAG pitch and map GND/VREF before soldering.
5. Solder the appropriate header and continuity-check for bridges.
6. Power ReSpeaker normally; confirm approximately 1.8 V on VREF.
7. Connect XTAG4 with power off, then power the target and run `xrun -l`.
8. Save the complete detection output and adapter orientation before any write.
9. Verify ordinary ESP32 firmware and the Seeed 1.1.0 I2C recovery path still work.
10. Factory-flash XVF3610 5.7.3 only after steps 1–9 pass.
11. After flashing, test USB enumeration, I2C address/DFU discovery, 48 kHz I2S capture/playback,
    then standard AEC controls. Do not begin with loudspeaker playback.

## References

- [Seeed ReSpeaker Lite with XIAO guide](https://wiki.seeedstudio.com/xiao_respeaker/)
- [Seeed partial ReSpeaker Lite schematic image](https://files.seeedstudio.com/wiki/SenseCAP/respeaker/respeaker_lite_v1.0_sch_1.png)
- [Seeed's ReSpeaker Lite firmware and hardware notes](https://github.com/respeaker/ReSpeaker_Lite/)
- [Seeed forum request for the complete ReSpeaker Lite schematic](https://forum.seeedstudio.com/t/respeaker-lite-voice-assistant-kit-schematic/280622) — useful context; not an authoritative XTAG4 pinout
- [XMOS XTAG4 datasheet](https://www.xmos.com/file/xtag4-datasheet?version=latest)
- [XMOS XU316-1024-QF60A package pinout (original documentation URL)](https://www.xmos.com/documentation/XM-014034-PC/html/rst/XU316-1024-QF60A.html)
- [XMOS XU316 JTAG/xSYS2 documentation](https://www.xmos.com/documentation/XM-015129-PC/html/rst/XU316-1024.html#jtag-xscope-and-debugging)
- [XMOS XTC target and XTAG setup](https://www.xmos.com/documentation/XM-014363-PC/html/installation/install-configure/install-tools/check_xtag.html)
- [Seeed ReSpeaker Lite board documentation](https://wiki.seeedstudio.com/reSpeaker_usb_v3/)
- Local release notes: `XVF3610-INT/Release Notes XVF3610-INT & -UA firmware version 5.7.3.txt`
- Local data-partition guide: `XVF3610-INT/data-partition/how_to_generate_data_partition_images.rst`
