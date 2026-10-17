#!/usr/bin/env bash
# Download a file from the ESP32 over Wi-Fi and print it.
#
#   ./ota_get.sh log.txt
#   ./ota_get.sh model.json
#   ./ota_get.sh            # list files
set -euo pipefail

cd "$(dirname "$0")"

ESP_HOST="${ESP_HOST:-192.168.1.73}"
ESP_PORT="${ESP_PORT:-8080}"
TOKEN="$(sed -n 's/^OTA_TOKEN = "\(.*\)"$/\1/p' secrets.py)"

curl --fail-with-body -sS -H "X-OTA-Token: $TOKEN" \
    "http://${ESP_HOST}:${ESP_PORT}/files${1:+/$1}"
