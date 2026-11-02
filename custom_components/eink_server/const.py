"""Constants for the eink-server integration."""

DOMAIN = "eink_server"
MANUFACTURER = "Visionect"

# Port tablets are configured to connect to.
DEFAULT_PORT = 11113

STORAGE_VERSION = 1
STORAGE_SAVE_DELAY = 10

# Dispatcher signals; per-tablet ones are suffixed with the UUID.
SIGNAL_NEW_TABLET = f"{DOMAIN}_new_tablet"
SIGNAL_UPDATE = f"{DOMAIN}_update"
SIGNAL_TOUCH = f"{DOMAIN}_touch"
