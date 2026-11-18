"""Constants for the Luke Roberts integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "luke_roberts"
MANUFACTURER: Final = "Luke Roberts"

# --- Bluetooth LE (Lamp Control API v1.3) -----------------------------------
SERVICE_UUID: Final = "44092840-0567-11e6-b862-0002a5d5c51b"
API_CHAR_UUID: Final = "44092842-0567-11e6-b862-0002a5d5c51b"
# Active scene id, one byte (read and notify). Not part of the Lamp Control API
# v1.3 document; confirmed on a Model F with firmware 1.2.2.
SCENE_CHAR_UUID: Final = "44092844-0567-11e6-b862-0002a5d5c51b"
# Scene records, read with the Sync command and written like the Luke Roberts
# app 2.4.0 does. The lamp answers writes on the API characteristic. Not in the
# Lamp Control API; verified on a Model F with firmware 1.2.2.
SETTINGS_CHAR_UUID: Final = "44092841-0567-11e6-b862-0002a5d5c51b"
SCENE_RECORD_CHAR_UUID: Final = "44092843-0567-11e6-b862-0002a5d5c51b"
# How long a Sync may take: about 2 s for 20 scenes through a proxy.
SYNC_TIMEOUT: Final = 15.0

# Device Information Service (0x180A)
DIS_MANUFACTURER_UUID: Final = "00002a29-0000-1000-8000-00805f9b34fb"
DIS_MODEL_UUID: Final = "00002a24-0000-1000-8000-00805f9b34fb"
DIS_SERIAL_UUID: Final = "00002a25-0000-1000-8000-00805f9b34fb"
DIS_FIRMWARE_UUID: Final = "00002a26-0000-1000-8000-00805f9b34fb"
DIS_HARDWARE_UUID: Final = "00002a27-0000-1000-8000-00805f9b34fb"
DIS_SOFTWARE_UUID: Final = "00002a28-0000-1000-8000-00805f9b34fb"

# The lamp drops idle links after 8 s (firmware >= 1.1.1). We keep the link
# alive with pings for a short window after the last command so follow-up
# commands are fast, then let go so the Luke Roberts app can connect again.
KEEPALIVE_INTERVAL: Final = 5.0
DEFAULT_LINGER: Final = 30

RESPONSE_TIMEOUT: Final = 5.0

# After switching the power on, how long to wait for the lamp to boot and
# answer over Bluetooth (it takes a few seconds, more through a proxy).
POWER_UP_TIMEOUT: Final = 20.0

# --- Cloud (Third party REST API v1.0) --------------------------------------
CLOUD_BASE_URL: Final = "https://cloud.luke-roberts.com/api/v1"
CLOUD_APP_URL: Final = "https://cloud.luke-roberts.com/app"
ACCOUNT_UNIQUE_ID: Final = "account"

# --- Firmware lookup (endpoint used by the Luke Roberts app 2.4.0) ----------
FIRMWARE_BASE_URL: Final = "https://luke-roberts-firmware.firebaseapp.com/api/v1"

# --- Config entry keys ------------------------------------------------------
CONF_ACCOUNT: Final = "account"
CONF_FIRMWARE_KEY: Final = "firmware_key"
CONF_API_KEY: Final = "api_key"
CONF_CLOUD_LAMP_ID: Final = "cloud_lamp_id"
CONF_CONNECTION_MODE: Final = "connection_mode"
CONF_LINGER: Final = "linger"
CONF_DEFAULT_SCENE: Final = "default_scene"
CONF_POWER_ENTITY: Final = "power_entity"

MODE_LOCAL_FIRST: Final = "local_first"
MODE_LOCAL_ONLY: Final = "local_only"
MODE_CLOUD_FIRST: Final = "cloud_first"
MODE_CLOUD_ONLY: Final = "cloud_only"
CONNECTION_MODES: Final = [
    MODE_LOCAL_FIRST,
    MODE_LOCAL_ONLY,
    MODE_CLOUD_FIRST,
    MODE_CLOUD_ONLY,
]

# --- Lamp limits -------------------------------------------------------------
MIN_KELVIN: Final = 2700
MAX_KELVIN: Final = 4000

SCENE_OFF: Final = 0x00
SCENE_DEFAULT: Final = 0xFF

# Opcode 08 Relative Brightness takes a signed change in percentage points.
# The Lamp Control API v1.3 describes a multiplication by PP/100, but the lamp
# behaves like the Luke Roberts app 2.4.0 labels it ("+20 % brighter").
RELATIVE_BRIGHTNESS_STEP: Final = 20

# Cloud API accepts scene ids 0..31; used when names are unknown (cloud only).
CLOUD_MAX_SCENE_ID: Final = 31

# --- Services ----------------------------------------------------------------
SERVICE_IMMEDIATE_LIGHT: Final = "immediate_light"
SERVICE_REFRESH_SCENES: Final = "refresh_scenes"
SERVICE_BACKUP_SCENES: Final = "backup_scenes"
SERVICE_RENAME_SCENE: Final = "rename_scene"
SERVICE_RESTORE_SCENE: Final = "restore_scene"
