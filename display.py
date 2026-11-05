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
        # The upstream driver's default uses reversed COM/SEG mapping;
        # zero reverses both again, rotating the whole panel 180 degrees.
        self.oled.rotate(0)
        self.oled.contrast(64)
        self.glyphs = framebuf.FrameBuffer(
            bytearray(64), 64, 8, framebuf.MONO_HLSB
        )
        self.first_frame = True
        self.log("OLED ready: 128x64 at 0x{:02x}, SDA {}, SCL {}".format(
            address, self.sda, self.scl
        ))

    def update(self, now, temperature, target=None, humidity=None,
               outdoor=None, heating=None, wifi=None):
        if self.oled is None and now < self.retry_at:
            return False

        try:
            if self.oled is None:
                self._connect()

            oled = self.oled
            oled.fill(0)
            # Leave margins and keep every glyph inside one color band.
            # After rotation the blue area is above the bottom 16 rows.
            oled.text("ROOM", 4, 2, 1)
            heat_text = "--" if heating is None else "ON" if heating else "OFF"
            oled.text("H:" + heat_text, 84, 2, 1)
            if temperature is None:
                oled.text("Sensor error", 4, 18, 1)
            else:
                # Scale the built-in font 2x so the reading is legible
                # on a 0.96-inch panel without a separate font file.
                value = "{:.1f}".format(temperature)
                self.glyphs.fill(0)
                self.glyphs.text(value, 0, 0, 1)
                for y in range(8):
                    for x in range(min(64, len(value) * 8)):
                        if self.glyphs.pixel(x, y):
                            oled.fill_rect(4 + x * 2, 14 + y * 2, 2, 2, 1)
                oled.text("C", 112, 18, 1)

            target_text = "--" if target is None else "{:.1f}".format(target)
            humidity_text = "--" if humidity is None else "{:.0f}".format(humidity)
            outdoor_text = "--" if outdoor is None else "{:.1f}".format(outdoor)
            oled.text("Target:{} C".format(target_text), 4, 36, 1)

            # Center one line in the 16-pixel green/yellow band. Alternate
            # secondary readings rather than squeezing two rows against
            # the panel edges or across the band boundary.
            if int(now // 4) % 2 == 0:
                footer = "RH{}% O{}C".format(humidity_text, outdoor_text)
            else:
                footer = (
                    "WiFi --" if wifi is None else
                    "WiFi connected" if wifi else "WiFi offline"
                )
            oled.text(footer, 4, 52, 1)
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
