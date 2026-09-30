import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import i2c, microphone
from esphome.const import CONF_ID, CONF_MICROPHONE

DEPENDENCIES = ["i2c", "microphone"]

xvf3610_diag_ns = cg.esphome_ns.namespace("xvf3610_diag")
Xvf3610Diag = xvf3610_diag_ns.class_("Xvf3610Diag", cg.PollingComponent, i2c.I2CDevice)

CONFIG_SCHEMA = (
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(Xvf3610Diag),
            cv.Required(CONF_MICROPHONE): cv.use_id(microphone.Microphone),
        }
    )
    .extend(cv.polling_component_schema("2s"))
    .extend(i2c.i2c_device_schema(0x2C))
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await i2c.register_i2c_device(var, config)
    cg.add(var.set_microphone(await cg.get_variable(config[CONF_MICROPHONE])))
