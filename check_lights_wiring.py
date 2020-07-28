# Self-Test file that makes sure all
# off the wired lights are functional.

import logging
import time

import lib.local_debug as local_debug
from configuration import configuration
from data_sources import weather
from lib import safe_logging
from lib.logger import Logger
import renderer

python_logger = logging.getLogger("check_lights_wiring")
python_logger.setLevel(logging.DEBUG)
LOGGER = Logger(python_logger)

if not local_debug.IS_PI:
    safe_logging.safe_log_warning(
        LOGGER,
        "This is only able to run on a Raspberry Pi.")

    exit(0)

airport_render_config = configuration.get_airport_configs()
colors = configuration.get_colors()

renderer = renderer.get_renderer(airport_render_config)

if __name__ == '__main__':
    # Start loading the METARs in the background
    # while going through the self-test
    safe_logging.safe_log(LOGGER, "Testing all colors for all airports.")

    # Test LEDS on startup
    colors_to_test = (
        weather.LOW,
        weather.RED,
        weather.YELLOW,
        weather.GREEN,
        weather.BLUE,
        weather.WHITE,
        weather.GRAY,
        weather.DARK_YELLOW,
        weather.OFF
    )

    for color in colors_to_test:
        safe_logging.safe_log(LOGGER, "Setting to {}".format(color))

        [renderer.set_led(airport_render_config[airport], colors[color])
            for airport in airport_render_config]

        renderer.show()

        time.sleep(0.5)

    safe_logging.safe_log(LOGGER, "Starting airport identification test")

    while True:
        for airport in airport_render_config:
            led_index = airport_render_config[airport]
            renderer.set_led(
                led_index,
                colors[weather.GREEN])

            renderer.show()

            safe_logging.safe_log(
                LOGGER,
                "LED {} - {} - Now lit".format(led_index, airport))

            input("Press Enter to continue...")

            renderer.set_led(
                airport_render_config[airport],
                colors[weather.OFF])

            renderer.show()
