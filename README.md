# First-Order Differential Apartment Temperature Controller

An ESP32 thermostat in MicroPython. It reads the room temperature, gets a
target from a local web server, and switches the heater by moving a servo.
It learns a first-order thermal model of the apartment and uses it to
switch the heater ahead of time. Code updates are sent over Wi-Fi.

## Hardware

| Part | Pin | Notes |
|---|---|---|
| ESP32 (generic, 4 MB) | | MicroPython v1.29, `ESP32_GENERIC-OTA` build |
| Temperature sensor | GPIO 18 | DHT11 data format with a decimal byte (0.1 °C) |
| Hobby servo | GPIO 19 | Moves the heater's thermostat to ON or OFF |

Use a short USB data cable and a 5 V supply of at least 1 A. Poor cables
can drop the voltage enough to reset the ESP32 when Wi-Fi starts.

## Files

| File | Purpose |
|---|---|
| `boot.py` | Connects to Wi-Fi, starts the update server |
| `main.py` | Main loop: sensor, server, heating control |
| `thermal.py` | Thermal model and controller |
| `ota.py` | Update server |
| `ota_push.sh`, `ota_get.sh` | Upload files to, or download files from, the board |
| `secrets.example.py` | Template for `secrets.py` (Wi-Fi details, update token; not in git) |

The board also writes `model.json` (learned model, saved every 15 minutes)
and `log.txt` (heating and model events, rotated at 16 KB).

## Setup

