"""Constants for Device Lifecycle."""

DOMAIN = "device_lifecycle"

SUBENTRY_TYPE_PURCHASE = "purchase"
SUBENTRY_TYPE_RUNTIME = "runtime"

CONF_DEVICE_ID = "device_id"
CONF_DEVICE_IDS = "device_ids"
CONF_PURCHASE_NAME = "purchase_name"
CONF_PURCHASE_DATE = "purchase_date"
CONF_INSTALLED_DATE = "installed_date"
CONF_WARRANTY_TYPE = "warranty_type"
CONF_WARRANTY_UNTIL = "warranty_until"
CONF_SELLER = "seller"
CONF_PURCHASE_PRICE = "purchase_price"
CONF_RECEIPT_REFERENCE = "receipt_reference"
CONF_NOTES = "notes"

CONF_RUNTIME_MODE = "runtime_mode"
CONF_SOURCE_ENTITY_ID = "source_entity_id"
CONF_POWER_THRESHOLD = "power_threshold"

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
