import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import audio_dac, i2c, microphone
from esphome.const import CONF_AUDIO_DAC, CONF_ID, CONF_MICROPHONE

DEPENDENCIES = ["i2c", "audio_dac", "microphone"]
CONF_AEC_DIAGNOSTICS = "aec_diagnostics"
CONF_FFVA_MODE = "ffva_mode"

xvf3610_audio_ns = cg.esphome_ns.namespace("xvf3610_audio")
Xvf3610Audio = xvf3610_audio_ns.class_("Xvf3610Audio", cg.Component, i2c.I2CDevice)

CONFIG_SCHEMA = (
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(Xvf3610Audio),
            cv.Required(CONF_AUDIO_DAC): cv.use_id(audio_dac.AudioDac),
            cv.Required(CONF_MICROPHONE): cv.use_id(microphone.Microphone),
            cv.Optional(CONF_AEC_DIAGNOSTICS, default=False): cv.boolean,
            cv.Optional(CONF_FFVA_MODE, default=False): cv.boolean,
        }
    )
    .extend(cv.COMPONENT_SCHEMA)
    .extend(i2c.i2c_device_schema(0x2C))
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await i2c.register_i2c_device(var, config)
    cg.add(var.set_audio_dac(await cg.get_variable(config[CONF_AUDIO_DAC])))
    cg.add(var.set_microphone(await cg.get_variable(config[CONF_MICROPHONE])))
    cg.add(var.set_aec_diagnostics(config[CONF_AEC_DIAGNOSTICS]))
    cg.add(var.set_ffva_mode(config[CONF_FFVA_MODE]))
