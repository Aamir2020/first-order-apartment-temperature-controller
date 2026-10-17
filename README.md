# First-Order Differential Apartment Temperature Controller

An ESP32 thermostat written in MicroPython. It reads the room temperature,
gets a target temperature from a web server, and turns a heater on or off
by moving a servo. It learns how the apartment warms and cools and uses
that to switch the heater a little ahead of time.

Code updates are sent over Wi-Fi, so the board only needs a USB cable for
the first install.

## Hardware

| Part | Pin | Notes |
|---|---|---|
| ESP32 (generic, 4 MB) | | Runs MicroPython v1.29 (`ESP32_GENERIC-OTA` build) |
| Temperature sensor | GPIO 18 | DHT11 data format with a decimal byte (0.1 °C) |
| Hobby servo | GPIO 19 | Moves the heater's thermostat to ON or OFF |

Use a short, good-quality USB data cable and a 5 V supply of at least 1 A.
Long or cheap cables can drop the voltage enough to reset the ESP32 when
Wi-Fi switches on.

## Files

| File | Purpose |
|---|---|
| `boot.py` | Connects to Wi-Fi and starts the update server |
| `secrets.example.py` | Template for `secrets.py` (Wi-Fi password, update token) |
| `main.py` | Main loop: sensor, server reports, heating control |
| `thermal.py` | Thermal model and predictive heating controller |
| `ota.py` | Update server for sending files over Wi-Fi |
| `ota_push.sh` | Upload files to the board and reboot it |
| `ota_get.sh` | Download files from the board (log, model) |

Files the board creates itself:

| File | Purpose |
|---|---|
| `model.json` | Learned model, saved every 15 minutes |
| `log.txt` | Heating and model events (rotated to `log.txt.old` at 16 KB) |

## Setup

