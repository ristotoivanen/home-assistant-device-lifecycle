"""Constants for Device Lifecycle."""

DOMAIN = "device_lifecycle"
CONFIG_ENTRY_VERSION = 4

SUBENTRY_TYPE_PURCHASE = "purchase"
SUBENTRY_TYPE_RUNTIME = "runtime"

# Stable Asset Core references stored inside config subentries. The normalized
# Asset/Purchase objects themselves live in Device Lifecycle storage.
CONF_PURCHASE_UUID = "purchase_uuid"
CONF_ASSET_UUID = "asset_uuid"

CONF_ASSET_NAME = "name"
CONF_CATEGORY = "category"
CONF_MANUFACTURER = "manufacturer"
CONF_MODEL = "model"
CONF_MODEL_ID = "model_id"
CONF_SERIAL_NUMBER = "serial_number"
CONF_SW_VERSION = "sw_version"
CONF_HW_VERSION = "hw_version"

CONF_DEVICE_ID = "device_id"
CONF_DEVICE_IDS = "device_ids"
CONF_PURCHASE_NAME = "purchase_name"
CONF_PURCHASE_DATE = "purchase_date"
CONF_INSTALLED_DATE = "installed_date"
CONF_WARRANTY_TYPE = "warranty_type"
CONF_WARRANTY_UNTIL = "warranty_until"
CONF_SELLER = "seller"
CONF_PURCHASE_PRICE = "purchase_price"
CONF_CURRENCY = "currency"
CONF_RECEIPT_REFERENCE = "receipt_reference"
CONF_RECEIPT_URL = "receipt_url"
CONF_NOTES = "notes"

CONF_DEPLOYMENT_STATE = "deployment_state"
CONF_HA_AREA_ID = "ha_area_id"

CONF_RUNTIME_MODE = "runtime_mode"
CONF_SOURCE_ENTITY_ID = "source_entity_id"
CONF_POWER_THRESHOLD = "power_threshold"
CONF_POWER_HYSTERESIS = "power_hysteresis"

WARRANTY_NONE = "none"
WARRANTY_ONE_YEAR = "1_year"
WARRANTY_TWO_YEARS = "2_years"
WARRANTY_MANUAL = "manual"

WARRANTY_TYPES = (
    WARRANTY_NONE,
    WARRANTY_ONE_YEAR,
    WARRANTY_TWO_YEARS,
    WARRANTY_MANUAL,
)

RUNTIME_MODE_ON = "on_state"
RUNTIME_MODE_POWER = "power"

RUNTIME_MODES = (
    RUNTIME_MODE_ON,
    RUNTIME_MODE_POWER,
)

DEFAULT_POWER_THRESHOLD = 1.0
DEFAULT_POWER_HYSTERESIS = 0.0

DEPLOYMENT_STATE_UNKNOWN = "unknown"
DEPLOYMENT_STATE_NOT_DEPLOYED = "not_deployed"
DEPLOYMENT_STATE_DEPLOYED = "deployed"

DEPLOYMENT_STATES = (
    DEPLOYMENT_STATE_UNKNOWN,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DEPLOYMENT_STATE_DEPLOYED,
)
