# Private settings. Copy this file to secrets.py and fill in your
# own values; secrets.py is listed in .gitignore.

WIFI_SSID = "your-wifi-name"
WIFI_PASSWORD = "your-wifi-password"

# Shared secret for OTA uploads; ota_push.sh and ota_get.sh read
# it from secrets.py. Generate one with:
#   python3 -I -c "import secrets; print(secrets.token_hex(16))"
OTA_TOKEN = "replace-with-a-random-token"
