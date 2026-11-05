"""Optional 128x64 I2C OLED; failures never escape into heater control."""


class TemperatureDisplay:
    RETRY_SECONDS = 60

    def __init__(self, sda=25, scl=27, log=print):
        self.sda = sda
        self.scl = scl
        self.log = log
        self.oled = None
        self.glyphs = None
        self.retry_at = 0
        self.first_frame = False

    def _connect(self):
        from machine import Pin, SoftI2C
        from ssd1306 import SSD1306_I2C
        import framebuf

        bus = SoftI2C(
            sda=Pin(self.sda), scl=Pin(self.scl),
            freq=100000, timeout=20000
        )
        addresses = bus.scan()
        address = next((a for a in (0x3C, 0x3D) if a in addresses), None)
        if address is None:
            raise OSError("OLED not found; I2C addresses: {}".format(addresses))

        self.oled = SSD1306_I2C(128, 64, bus, addr=address)
        self.oled.contrast(64)
        self.glyphs = framebuf.FrameBuffer(
            bytearray(64), 64, 8, framebuf.MONO_HLSB
        )
        self.first_frame = True
        self.log("OLED ready: 128x64 at 0x{:02x}, SDA {}, SCL {}".format(
            address, self.sda, self.scl
        ))

    def update(self, now, temperature):
        if self.oled is None and now < self.retry_at:
            return False

        try:
            if self.oled is None:
                self._connect()

            oled = self.oled
            oled.fill(0)
            oled.text("TEMPERATURE", 0, 0, 1)
            if temperature is None:
                oled.text("Sensor error", 0, 28, 1)
            else:
                # Scale the built-in font 2x so the reading is legible
                # on a 0.96-inch panel without a separate font file.
                value = "{:.1f}".format(temperature)
                self.glyphs.fill(0)
                self.glyphs.text(value, 0, 0, 1)
                for y in range(8):
                    for x in range(min(64, len(value) * 8)):
                        if self.glyphs.pixel(x, y):
                            oled.fill_rect(x * 2, 26 + y * 2, 2, 2, 1)
                oled.text("C", 116, 30, 1)
            oled.show()

            if self.first_frame and temperature is not None:
                self.log("OLED first temperature displayed: {:.1f} C".format(
                    temperature
                ))
                self.first_frame = False
            return True
        except Exception as exc:
            self.oled = None
            self.glyphs = None
            self.retry_at = now + self.RETRY_SECONDS
            self.log("OLED unavailable; retry in 60 seconds:", exc)
            return False
