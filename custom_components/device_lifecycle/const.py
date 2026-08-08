"""Constants for Device Lifecycle."""

DOMAIN = "device_lifecycle"
SUBENTRY_TYPE_PURCHASE = "purchase"

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
