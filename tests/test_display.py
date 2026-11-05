import sys
import types
import unittest
from unittest.mock import patch

from display import TemperatureDisplay


class FakeScreen:
    def __init__(self, *args, **kwargs):
        self.address = kwargs["addr"]
        self.texts = []
        self.frames = 0
        self.fail = False
        self.rects = []

    def contrast(self, value):
        pass

    def fill(self, value):
        self.texts.clear()
        self.rects.clear()

    def text(self, value, *args):
        self.texts.append(value)

    def fill_rect(self, *args):
        self.rects.append(args)

    def show(self):
        if self.fail:
            raise OSError("display unplugged")
        self.frames += 1


class FakeGlyphs:
    def __init__(self, *args):
        self.value = None

    def fill(self, value):
        self.value = None

    def text(self, value, *args):
        self.value = value

    def pixel(self, x, y):
        return x == 0 and y == 0


class DisplayTests(unittest.TestCase):
    def setUp(self):
        self.addresses = [0x3C]
        self.scans = 0
        self.bus_config = None

        def bus(**kwargs):
            self.bus_config = kwargs
            return types.SimpleNamespace(scan=scan)

        def scan():
            self.scans += 1
            return self.addresses

        self.logs = []
        self.display = TemperatureDisplay(log=lambda *args: self.logs.append(args))
        self.modules = patch.dict(sys.modules, {
            "machine": types.SimpleNamespace(Pin=lambda p: p, SoftI2C=bus),
            "ssd1306": types.SimpleNamespace(SSD1306_I2C=FakeScreen),
            "framebuf": types.SimpleNamespace(FrameBuffer=FakeGlyphs, MONO_HLSB=3),
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_fresh_temperature_and_bus_configuration(self):
        self.assertTrue(self.display.update(0, 24.7))
        self.assertEqual(self.display.glyphs.value, "24.7")
        self.assertEqual(self.bus_config, {
            "sda": 25, "scl": 27, "freq": 100000, "timeout": 20000,
        })
        self.assertTrue(self.display.oled.rects)
        self.assertTrue(self.display.update(2, 25.1))
        self.assertEqual(self.display.glyphs.value, "25.1")
        self.assertEqual(self.display.oled.frames, 2)
        self.assertEqual(len(self.logs), 2)  # No per-frame flash logging.

    def test_alternate_i2c_address(self):
        self.addresses = [0x3D]
        self.assertTrue(self.display.update(0, 24.7))
        self.assertEqual(self.display.oled.address, 0x3D)

    def test_missing_display_backs_off_and_recovers(self):
        self.addresses = []
        self.assertFalse(self.display.update(0, 24.7))
        self.assertFalse(self.display.update(59, 24.7))
        self.assertEqual(self.scans, 1)
        self.addresses = [0x3C]
        self.assertTrue(self.display.update(60, 24.7))
        self.assertEqual(self.scans, 2)

    def test_sensor_failure_replaces_old_reading(self):
        self.display.update(0, 24.7)
        self.assertTrue(self.display.update(2, None))
        self.assertIn("Sensor error", self.display.oled.texts)
        self.assertEqual(self.display.oled.rects, [])

    def test_bus_error_is_contained_and_retry_can_reconnect(self):
        self.display.update(0, 24.7)
        self.display.oled.fail = True
        self.assertFalse(self.display.update(2, 24.8))
        self.assertIsNone(self.display.oled)
        self.assertFalse(self.display.update(61, 24.9))
        self.assertTrue(self.display.update(62, 24.9))
        self.assertEqual(self.display.glyphs.value, "24.9")


if __name__ == "__main__":
    unittest.main()
