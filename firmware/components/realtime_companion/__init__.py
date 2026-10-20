import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import esp32, microphone, speaker
from esphome.const import CONF_ID, CONF_MICROPHONE, CONF_SPEAKER

DEPENDENCIES = ["wifi"]
AUTO_LOAD = ["audio", "json"]

CONF_URL = "url"
CONF_TOKEN = "token"
CONF_DEVICE_ID = "device_id"
CONF_DEVICE_NAME = "device_name"
CONF_OUTPUT_VOLUME = "output_volume"
CONF_VOLUME_PREFERENCE_KEY = "volume_preference_key"
CONF_READY_KEYWORD = "ready_keyword"
CONF_MIC_GAIN_CONTROLLER = "mic_gain_controller"
CONF_STARTUP_GUARD = "startup_guard"
CONF_PLAYBACK_PREBUFFER_FRAMES = "playback_prebuffer_frames"
CONF_AUDIO_STATS_INTERVAL_MS = "audio_stats_interval_ms"

companion_ns = cg.esphome_ns.namespace("realtime_companion")
RealtimeCompanion = companion_ns.class_("RealtimeCompanion", cg.Component)

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(RealtimeCompanion),
        cv.Required(CONF_MICROPHONE): cv.use_id(microphone.Microphone),
        cv.Required(CONF_SPEAKER): cv.use_id(speaker.Speaker),
        cv.Required(CONF_URL): cv.string_strict,
        cv.Required(CONF_TOKEN): cv.string_strict,
        cv.Required(CONF_DEVICE_ID): cv.string_strict,
        cv.Optional(CONF_DEVICE_NAME): cv.string_strict,
        cv.Optional(CONF_OUTPUT_VOLUME, default=0.5): cv.float_range(min=0.0, max=1.0),
        cv.Optional(CONF_VOLUME_PREFERENCE_KEY, default="realtime_companion_output_volume"): cv.string_strict,
        cv.Optional(CONF_READY_KEYWORD, default=False): cv.boolean,
        cv.Optional(CONF_PLAYBACK_PREBUFFER_FRAMES, default=6): cv.int_range(min=1, max=16),
        cv.Optional(CONF_AUDIO_STATS_INTERVAL_MS, default=2000): cv.int_range(min=1000, max=60000),
        cv.Optional(CONF_MIC_GAIN_CONTROLLER): cv.use_id(cg.Component),
        cv.Optional(CONF_STARTUP_GUARD): cv.use_id(cg.Component),
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    # 1.8.0 fixes a potential segfault in the client's variadic error formatter. Audio transport
    # exercises that path whenever a write is interrupted by a reconnect, so do not loosen this pin.
    esp32.add_idf_component(name="espressif/esp_websocket_client", ref="1.8.0")
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    mic = await cg.get_variable(config[CONF_MICROPHONE])
    output = await cg.get_variable(config[CONF_SPEAKER])
    cg.add(var.set_microphone(mic))
    cg.add(var.set_speaker(output))
    cg.add(var.set_url(config[CONF_URL]))
    cg.add(var.set_token(config[CONF_TOKEN]))
    cg.add(var.set_device_id(config[CONF_DEVICE_ID]))
    if CONF_DEVICE_NAME in config:
        cg.add(var.set_device_name(config[CONF_DEVICE_NAME]))
    cg.add(var.set_output_volume(config[CONF_OUTPUT_VOLUME]))
    cg.add(var.set_volume_preference_key(config[CONF_VOLUME_PREFERENCE_KEY]))
    cg.add(var.set_ready_keyword_enabled(config[CONF_READY_KEYWORD]))
    cg.add(var.set_playback_prebuffer_frames(config[CONF_PLAYBACK_PREBUFFER_FRAMES]))
    cg.add(var.set_audio_stats_interval_ms(config[CONF_AUDIO_STATS_INTERVAL_MS]))
    if CONF_MIC_GAIN_CONTROLLER in config:
        cg.add(var.set_mic_gain_controller(await cg.get_variable(config[CONF_MIC_GAIN_CONTROLLER])))
    if CONF_STARTUP_GUARD in config:
        cg.add(var.set_startup_guard(await cg.get_variable(config[CONF_STARTUP_GUARD])))
