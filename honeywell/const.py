"""Constants for the Honeywell Home / Resideo T5 integration."""

DOMAIN = "honeywellt5"
MANUFACTURER = "Honeywell Home"

# ─────────────────────────────────────────────────────────────
# Resideo / Honeywell Home API v2
# ─────────────────────────────────────────────────────────────
API_BASE = "https://api.honeywellhome.com"
OAUTH_AUTHORIZE_URL = f"{API_BASE}/oauth2/authorize"
OAUTH_TOKEN_URL = f"{API_BASE}/oauth2/token"

# Redirect URI = Homey's own OAuth2 callback. Homey catches this redirect via
# its cloud (create_oauth2_callback) and hands the code back to the app
# automatically — no manual copy-paste. This exact URL must be registered as the
# "Callback URL" of the developer app at developer.honeywellhome.com.
DEFAULT_REDIRECT_URI = "https://callback.athom.com/oauth2/callback"

# ─────────────────────────────────────────────────────────────
# Shared developer-app credentials (baked in for the public store)
# ─────────────────────────────────────────────────────────────
# One Resideo developer app serves every user, so they only sign in — no key
# entry. To set this up (once, as the app publisher):
#   1. Create an app at developer.honeywellhome.com
#   2. Set its Callback URL to DEFAULT_REDIRECT_URI (above), exactly
#   3. Copy honeywell/credentials.example.py to honeywell/credentials.py and
#      paste the Consumer Key / Secret there
# The real values live in honeywell/credentials.py, which is git-ignored (kept
# out of the public repo) but DOES ship inside the published Homey package where
# the runtime needs it. A fresh clone without that file falls back to the
# placeholders below. Rate limit is ~20 devices/hour SHARED across all users of
# this app; request an increase from developerinfo@resideo.com if needed.
try:
    from .credentials import CLIENT_ID, CLIENT_SECRET
except ImportError:  # no credentials.py yet (e.g. a fresh clone)
    CLIENT_ID = "REPLACE_WITH_YOUR_CONSUMER_KEY"
    CLIENT_SECRET = "REPLACE_WITH_YOUR_CONSUMER_SECRET"

# Identity subsystem for the authorize step. Resideo unified logins under
# "Resideo ID" (myid.resideo.com); an account created via the First Alert app
# does NOT exist in the legacy Honeywell Home login the bare authorize endpoint
# uses, so its email is rejected ("no valid email"). The First Alert App
# Integration Guide fixes this with a `subSystemId` (or `appSelect`) param on
# /oauth2/authorize: "1" = Resideo, "5" = First Alert. The API itself is
# backward-compatible — only the id FORMATS change (UUID deviceId, bigint
# locationId), which this app already treats as opaque strings.
ACCOUNT_TYPES = {
    "first_alert": {"subSystemId": "5"},   # First Alert app account (Resideo ID)
    "resideo": {"subSystemId": "1"},       # Honeywell Home / Resideo app account
    "select": {"appSelect": "1"},          # show a "First Alert or Resideo" chooser
}
DEFAULT_ACCOUNT_TYPE = "first_alert"

# Access tokens live ~599 s; refresh a bit early. Refresh tokens rotate on every
# refresh, so the newest one must always be persisted back to the device store.
TOKEN_LIFETIME_FALLBACK = 599  # seconds, used when the response omits expires_in
TOKEN_REFRESH_MARGIN = 90      # seconds before expiry to trigger a refresh

# The Resideo API is aggressively rate-limited; poll gently.
DEFAULT_POLL_INTERVAL = 300  # seconds

# ─────────────────────────────────────────────────────────────
# Mode / fan maps  (API value  <->  Homey capability slug)
# ─────────────────────────────────────────────────────────────
# Homey's built-in thermostat_mode enum uses off/heat/cool/auto.
MODE_API_TO_HOMEY = {
    "Off": "off",
    "Heat": "heat",
    "Cool": "cool",
    "Auto": "auto",
    # EmergencyHeat is reported by some systems; surface it as heat.
    "EmergencyHeat": "heat",
}
MODE_HOMEY_TO_API = {
    "off": "Off",
    "heat": "Heat",
    "cool": "Cool",
    "auto": "Auto",
}

# Fan modes. The T5 fan supports Auto / On / Circulate.
FAN_API_TO_HOMEY = {
    "Auto": "auto",
    "On": "on",
    "Circulate": "circulate",
}
FAN_HOMEY_TO_API = {
    "auto": "Auto",
    "on": "On",
    "circulate": "Circulate",
}

# operationStatus.mode  ->  honeywell_operating_state slug
OPERATION_TO_STATE = {
    "EquipmentOff": "idle",
    "Off": "idle",
    "Heat": "heating",
    "Heating": "heating",
    "Cool": "cooling",
    "Cooling": "cooling",
    "Fan": "fan",
}

# LCC (T-series) setpoint hold used when the user changes a value from Homey.
DEFAULT_SETPOINT_STATUS = "TemporaryHold"
