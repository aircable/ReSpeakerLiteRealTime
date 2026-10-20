# Wake chime provenance

`wake_word_triggered.flac` is the wake notification used by the original
[ReSpeaker Lite ESPHome integration](https://github.com/formatBCE/Respeaker-Lite-ESPHome-integration/blob/main/config/common/respeaker-satellite-base.yaml).
It comes from [ESPHome Home Assistant Voice PE](https://github.com/esphome/home-assistant-voice-pe/blob/dev/sounds/wake_word_triggered.flac).
Copyright (c) 2019 ESPHome; the upstream repository states its non-code assets
are under the MIT license (see its [LICENSE](https://github.com/esphome/home-assistant-voice-pe/blob/dev/LICENSE)).

`wake_chime_pcm.h` is a 24 kHz mono PCM16 conversion of this file, compiled
into the local wake acknowledgement path. It is played before microphone
transport starts, so the chime cannot be mistaken for the user's first words.
