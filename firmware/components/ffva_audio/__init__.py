import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import audio_dac, i2c, microphone
from esphome.const import CONF_AUDIO_DAC, CONF_MICROPHONE

DEPENDENCIES = ["i2c", "audio_dac", "microphone"]

ffva_audio_ns = cg.esphome_ns.namespace("ffva_audio")
FfvaAudio = ffva_audio_ns.class_("FfvaAudio", cg.Component, i2c.I2CDevice)

CONFIG_SCHEMA = (
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(FfvaAudio),
            cv.Required(CONF_AUDIO_DAC): cv.use_id(audio_dac.AudioDac),
            cv.Required(CONF_MICROPHONE): cv.use_id(microphone.Microphone),
        }
    )
    .extend(cv.COMPONENT_SCHEMA)
    .extend(i2c.i2c_device_schema(0x18))
)


async def to_code(config):
    var = cg.new_Pvariable(config["id"])
    await cg.register_component(var, config)
    await i2c.register_i2c_device(var, config)
    cg.add(var.set_audio_dac(await cg.get_variable(config[CONF_AUDIO_DAC])))
    cg.add(var.set_microphone(await cg.get_variable(config[CONF_MICROPHONE])))
