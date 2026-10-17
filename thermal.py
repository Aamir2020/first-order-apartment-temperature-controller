import math

# ============================================================
# Apartment thermal model
#
#     dT/dt = a * (Tout - Tin) + b * u
#
#     a = 1 / (R*C)     [1/hour]
#     b = P / C         [C/hour]
#     u = fraction of the interval the heater was on (0..1)
#
# Time is in hours, not seconds: the ESP32 build uses 32-bit
# floats, and per-second values (a ~ 1e-5) lose too much precision
# inside the least-squares update.
# ============================================================


class ThermalModel:

    # Physically plausible bounds; estimates are clamped to these
    # so one bad sample cannot produce a negative or absurd model.
    A_MIN, A_MAX = 0.005, 1.0     # RC between 1 h and 200 h
    B_MIN, B_MAX = 0.05, 10.0     # heater adds 0.05 to 10 C/hour

    # Covariance limits; forgetting is paused above these so the
    # covariance cannot blow up while the heater state is constant.
    P11_MAX = 0.01
    P22_MAX = 4.0

    # Skip samples whose prediction error is larger than this
    # (window opened, sensor glitch). DHT11 1 C steps over a
    # 15-minute sample already give 4 C/hour.
    OUTLIER_C_PER_HOUR = 8.0

    def __init__(self):
        # Initial guesses: RC = 20 h, heater adds 1 C/hour.
        self.a = 0.05
        self.b = 1.0

        # Covariance for Recursive Least Squares, sized to the
        # uncertainty of the initial guesses.
        self.P11 = 0.0025
        self.P12 = 0.0
        self.P22 = 1.0

        # Forgetting factor: 0.995 per 15-minute sample remembers
        # roughly the last two days.
        self.lam = 0.995

        self.samples = 0

    def update(self, tin_previous, tin, tout, heater, dt_hours):
        """
        Add one measurement interval to the model.

        tin_previous, tin = indoor temperature at the start and end
        heater = fraction of the interval the heater was on (0..1)

        Returns the prediction error in C/hour, or None if skipped.
        """
        y = (tin - tin_previous) / dt_hours

        # Regressors describe the interval itself: the midpoint
        # temperature and the heater duty during it, not the state
        # at the end of the interval.
        x1 = tout - (tin + tin_previous) / 2
        x2 = heater

        error = y - (self.a * x1 + self.b * x2)
        if abs(error) > self.OUTLIER_C_PER_HOUR:
            return None

        px1 = self.P11 * x1 + self.P12 * x2
        px2 = self.P12 * x1 + self.P22 * x2

        denominator = self.lam + x1 * px1 + x2 * px2
        if denominator <= 0:
            return None

        k1 = px1 / denominator
        k2 = px2 / denominator

        self.a = min(self.A_MAX, max(self.A_MIN, self.a + k1 * error))
        self.b = min(self.B_MAX, max(self.B_MIN, self.b + k2 * error))

        # Symmetric covariance update; P12 is stored once so
        # rounding cannot make the matrix asymmetric.
        p11 = self.P11 - k1 * px1
        p12 = self.P12 - k1 * px2
        p22 = self.P22 - k2 * px2

        if p11 < self.P11_MAX and p22 < self.P22_MAX:
            p11 /= self.lam
            p12 /= self.lam
            p22 /= self.lam

        self.P11, self.P12, self.P22 = p11, p12, p22
        self.samples += 1
        return error

    def predict(self, tin, tout, heater, hours):
        """
        Predict indoor temperature after 'hours' with constant
        outdoor temperature and heater state.
        """
        equilibrium = tout + (self.b / self.a) * heater

        return equilibrium + (tin - equilibrium) * math.exp(
            -self.a * hours
        )

    def get_parameters(self, heater_power):
        """
        Convert a, b into physical R [C/W], C [J/C] and RC [hours].
        """
        if heater_power <= 0:
            return None

        C = heater_power * 3600 / self.b

        return {
            "a": self.a,
            "b": self.b,
            "R": self.b / (self.a * heater_power),
            "C": C,
            "RC_hours": 1.0 / self.a,
        }

    def to_dict(self):
        return {"a": self.a, "b": self.b, "P11": self.P11,
                "P12": self.P12, "P22": self.P22,
                "samples": self.samples}

    def load_dict(self, d):
        self.a = d["a"]
        self.b = d["b"]
        self.P11 = d["P11"]
        self.P12 = d["P12"]
        self.P22 = d["P22"]
        self.samples = d["samples"]


