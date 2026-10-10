"""Constants for the Tuya BLE Lock integration."""

DOMAIN = "tuya_ble_lock"
EVENT_TUYA_BLE_LOCK = f"{DOMAIN}_event"
DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 30
MAX_SCAN_INTERVAL = 600
CONF_GATEWAY_ID = "gateway_id"
CONF_LOCAL_KEY = "local_key"
CONF_VERSION = "protocol_version"
CONF_LOCKS = "locks"
CONF_MEMBER = "member"
CONF_REMOTE_CODE = "remote_unlock_code"
CONF_NODE_ID = "node_id"
CONF_PROFILE = "confirm_profile"
CONF_PAYLOAD = "unlock_payload"
CONF_STATE_SOURCE = "state_source"
STATE_SOURCE_MOTOR_DP47 = "motor_dp47"
STATE_SOURCE_HX201_DP40 = "hx201_dp40"
HX201_PRODUCT_ID = "ahz7wpy9"
