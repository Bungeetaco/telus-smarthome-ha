"""Constants for the TELUS SmartHome+ integration.

All values below were reverse-engineered from the TELUS SmartHome+ Android app
v1.74.0 (com.telus.smarthome). See claude/telus-smarthome-api-map.md in the
project for the full map and provenance.

NOTE: the API keys and client_id are embedded in the app and MAY ROTATE on an
app update. If auth suddenly starts failing with 401/403, re-extract them from a
newer APK or capture a live request. This is expected for this kind of
interoperability integration.
"""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "telus_smarthome_plus"

# ---------------------------------------------------------------------------
# Hosts (production)
# ---------------------------------------------------------------------------
# Main "Platform" API (current). Everything the integration needs lives here.
PLATFORM_HOST = "https://api.end-user.prod.telus.smarthomeplus.com"
# Legacy FEBE BFF (api/v1, api/v2). Kept for reference / fallback only.
FEBE_HOST = "https://telus.febe.prod.smarthomeplus.com"

# NOTE: the app does NOT open a browser OAuth page. It performs a HEADLESS
# username/password login (see FebeLoginRepository):
#   1. GET  acr/idpurl           -> the real login URL (loginUrl)  [server-driven]
#   2. POST <loginUrl>           -> session cookies (Set-Cookie)
#   3. GET  acr/token (Cookie:)  -> access + refresh token
# The hardcoded auth-gateway-pr.telus.com host is an INTERNAL PingFederate
# gateway that the app never contacts directly (it only appears as idp
# parameters); that is why opening it in a browser fails with NXDOMAIN. Always
# use the loginUrl returned by acr/idpurl at runtime.
OAUTH_CLIENT_ID = "f9c805a3-70ae-44da-ac9a-a46097f316b7"  # informational only
# authDomain field sent in the LoginRequest body. Defaults to "telus"; override
# in the config flow if a live capture shows otherwise.
DEFAULT_AUTH_DOMAIN = "telus"

# x-api-key candidates found in the auth/network code. One is for the Platform
# API, one likely for FEBE. Confirm by capturing a real request; set the right
# one as PLATFORM_API_KEY.
# Confirmed from the app: BuildConfig.API_KEY (used across every feature module)
# is the gateway x-api-key. The other string found in auth code is NOT the
# gateway key (using it returns 403 {"message":"Forbidden"}).
PLATFORM_API_KEY = "TNUrbgYIhXjDvxDVmTsUcfoojeWfdhBd"

# User-Agent the app presents. TELUS fronts the API with Cloudflare/a WAF, so
# send the exact app UA (confirmed from app 1.76.0) rather than a generic okhttp
# string that a WAF rule can single out.
USER_AGENT = "smarthomeplus/1.76.0 (com.telus.smarthome; build:198; Android 14)"

# ---------------------------------------------------------------------------
# Platform API paths (relative to PLATFORM_HOST)
# ---------------------------------------------------------------------------
# Auth — headless end-user (acr) flow
PATH_IDP_URL = "/authentication-management/end-user/acr/idpurl"
PATH_DISCOVERY = "/profile-management/discovery"  # alt: returns idpUrl/token URLs
PATH_ACR_TOKEN = "/authentication-management/end-user/acr/token"
PATH_ACR_REFRESH = "/authentication-management/end-user/acr/refresh-token"

# Bootstrap
PATH_MY_PROFILE = "/profile-management/profiles/my-profile"
PATH_HOUSEHOLDS = "/profile-management/profiles/{profile_id}/households"
PATH_HOMESTATE = "/profile-management/households/{household_id}/homestate"

# Devices
PATH_DEVICES = "/presentation-management/household/{household_id}/devices"
PATH_DEVICE = "/presentation-management/household/{household_id}/devices/{device_id}"
PATH_REFRESH_STATUS = "/presentation-management/assets/refreshDeviceStatus"
PATH_SEND_COMMANDS = (
    "/asset-management/cloud-asset-manager/asset-control/commands/send-commands"
)

# Automations
PATH_USERRULES = "/presentation-management/households/{household_id}/userrules"
PATH_USERRULE_EXECUTE = (
    "/presentation-management/households/{household_id}/userrules/{rule_id}/execute"
)

