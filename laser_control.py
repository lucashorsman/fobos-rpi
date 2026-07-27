"""
laser_control.py

Direct GPIO control of the laser diode via the AQY212 chip.
Uses gpiozero (PWMLED) so we get both a simple on/off API and intensity
control for free, without hand-rolling PWM.
Wire the transistor base (through its base resistor) to GPIO_PIN.
Default here is GPIO 17.
"""

import logging
import platform
import sys

logger = logging.getLogger("laser_control")

GPIO_AVAILABLE = False
PWMLED = None

if sys.platform.startswith("linux") and platform.machine() in {"armv7l", "aarch64"}:
    try:
        from gpiozero import PWMLED

        GPIO_AVAILABLE = True
    except ImportError:
        # Lets this module import cleanly on a dev machine that isn't a Pi,
        # e.g. if you want to unit test camera_stream_server.py separately.
        logger.warning("gpiozero not available -- laser control running in stub mode")
else:
    logger.info("Non-RPi host detected -- laser control running in stub mode")


class LaserController:
    """Thin wrapper so main.py doesn't care whether GPIO is real or stubbed."""

    def __init__(self, gpio_pin: int = 17):
        self.gpio_pin = gpio_pin
        self._intensity = 0.0
        self._led = None
        if GPIO_AVAILABLE:
            try:
                self._led = PWMLED(gpio_pin)
            except Exception:
                logger.warning(
                    "gpiozero could not initialize pin %d -- laser control running in stub mode",
                    gpio_pin,
                )
        if self._led is None:
            logger.info("LaserController stub initialized (pin=%d, no hardware)", gpio_pin)

    def on(self, intensity: float = 1.0) -> None:
        """Turn laser on at given intensity (0.0-1.0). Default full power."""
        self.set_intensity(intensity)

    def off(self) -> None:
        self._intensity = 0.0
        if self._led:
            self._led.off()
        logger.debug("Laser off")

    def set_intensity(self, value: float) -> None:
        value = max(0.0, min(1.0, value))
        self._intensity = value
        if self._led:
            self._led.value = value
        logger.debug("Laser intensity set to %.2f", value)

    @property
    def intensity(self) -> float:
        return self._intensity

    @property
    def is_on(self) -> bool:
        return self._intensity > 0.0

    def close(self) -> None:
        if self._led:
            self._led.close()

    def status(self) -> dict:
        return {"on": self.is_on, "intensity": self._intensity, "gpio_pin": self.gpio_pin}
