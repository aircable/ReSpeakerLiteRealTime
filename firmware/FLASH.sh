# flash via esphome
uvx --from esphome==2026.6.0 esphome run respeaker-thinking-companion.yaml --device /dev/ttyACM0
# subsequent OTA updates:
# uvx --from esphome==2026.6.0 esphome run respeaker-thinking-companion.yaml --device thinking-companion.local