1. Download the `ESP32_GENERIC-OTA` firmware from
   [micropython.org](https://micropython.org/download/ESP32_GENERIC/)
   and flash it (this erases the board):

   ```
   esptool --port /dev/ttyUSB0 erase_flash
   esptool --port /dev/ttyUSB0 write_flash 0x1000 ESP32_GENERIC-OTA-20260824-v1.29.0.bin
   ```

2. Create `secrets.py` and fill in your Wi-Fi details and a random
   update token:

   ```
   cp secrets.example.py secrets.py
   python3 -I -c "import secrets; print(secrets.token_hex(16))"
   ```

3. Edit the other settings (see [Configuration](#configuration)).

4. Copy the files over USB the first time:

   ```
   mpremote connect /dev/ttyUSB0 fs cp secrets.py :secrets.py \
       + fs cp boot.py :boot.py + fs cp ota.py :ota.py \
       + fs cp thermal.py :thermal.py + fs cp main.py :main.py + reset
   ```

On WSL2, attach the board first from Windows with
`usbipd attach --wsl --busid <busid>`.

## Updating over Wi-Fi

```
./ota_push.sh                    # upload main.py and reboot
./ota_push.sh thermal.py main.py # upload several files
./ota_get.sh                     # list files on the board
./ota_get.sh log.txt             # read the log
./ota_get.sh model.json          # read the learned model
ESP_HOST=192.168.1.50 ./ota_push.sh   # board at a different address
```

The update server runs on port 8080:

| Request | Action |
|---|---|
| `PUT /files/<name>` | Save a file |
| `GET /files` | List files |
| `GET /files/<name>` | Download a file |
| `POST /reset` | Reboot |

Every request needs the header `X-OTA-Token`, which must match
`OTA_TOKEN` in `secrets.py`.

How it stays safe:

- Python files with syntax errors are rejected and the old file is kept.
- Files are written to a temporary file and then renamed, so a dropped
  upload never leaves a half-written file.
- The server starts from `boot.py`, so it keeps running even if
  `main.py` crashes. If startup fails, `main.py` reboots after 30 seconds.
- Be careful sending `boot.py`, `ota.py` or `secrets.py`: if any of them is
  broken, Wi-Fi updates stop and you need USB to recover.

A DHCP reservation on the router keeps the board at the same address.

## Configuration

`secrets.py` (not in git): `WIFI_SSID`, `WIFI_PASSWORD` and `OTA_TOKEN`.

`main.py`:

| Setting | Default | Meaning |
|---|---|---|
| `WEB_SERVER`, `WEB_PORT` | | Server that provides the target and receives readings |
| `OUTDOOR_LATITUDE`, `OUTDOOR_LONGITUDE` | Edmonton | Location for the outdoor temperature |
| `HEATER_POWER_WATTS` | 1500 | Only used to print R and C in physical units |
| `SERVO_MIN_US`, `SERVO_MAX_US` | 500, 2500 | Servo pulse range |
| `SERVO_OFF_PERCENT`, `SERVO_ON_PERCENT` | 0, 80 | Servo positions for heating off and on |

`thermal.py` (`HeatingController`):

| Setting | Default | Meaning |
|---|---|---|
| `PREDICTION_HOURS` | 0.25 | How far ahead it predicts |
| `HYSTERESIS` | 0.2 °C | Allowed overshoot above the target |
| `MIN_ON_SECONDS`, `MIN_OFF_SECONDS` | 600 | Minimum time before switching again |
| `SAMPLE_SECONDS` | 900 | How often the model learns |

## Web server API

Every 2 seconds the board:

- reads the target with `GET /api/target?esp`, which must return
  `{"target": 21.5}`
- sends readings with `POST /api/device/report`, using the body
  `{"temp": "25.50", "humidity": "9.20"}` (the values are strings)

The outdoor temperature comes from [Open-Meteo](https://open-meteo.com/),
which needs no API key. It is refreshed every 15 minutes.

## How the heating control works

The apartment is modelled as:

```
dT/dt = a * (Tout - Tin) + b * heater
```

- `a` is how fast the apartment loses heat to outside (per hour; `1/a` is
  the time constant in hours).
- `b` is how fast the heater warms the room (°C per hour).
- `heater` is the fraction of time the heater was on.

**Learning.** Every 15 minutes the model compares how much the room
temperature changed with what it predicted, and adjusts `a` and `b`
(recursive least squares with a forgetting factor of 0.995, which keeps
about two days of memory). Time is in hours because the ESP32 build only
has 32-bit floats. The estimates are kept within physical limits, very
large errors are skipped, and the model is saved to `model.json` so
learning survives reboots.

**Control.** Every 2 seconds the controller predicts the temperature
15 minutes ahead:

- If the room would fall below the target with the heater off, it turns
  the heater on.
- If the room would go above target + 0.2 °C with the heater on, it turns
  the heater off.
- Otherwise it leaves the heater as it is.

The heater must stay on or off for at least 10 minutes before switching
again, unless the target changes, which takes effect on the next check.
If the outdoor temperature is unknown, it falls back to plain on/off
control. With no target from the server, the heater stays off.

**Servo.** The servo gets a position signal for 1 second when the heater
switches, then the signal stops, so the servo does not buzz or draw
current while holding a position.

## Control theory

This section gives the equations behind `thermal.py`.

### 1. Thermal model

The apartment is modelled as a single heat capacity $`C`$ (J/°C)
connected to the outdoors through a thermal resistance $`R`$ (°C/W), with
a heater of power $`P`$ (W) that is on for a fraction $`u`$ of the time:

```math
C \, \frac{dT_{in}}{dt} = \frac{T_{out} - T_{in}}{R} + P \, u(t)
```

Dividing by $`C`$ gives the first-order form used in the code:

```math
\frac{dT_{in}}{dt} = a \, (T_{out} - T_{in}) + b \, u,
\qquad
a = \frac{1}{RC} \;\; [\mathrm{h^{-1}}],
\qquad
b = \frac{P}{C} \;\; [\mathrm{°C/h}],
\qquad
u \in [0, 1]
```

The time constant is $`\tau = RC = 1/a`$. The physical values are
recovered with (time in hours, hence the factor 3600):

```math
C = \frac{3600 \, P}{b},
\qquad
R = \frac{b}{a P},
\qquad
\tau = \frac{1}{a}
```

### 2. Measurements for learning

Every $`\Delta t = 0.25`$ h, the average indoor temperatures of two
consecutive 15-minute windows, $`\bar{T}_{k-1}`$ and $`\bar{T}_k`$, give
one sample:

```math
y_k = \frac{\bar{T}_k - \bar{T}_{k-1}}{\Delta t},
\qquad
\mathbf{x}_k =
\begin{bmatrix}
T_{out} - \dfrac{\bar{T}_k + \bar{T}_{k-1}}{2} \\[6pt]
u_k
\end{bmatrix},
\qquad
y_k \approx \boldsymbol{\theta}^{\mathsf{T}} \mathbf{x}_k,
\qquad
\boldsymbol{\theta} =
\begin{bmatrix} a \\ b \end{bmatrix}
```

The indoor temperature in $`\mathbf{x}_k`$ is taken at the midpoint of the
interval, and $`u_k`$ is the fraction of time the heater was on between
the two window centres. Both describe the interval itself rather than its
end.

### 3. Recursive least squares with forgetting

For each sample, the parameters and their covariance $`\mathbf{P}`$ are
updated with a forgetting factor $`\lambda = 0.995`$:

```math
\begin{aligned}
e_k &= y_k - \boldsymbol{\theta}_{k-1}^{\mathsf{T}} \mathbf{x}_k
  && \text{prediction error} \\
\mathbf{K}_k &= \frac{\mathbf{P}_{k-1} \mathbf{x}_k}
  {\lambda + \mathbf{x}_k^{\mathsf{T}} \mathbf{P}_{k-1} \mathbf{x}_k}
  && \text{gain} \\
\boldsymbol{\theta}_k &= \operatorname{clamp}\!\left(
  \boldsymbol{\theta}_{k-1} + \mathbf{K}_k e_k \right)
  && \text{parameter update} \\
\mathbf{P}_k &= \frac{1}{\lambda} \left(
  \mathbf{P}_{k-1} - \mathbf{K}_k \mathbf{x}_k^{\mathsf{T}} \mathbf{P}_{k-1}
  \right)
  && \text{covariance update}
\end{aligned}
```

The memory is about $`1/(1-\lambda) = 200`$ samples, roughly two days.
The safeguards are:

| Safeguard | Rule |
|---|---|
| Outlier rejection | skip the sample if $`\lvert e_k \rvert > 8`$ °C/h |
| Covariance limit | skip the division by $`\lambda`$ if $`P_{11} \ge 0.01`$ or $`P_{22} \ge 4`$ |
| Parameter bounds | $`a \in [0.005, 1]`$ h⁻¹, $`b \in [0.05, 10]`$ °C/h |
| Symmetry | $`P_{12}`$ is stored once, so rounding cannot make $`\mathbf{P}`$ asymmetric |
| Initial values | $`\boldsymbol{\theta}_0 = [0.05,\ 1.0]^{\mathsf{T}}`$, $`\mathbf{P}_0 = \operatorname{diag}(0.0025,\ 1)`$ |

Time is in hours because the ESP32 build has only 32-bit floats. In
seconds, $`a \approx 10^{-5}`$ and the update loses too much precision.

### 4. Prediction

With $`T_{out}`$ and $`u`$ held constant, the model has an exact
solution: the temperature approaches an equilibrium exponentially.

```math
T_\infty(u) = T_{out} + \frac{b}{a} \, u,
\qquad
T_{in}(t + h) = T_\infty + \bigl( T_{in}(t) - T_\infty \bigr) \, e^{-a h}
```

With the heater fully on, $`b/a`$ is the most the heater can raise the
room above the outdoor temperature.

### 5. Control law

Every 2 seconds, with filtered indoor temperature $`\hat{T}`$, target
$`T^{*}`$, horizon $`h = 0.25`$ h and hysteresis $`\delta = 0.2`$ °C:

```math
T_{off} = T_{in}(t+h \mid u=0),
\qquad
T_{on} = T_{in}(t+h \mid u=1)
```

```math
u_{next} =
\begin{cases}
1 & \text{if } T_{off} < T^{*} \\
0 & \text{if } T_{on} > T^{*} + \delta \\
u_{now} & \text{otherwise}
\end{cases}
```

The 2-second readings are smoothed with an exponential filter, with a time
constant of about 40 seconds:

```math
\hat{T}_n = \hat{T}_{n-1} + \alpha \, (T_n - \hat{T}_{n-1}),
\qquad
\alpha = 0.05
```

A switch is only allowed if

```math
t - t_{\text{last switch}} \ge 600 \text{ s}
\quad \text{or} \quad
T^{*} \text{ has changed.}
```

This is a simple form of model predictive control: a 15-minute horizon,
two possible inputs ($`u \in \{0, 1\}`$), and hysteresis plus minimum
on/off times to limit switching.

### 6. Testing

The model and controller were tested in a 5-day simulation with a known
apartment, a night setback, mild and Edmonton winter weather, and sensors
reporting in 1 °C and 0.1 °C steps:

- During the day the temperature stayed mostly within about ±0.5 °C of
  the target.
- A 10-minute minimum on/off time roughly halved switching (to about
  36–115 switches per day) without hurting control.
- With 1 °C readings, the ratio $`b/a`$ was learned almost exactly, but
  $`a`$ and $`b`$ themselves came out 20–45 % low. With 0.1 °C readings
  they were within about 5 %.

A background-heat term $`c`$ for central building heating,
$`\dot{T}_{in} = a(T_{out} - T_{in}) + b u + c`$, was also tested, both in
simulation and on a day of real data. It did not improve predictions or
control, so it is not included.

## Limitations

- **Building heat.** In apartments with central heating, much of the heat
  comes from the building and switches on and off unpredictably. The
  thermostat can only add heat on top of that. A background-heat term was
  tested and did not improve predictions, so it is not included.
- **Sensor accuracy.** DHT11-grade sensors are only accurate to about
  ±1–2 °C. Their humidity readings below 20 % are not reliable.
- **Security.** The update server uses plain HTTP and is protected only by
  the token. Use it on a trusted home network only.

## License

MIT. See [LICENSE](LICENSE).