1. Flash the `ESP32_GENERIC-OTA` firmware from
   [micropython.org](https://micropython.org/download/ESP32_GENERIC/)
   (this erases the board):

   ```
   esptool --port /dev/ttyUSB0 erase_flash
   esptool --port /dev/ttyUSB0 write_flash 0x1000 ESP32_GENERIC-OTA-20260824-v1.29.0.bin
   ```

2. Create `secrets.py` from the template, with a random token:

   ```
   cp secrets.example.py secrets.py
   python3 -I -c "import secrets; print(secrets.token_hex(16))"
   ```

3. Adjust the [configuration](#configuration), then copy the files over
   USB once (on WSL2, first run `usbipd attach --wsl --busid <busid>`):

   ```
   mpremote connect /dev/ttyUSB0 fs cp secrets.py :secrets.py \
       + fs cp boot.py :boot.py + fs cp ota.py :ota.py \
       + fs cp thermal.py :thermal.py + fs cp main.py :main.py + reset
   ```

## Updating over Wi-Fi

```
./ota_push.sh                         # upload main.py and reboot
./ota_push.sh thermal.py main.py      # upload several files
./ota_get.sh                          # list files on the board
./ota_get.sh log.txt                  # read the log or model.json
ESP_HOST=192.168.1.50 ./ota_push.sh   # board at another address
```

The update server listens on port 8080 (`PUT /files/<name>`,
`GET /files`, `GET /files/<name>`, `POST /reset`). Every request needs
the header `X-OTA-Token`, which must match `OTA_TOKEN` in `secrets.py`.

- Python files with syntax errors are rejected, and files are swapped in
  only after a complete upload.
- The server starts from `boot.py`, so it keeps running if `main.py`
  crashes. If startup fails, `main.py` reboots after 30 seconds.
- A broken `boot.py`, `ota.py` or `secrets.py` stops Wi-Fi updates, and
  you need USB to recover.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `WEB_SERVER`, `WEB_PORT` | | Server for the target and readings |
| `OUTDOOR_LATITUDE`, `OUTDOOR_LONGITUDE` | Edmonton | Location for the outdoor temperature |
| `HEATER_POWER_WATTS` | 1500 | Only used to log R and C |
| `SERVO_MIN_US`, `SERVO_MAX_US` | 500, 2500 | Servo pulse range |
| `SERVO_OFF_PERCENT`, `SERVO_ON_PERCENT` | 0, 80 | Servo positions for heater off and on |

These are in `main.py`. The controller settings are in `thermal.py` and
are described under [Control law](#4-control-law).

## Web server API

Every 2 seconds the board reads the target with `GET /api/target?esp`,
which must return `{"target": 21.5}`. It also sends readings with
`POST /api/device/report`, with the body
`{"temp": "25.50", "humidity": "9.20"}` (the values are strings).

The outdoor temperature comes from [Open-Meteo](https://open-meteo.com/)
every 15 minutes. It needs no API key.

## How it works

In short: every 15 minutes the board learns how fast the apartment loses
heat and how strongly the heater warms it. Every 2 seconds it predicts
the temperature 15 minutes ahead, with the heater on and with it off, and
switches the heater before the room drifts away from the target.

### 1. Thermal model

The apartment is a single heat capacity $`C`$ (J/°C) connected to the
outdoors through a thermal resistance $`R`$ (°C/W), heated by power $`P`$
(W) that is on for a fraction $`u`$ of the time:

```math
C \, \frac{dT_{in}}{dt} = \frac{T_{out} - T_{in}}{R} + P \, u
\quad\Longrightarrow\quad
\frac{dT_{in}}{dt} = a \, (T_{out} - T_{in}) + b \, u,
\qquad
a = \frac{1}{RC}, \quad b = \frac{P}{C}
```

Here $`a`$ is in h⁻¹, with time constant $`\tau = 1/a`$, and $`b`$ is in
°C/h. Time is in hours because the ESP32 has only 32-bit floats, and
per-second values ($`a \approx 10^{-5}`$) lose too much precision. With
$`T_{out}`$ and $`u`$ held constant, the exact solution is used for
prediction:

```math
T_{in}(t + h) = T_\infty + \bigl( T_{in}(t) - T_\infty \bigr) \, e^{-a h},
\qquad
T_\infty = T_{out} + \frac{b}{a} \, u
```

### 2. Learning samples

Every $`\Delta t = 0.25`$ h, the average indoor temperatures of two
consecutive 15-minute windows give one sample. The regressors describe
the interval itself: its midpoint temperature, and the heater on-fraction
between the window centres.

```math
y_k = \frac{\bar{T}_k - \bar{T}_{k-1}}{\Delta t}
\;\approx\;
\boldsymbol{\theta}^{\mathsf{T}} \mathbf{x}_k,
\qquad
\mathbf{x}_k =
\begin{bmatrix}
T_{out} - \tfrac{1}{2}(\bar{T}_k + \bar{T}_{k-1}) \\
u_k
\end{bmatrix},
\qquad
\boldsymbol{\theta} =
\begin{bmatrix} a \\ b \end{bmatrix}
```

### 3. Recursive least squares

The forgetting factor is $`\lambda = 0.995`$, which keeps about 200
samples, roughly two days:

```math
\begin{aligned}
e_k &= y_k - \boldsymbol{\theta}_{k-1}^{\mathsf{T}} \mathbf{x}_k, &
\mathbf{K}_k &= \frac{\mathbf{P}_{k-1} \mathbf{x}_k}
  {\lambda + \mathbf{x}_k^{\mathsf{T}} \mathbf{P}_{k-1} \mathbf{x}_k}, \\
\boldsymbol{\theta}_k &= \operatorname{clamp}\bigl(
  \boldsymbol{\theta}_{k-1} + \mathbf{K}_k e_k \bigr), &
\mathbf{P}_k &= \tfrac{1}{\lambda} \bigl(
  \mathbf{P}_{k-1} - \mathbf{K}_k \mathbf{x}_k^{\mathsf{T}} \mathbf{P}_{k-1} \bigr)
\end{aligned}
```

| Safeguard | Rule |
|---|---|
| Outliers | skip the sample if $`\lvert e_k \rvert > 8`$ °C/h |
| Covariance limit | skip the division by $`\lambda`$ if $`P_{11} \ge 0.01`$ or $`P_{22} \ge 4`$ |
| Bounds | $`a \in [0.005, 1]`$ h⁻¹, $`b \in [0.05, 10]`$ °C/h |
| Start | $`\boldsymbol{\theta}_0 = [0.05,\ 1]^{\mathsf{T}}`$, $`\mathbf{P}_0 = \operatorname{diag}(0.0025,\ 1)`$; $`\mathbf{P}`$ is kept symmetric |

### 4. Control law

Every 2 seconds the controller works with the filtered indoor
temperature $`\hat{T}`$ and the target $`T^{*}`$. The settings
`PREDICTION_HOURS` $`h = 0.25`$ and `HYSTERESIS` $`\delta = 0.2`$ °C are
in `thermal.py`.

```math
u_{next} =
\begin{cases}
1 & \text{if } T_{in}(t+h \mid u=0) < T^{*} \\
0 & \text{if } T_{in}(t+h \mid u=1) > T^{*} + \delta \\
u_{now} & \text{otherwise}
\end{cases}
\qquad
\hat{T}_n = \hat{T}_{n-1} + 0.05 \, (T_n - \hat{T}_{n-1})
```

The heater only switches if it has been in its current state for at least
`MIN_ON_SECONDS` / `MIN_OFF_SECONDS` (600 s), or if the target has just
changed. That makes this a simple model predictive controller with two
inputs, $`u \in \{0, 1\}`$. Without an outdoor temperature it falls back
to plain on/off control, and without a target the heater stays off. The
servo gets a signal for 1 second per switch only, so it doesn't buzz.

### 5. Testing

The model and controller were tested in a 5-day simulation with a night
setback, mild and Edmonton winter weather, and 1 °C and 0.1 °C sensors.

- During the day the room stayed mostly within about ±0.5 °C of the
  target.
- The 10-minute minimum time halved switching, to about 36–115 switches
  per day.
- With 1 °C readings, $`b/a`$ was learned almost exactly, but $`a`$ and
  $`b`$ came out 20–45 % low. With 0.1 °C readings they were within
  about 5 %.

## Limitations

- **Building heat.** With central heating, much of the heat comes from the
  building at unpredictable times, and the thermostat can only add heat on
  top of it. A background term $`c`$ in $`\dot{T}_{in}`$ was tested in
  simulation and on real data. It did not improve predictions, so it is
  not included.
- **Sensor.** DHT11-grade sensors are accurate to about ±1–2 °C, and their
  humidity readings below 20 % are unreliable.
- **Security.** Updates use plain HTTP protected only by the token. Use
  them on a trusted home network only.

## License

MIT. See [LICENSE](LICENSE).
