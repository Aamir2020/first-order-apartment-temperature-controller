"""Exercise real control functions without importing ESP32-only modules."""
import ast
from pathlib import Path
import unittest


class StopLoop(BaseException):
    pass


class Clock:
    PERIOD = 1 << 30

    def __init__(self):
        self.ms = 0
        self.fail_sleep = False

    def ticks_ms(self):
        return self.ms % self.PERIOD

    def ticks_diff(self, end, start):
        half = self.PERIOD // 2
        return (end - start + half) % self.PERIOD - half

    def sleep(self, seconds):
        if self.fail_sleep:
            self.fail_sleep = False
            raise OSError("interrupted move")
        self.ms += int(seconds * 1000)


class Servo:
    def __init__(self):
        self.calls = []
        self.fail_pulse = None

    def duty_ns(self, pulse):
        self.calls.append(pulse)
        if pulse == self.fail_pulse:
            self.fail_pulse = None
            raise OSError("PWM failure")


def control_namespace():
    source = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(source.read_text())
    names = {
        "SERVO_MIN_US", "SERVO_MAX_US", "SERVO_OFF_PERCENT",
        "SERVO_ON_PERCENT", "SERVO_SETTLE_SECONDS",
        "SERVO_OFF_REFRESH_SECONDS", "POLL_INTERVAL_SECONDS",
        "OUTDOOR_MAX_AGE_SECONDS", "OUTDOOR_REFRESH_SECONDS",
        "HEATER_POWER_WATTS", "_servo_percent", "_servo_last_command",
    }
    nodes = [node for node in tree.body if (
        isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id in names for t in node.targets)
    ) or (
        isinstance(node, ast.FunctionDef)
        and node.name in {"set_servo_position", "main"}
    )]
    namespace = {"time": Clock(), "servo": Servo(), "log": lambda *a: None}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    return namespace


class ServoControlTests(unittest.TestCase):
    def setUp(self):
        self.ns = control_namespace()
        self.move = self.ns["set_servo_position"]
        self.servo = self.ns["servo"]
        self.clock = self.ns["time"]

    def test_off_is_reasserted_after_missed_physical_move(self):
        # PWM can succeed while the mechanism fails to move. No sensor
        # can detect that here, so another burst must eventually be sent.
        self.move(100)
        self.move(0)
        self.servo.calls.clear()
        self.clock.ms += 59000
        self.assertFalse(self.move(0))
        self.assertEqual(self.servo.calls, [])
        self.clock.ms += 1000
        self.assertTrue(self.move(0))
        self.assertEqual(self.servo.calls, [500000, 0])

    def test_refresh_survives_ticks_wrap(self):
        self.clock.ms = Clock.PERIOD - 30000
        self.move(0)
        self.servo.calls.clear()
        self.clock.ms += 60000
        self.assertTrue(self.move(0))
        self.assertEqual(self.servo.calls, [500000, 0])

    def test_failed_on_move_does_not_suppress_return_to_off(self):
        self.move(0)
        self.servo.fail_pulse = 2500000
        with self.assertRaises(OSError):
            self.move(100)
        self.assertIsNone(self.ns["_servo_percent"])
        self.servo.calls.clear()
        self.move(0)
        self.assertEqual(self.servo.calls, [500000, 0])

    def test_interrupted_wait_stops_pwm_and_allows_retry(self):
        self.clock.fail_sleep = True
        with self.assertRaises(OSError):
            self.move(0)
        self.assertEqual(self.servo.calls, [500000, 0])
        self.assertIsNone(self.ns["_servo_percent"])
        self.assertTrue(self.move(0))

    def test_pwm_stop_failure_does_not_cache_success(self):
        self.servo.fail_pulse = 0
        with self.assertRaises(OSError):
            self.move(0)
        self.assertIsNone(self.ns["_servo_percent"])
        self.assertTrue(self.move(0))

    def test_unchanged_on_does_not_pulse_every_loop(self):
        self.move(100)
        self.servo.calls.clear()
        self.clock.ms += 120000
        self.assertFalse(self.move(100))
        self.assertEqual(self.servo.calls, [])

    def test_main_reports_failed_move_as_unknown_and_retries(self):
        events = []
        ns = self.ns

        class WLAN:
            def isconnected(self):
                return True

        class Network:
            class WLAN:
                IF_STA = 0

                def __new__(cls, interface):
                    return WLAN()

        class Sensor:
            buf = []

            def measure(self):
                pass

            def temperature(self):
                return 25

            def humidity(self):
                return 40

        class Model:
            samples = 0

        class Controller:
            tin_filtered = 25

            def __init__(self, model, log):
                pass

            def step(self, *args):
                return False

        attempts = 0

        def move(percent):
            nonlocal attempts
            events.append(("move", percent))
            attempts += 1
            if attempts == 2:  # Startup succeeds; first loop fails.
                raise OSError("missed command")

        loops = 0

        def sleep(seconds):
            nonlocal loops
            loops += 1
            if loops == 2:
                raise StopLoop()

        ns.update({
            "network": Network, "sensor": Sensor(), "load_model": Model,
            "HeatingController": Controller, "set_servo_position": move,
            "get_target_temperature": lambda: 20,
            "report_measurements": lambda t, h, heating: events.append(("report", heating)),
            "get_outdoor_temperature": lambda: events.append(("weather", None)) or -2,
        })
        self.clock.sleep = sleep
        with self.assertRaises(StopLoop):
            ns["main"]()
        self.assertEqual(events, [
            ("move", 0), ("move", 0), ("report", None),
            ("weather", None), ("move", 0), ("report", False),
        ])


if __name__ == "__main__":
    unittest.main()