class HeatingController:
    """
    Predictive on/off control:

    - heater ON if the model predicts that, with the heater off,
      the room falls below target within PREDICTION_HOURS
    - heater OFF if, with the heater on, it would overshoot
      target + HYSTERESIS
    - otherwise keep the current state

    Decisions are made on every call (every 2 seconds). Minimum
    on/off times stop the servo from flapping when the reading sits
    on a DHT11 1 C step, but a change of target bypasses them.

    It also collects measurements and feeds the model one sample
    every SAMPLE_SECONDS.
    """

    PREDICTION_HOURS = 0.25
    HYSTERESIS = 0.2
    MIN_ON_SECONDS = 600
    MIN_OFF_SECONDS = 600
    SAMPLE_SECONDS = 15 * 60

    # Smoothing of the 2-second DHT readings used for decisions.
    FILTER_ALPHA = 0.05

    def __init__(self, model, log=print):
        self.model = model
        self.log = log

        self.heater = False
        self.last_switch = None
        self.last_step = None
        self.last_target = None
        self.tin_filtered = None

        # Sample accumulators. Heater on-time is split into the
        # first and second half of each sample so the duty between
        # sample centres (where the averages sit) is exact.
        self.sample_start = None
        self.tin_sum = 0.0
        self.tin_count = 0
        self.on_first_half = 0.0
        self.on_second_half = 0.0

        # Previous completed sample: (average tin, second-half on).
        self.previous = None

    def _accumulate_heater(self, now):
        if self.last_step is None or not self.heater:
            return

        # Split the elapsed on-time across the half-sample boundary.
        half = self.sample_start + self.SAMPLE_SECONDS / 2
        start, end = self.last_step, now
        if end <= half:
            self.on_first_half += end - start
        elif start >= half:
            self.on_second_half += end - start
        else:
            self.on_first_half += half - start
            self.on_second_half += end - half

    def _finish_sample(self, tout):
        if self.tin_count == 0:
            # No readings this sample; the chain is broken.
            self.previous = None
            return

        tin_avg = self.tin_sum / self.tin_count

        if self.previous is not None and tout is not None:
            prev_avg, prev_second_half = self.previous
            duty = (prev_second_half + self.on_first_half) / self.SAMPLE_SECONDS
            error = self.model.update(
                prev_avg, tin_avg, tout, duty,
                self.SAMPLE_SECONDS / 3600
            )
            self.log(
                "Model sample: Tin {:.2f} -> {:.2f}, Tout {:.1f}, "
                "duty {:.2f}, error {}, a {:.4f}/h, b {:.3f} C/h, "
                "n {}".format(
                    prev_avg, tin_avg, tout, duty,
                    "skipped" if error is None else "{:.2f}".format(error),
                    self.model.a, self.model.b, self.model.samples
                )
            )

        self.previous = (tin_avg, self.on_second_half)

    def step(self, now, tin, tout, target):
        """
        Call regularly (every few seconds).

        now    = monotonic time in seconds
        tin    = indoor reading, or None if the sensor failed
        tout   = outdoor temperature, or None if unknown
        target = target temperature

        Returns True if the heater should be on.
        """
        if self.sample_start is None:
            self.sample_start = now

        self._accumulate_heater(now)
        self.last_step = now

        if now - self.sample_start >= self.SAMPLE_SECONDS:
            self._finish_sample(tout)
            self.sample_start = now
            self.tin_sum = 0.0
            self.tin_count = 0
            self.on_first_half = 0.0
            self.on_second_half = 0.0

        if tin is not None:
            self.tin_sum += tin
            self.tin_count += 1
            if self.tin_filtered is None:
                self.tin_filtered = tin
            else:
                self.tin_filtered += self.FILTER_ALPHA * (
                    tin - self.tin_filtered
                )

        want = self._choose(self.tin_filtered, tout, target)

        # A new target from the user is acted on straight away; the
        # minimum on/off time only stops the controller flapping on
        # its own.
        target_changed = (
            self.last_target is not None and target != self.last_target
        )
        self.last_target = target

        if want != self.heater:
            held = (
                None if self.last_switch is None
                else now - self.last_switch
            )
            minimum = (
                self.MIN_ON_SECONDS if self.heater
                else self.MIN_OFF_SECONDS
            )
            if target_changed or held is None or held >= minimum:
                self.heater = want
                self.last_switch = now

        return self.heater

    def _choose(self, tin, tout, target):
        if tin is None or tout is None:
            # Without both temperatures there is no prediction;
            # fall back to simple on/off around the target.
            if tin is None:
                return False
            if tin < target:
                return True
            if tin > target + self.HYSTERESIS:
                return False
            return self.heater

        off_temp = self.model.predict(tin, tout, 0, self.PREDICTION_HOURS)
        on_temp = self.model.predict(tin, tout, 1, self.PREDICTION_HOURS)

        if off_temp < target:
            return True

        if on_temp > target + self.HYSTERESIS:
            return False

        return self.heater
