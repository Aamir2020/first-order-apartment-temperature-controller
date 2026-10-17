#!/usr/bin/env bash
# Upload files to the ESP32 over Wi-Fi and reboot it.
#
#   ./ota_push.sh                  # uploads main.py
#   ./ota_push.sh main.py ota.py   # uploads the listed files
#   ESP_HOST=192.168.1.73 ./ota_push.sh
#
# Uploading boot.py or ota.py is allowed, but a broken copy of
# either disables OTA and needs a USB cable to recover.
set -euo pipefail

cd "$(dirname "$0")"

ESP_HOST="${ESP_HOST:-192.168.1.73}"
ESP_PORT="${ESP_PORT:-8080}"
TOKEN="$(sed -n 's/^OTA_TOKEN = "\(.*\)"$/\1/p' secrets.py)"
BASE="http://${ESP_HOST}:${ESP_PORT}"

if [ -z "$TOKEN" ]; then
    echo "Could not read OTA_TOKEN from secrets.py" >&2
    exit 1
fi

files=("$@")
if [ ${#files[@]} -eq 0 ]; then
    files=(main.py)
fi

for f in "${files[@]}"; do
    echo "Uploading $f..."
    curl --fail-with-body -sS -X PUT \
        -H "X-OTA-Token: $TOKEN" \
        --data-binary "@$f" \
        "$BASE/files/$(basename "$f")"
done

echo "Rebooting..."
curl --fail-with-body -sS -X POST -H "X-OTA-Token: $TOKEN" "$BASE/reset" || true
