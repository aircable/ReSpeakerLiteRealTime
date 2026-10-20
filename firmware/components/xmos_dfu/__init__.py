import hashlib
from pathlib import Path

import esphome.automation as automation
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import i2c
from esphome.const import CONF_ID, CONF_RAW_DATA_ID
from esphome.core import CORE, HexInt

DEPENDENCIES = ["i2c"]

CONF_FILE = "file"
CONF_AUTO_UPDATE = "auto_update"
CONF_SOURCE_VERSION = "source_version"
CONF_TARGET_VERSION = "target_version"
CONF_SHA256 = "sha256"

xmos_dfu_ns = cg.esphome_ns.namespace("xmos_dfu")
XmosDfu = xmos_dfu_ns.class_("XmosDfu", cg.Component, i2c.I2CDevice)
XmosDfuFlashAction = xmos_dfu_ns.class_("XmosDfuFlashAction", automation.Action)

def _version_parts(value):
    parts = value.split(".")
    if len(parts) != 3 or not all(part.isdecimal() and 0 <= int(part) <= 255 for part in parts):
        raise cv.Invalid(f"XMOS version must be three bytes, e.g. 1.1.0: {value}")
    return tuple(int(part) for part in parts)


def _validate_auto_update(config):
    if config[CONF_AUTO_UPDATE]:
        for key in (CONF_SOURCE_VERSION, CONF_TARGET_VERSION, CONF_SHA256):
            if key not in config:
                raise cv.Invalid(f"{key} is required when auto_update is enabled")
        if _version_parts(config[CONF_SOURCE_VERSION]) == _version_parts(config[CONF_TARGET_VERSION]):
            raise cv.Invalid("source_version and target_version must differ")
    for key in (CONF_SOURCE_VERSION, CONF_TARGET_VERSION):
        if key in config:
            _version_parts(config[key])
    if CONF_SHA256 in config and (len(config[CONF_SHA256]) != 64 or
                                   any(c not in "0123456789abcdefABCDEF" for c in config[CONF_SHA256])):
        raise cv.Invalid("sha256 must be a 64-character hexadecimal digest")
    return config


CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(XmosDfu),
            cv.Required(CONF_FILE): cv.file_,
            cv.Optional(CONF_AUTO_UPDATE, default=False): cv.boolean,
            cv.Optional(CONF_SOURCE_VERSION): cv.string_strict,
            cv.Optional(CONF_TARGET_VERSION): cv.string_strict,
            cv.Optional(CONF_SHA256): cv.string_strict,
            cv.GenerateID(CONF_RAW_DATA_ID): cv.declare_id(cg.uint8),
        }
    )
    .extend(cv.COMPONENT_SCHEMA)
    .extend(i2c.i2c_device_schema(0x42)),
    _validate_auto_update,
)

FLASH_ACTION_SCHEMA = cv.Schema({cv.GenerateID(): cv.use_id(XmosDfu)})


@automation.register_action(
    "xmos_dfu.flash", XmosDfuFlashAction, FLASH_ACTION_SCHEMA, synchronous=False
)
async def flash_action_to_code(config, action_id, template_arg, args):
    parent = await cg.get_variable(config[CONF_ID])
    return cg.new_Pvariable(action_id, template_arg, parent)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await i2c.register_i2c_device(var, config)

    path: Path = CORE.relative_config_path(config[CONF_FILE])
    try:
        image = path.read_bytes()
    except OSError as err:
        raise cv.Invalid(f"Could not read XMOS firmware image {path}: {err}") from err
    if not image:
        raise cv.Invalid(f"XMOS firmware image is empty: {path}")
    if CONF_SHA256 in config:
        digest = hashlib.sha256(image).hexdigest()
        if digest.lower() != config[CONF_SHA256].lower():
            raise cv.Invalid(f"XMOS image SHA-256 mismatch for {path}: {digest}")

    data = cg.progmem_array(config[CONF_RAW_DATA_ID], [HexInt(value) for value in image])
    cg.add(var.set_firmware(data, len(image), path.name))
    cg.add(var.set_auto_update(config[CONF_AUTO_UPDATE]))
    if config[CONF_AUTO_UPDATE]:
        cg.add(var.set_source_version(*_version_parts(config[CONF_SOURCE_VERSION])))
        cg.add(var.set_target_version(*_version_parts(config[CONF_TARGET_VERSION])))