# Camera / video
PATH_LIVE_CONFIG = (
    "/video-management/video-play/{household_id}/live-communication/devices/"
    "{device_id}?protocols=[\"WSS\",\"HTTPS\"]&role=VIEWER"
)
PATH_LIVE_SESSION_DELETE = (
    "/video-management/video-play/{household_id}/live-communication/devices/"
    "{device_id}/sessions/{session_id}"
)
PATH_SIGNALING_OFFER = "/video-management/signaling/{household_id}"
PATH_SIGNALING_ANSWER = "/video-management/signaling/{household_id}/session/{session_id}"
PATH_CLIPS = "/video-management/clips/{household_id}?thumbnail=MOBILE"
PATH_CLIP = "/video-management/clips/{household_id}/clip/{clip_id}?thumbnail=MOBILE"
PATH_CLIP_STREAM = "/video-management/streaming/{household_id}/clip/{clip_id}"

# ---------------------------------------------------------------------------
# Config-entry data keys
# ---------------------------------------------------------------------------
CONF_USERNAME = "username"                    # TELUS email (login id)
CONF_PASSWORD = "password"                    # stored to allow silent re-login
CONF_AUTH_DOMAIN = "auth_domain"
CONF_ACCESS_TOKEN = "access_token"            # platform access token
CONF_REFRESH_TOKEN = "refresh_token"          # platform refresh token
CONF_PROFILE_ID = "profile_id"
CONF_HOUSEHOLD_ID = "household_id"
CONF_BRIDGE_URL = "bridge_url"                # KVS->RTSP bridge base URL
CONF_AUTO_SAVE = "auto_save_clips"            # save new clips to /media
CONF_SAVE_FOLDER = "save_folder"                # where clips are saved (default <media>/telus_clips)
CONF_KEEP_DAYS = "keep_days"                  # delete saved clips older than N days (0 = never)
DEFAULT_KEEP_DAYS = 14

# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------
UPDATE_INTERVAL = timedelta(seconds=30)
REQUEST_TIMEOUT = 30
MANUFACTURER = "TELUS"

# How often the coordinator asks the backend to pull LIVE telemetry from the
# physical camera devices (POST refreshDeviceStatus). GET /devices otherwise
# returns backend-CACHED resource values, so wifi signal, temperature and
# connectivity freeze at their last app-triggered refresh. This POST pokes the
# device's radio, so it runs once every REFRESH_STATUS_EVERY polls, NOT every
# poll (6 * 30s = ~3 min).
REFRESH_STATUS_EVERY = 6

# The aiSensor resource reports value:true while detecting and carries an epoch
# timestamp. The backend cache holds the last event's value:true until the next
# refresh/clear, so the motion binary_sensor honours value:true only while its
# timestamp is within this window; past it, motion is treated as cleared. This
# is what stops the sensor latching ON indefinitely.
MOTION_AUTO_OFF_SECONDS = 60

# ---------------------------------------------------------------------------
# Capability -> HA platform mapping. Capability identifiers come from the app's
# capability model (BinarySwitchCapability, BrightnessCapability, ...). The
# exact JSON identifier strings must be confirmed from a live device-list
# response; the keys below are the normalized names we match on.
# ---------------------------------------------------------------------------
CAP_BINARY_SWITCH = "binarySwitch"
CAP_BRIGHTNESS = "brightness"
CAP_DIMMING = "dimming"
CAP_COLOR_HS = "colorHueSaturation"
CAP_COLOR_TEMP = "colorTemperature"
CAP_DOOR_STATE = "doorState"        # locks
CAP_LOCK = "lock"
CAP_ALARM = "alarm"
CAP_BATTERY = "battery"
CAP_HUMIDITY = "humidity"
CAP_ENERGY_POWER = "energyPower"
CAP_TEMPERATURE = "temperatureMeasurement"
CAP_CONTACT = "contactSensor"
CAP_MOTION = "motionSensor"
CAP_CONNECTION = "connection"
CAP_CAMERA = "camera"
CAP_THERMOSTAT = "thermostat"
CAP_FAN_MODE = "fanMode"
