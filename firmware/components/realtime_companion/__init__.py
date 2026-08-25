import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import esp32, microphone, speaker
from esphome.const import CONF_ID, CONF_MICROPHONE, CONF_SPEAKER

DEPENDENCIES = ["wifi"]
AUTO_LOAD = ["audio"]

CONF_URL = "url"
CONF_TOKEN = "token"
CONF_DEVICE_ID = "device_id"

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
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    esp32.add_idf_component(name="espressif/esp_websocket_client", ref="1.4.0")
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    mic = await cg.get_variable(config[CONF_MICROPHONE])
    output = await cg.get_variable(config[CONF_SPEAKER])
    cg.add(var.set_microphone(mic))
    cg.add(var.set_speaker(output))
    cg.add(var.set_url(config[CONF_URL]))
    cg.add(var.set_token(config[CONF_TOKEN]))
    cg.add(var.set_device_id(config[CONF_DEVICE_ID]))
