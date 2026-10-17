import network
import time

from secrets import WIFI_SSID, WIFI_PASSWORD, OTA_TOKEN

network.hostname("thermostat")

wlan = network.WLAN(network.WLAN.IF_STA)
wlan.active(True)

if not wlan.isconnected():
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)

for _ in range(30):
    if wlan.isconnected():
        break
    time.sleep(1)

if wlan.isconnected():
    print("Wi-Fi connected:", wlan.ifconfig())
else:
    print("Wi-Fi connection failed")

# Start the OTA server even without Wi-Fi; it begins accepting
# connections once main.py reconnects.
try:
    import ota
    ota.start(OTA_TOKEN)
except Exception as exc:
    print("OTA start failed:", exc)
