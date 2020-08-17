# Live Sectional Map controller
# Dylan Rush 2017
# Additional modifications:
#   2018-2020, John Marzulli
# dylanhrush.com
# Uses RPi.GPIO library: https://sourceforge.net/p/raspberry-gpio-python/wiki/BasicUsage/
# Free for personal use. Prohibited for commercial without consent
#
# pip install Adafruit-GPIO
# pip install RPi-GPIO
# pip install pytest
# pip install Adafruit_WS2801
#
# Raspberry Pi
# Run 'raspi-config' and enable the SPI bus under Advanced
#
# Wiring the WS2801 :
# https://learn.adafruit.com/12mm-led-pixels/wiring
# https://tutorials-raspberrypi.com/how-to-control-a-raspberry-pi-ws2801-rgb-led-strip/
# Blue -> 5V Minus AND Pi GND (Physical Pi 25)
# Red -> 5V Plus
# Yellow -> Pin 19(Physical)/SPI MOSI
# Green -> Pin 23(Physical)/SCLK/SPI
#


import json
import logging
import logging.handlers
import re
import sys
import threading
import time
import urllib
from datetime import datetime

import lib.colors as colors_lib
import lib.local_debug as local_debug
import renderer
from configuration import configuration, configuration_server
from data_sources import weather
from lib import safe_logging, colors
from lib.logger import LOGGER, Logger
from lib.recurring_task import RecurringTask
from visualizers import visualizers

thread_lock_object = threading.Lock()


if not local_debug.is_debug():
    import RPi.GPIO as GPIO
    try:
        GPIO.setmode(GPIO.BOARD)
    except:
        # ws281x causes an exception
        # when you try to set the board type
        pass

airport_render_config = configuration.get_airport_configs()
rgb_colors = colors.get_colors()

renderer = renderer.get_renderer(airport_render_config)


def update_weather_for_all_stations():
    """
    Updates the weather for all of the stations.
    This does not update the conditions or category.
    """

    weather.get_metars(airport_render_config.keys(), logger=LOGGER)


def __get_dimmed_color__(
    starting_color: list
) -> list:
    """
    Given a starting color, get the version that is dimmed.

    Arguments:
        starting_color {list} -- The starting color that will be dimmed.

    Returns:
        list -- The color with the dimming adjustment.
    """
    dimmed_color = []
    brightness_adjustment = configuration.get_brightness_proportion()
    for color in starting_color:
        reduced_color = float(color) * brightness_adjustment

        # Some colors are floats, some are integers.
        # Make sure we keep everything the same.
        if isinstance(color, int):
            reduced_color = int(reduced_color)

        dimmed_color.append(reduced_color)

    return dimmed_color


def all_airports(
    color
):
    """
    Sets all of the airports to the given color

    Arguments:
        color {triple} -- Three integer tuple(triple?) of the RGB values
        of the color to set for ALL airports.
    """

    [renderer.set_led(airport_render_config[airport], rgb_colors[color])
        for airport in airport_render_config]

    renderer.show()


def __all_leds_to_color__(
    color: list
):
    renderer.set_all(color)


def get_station_by_led(
    index: int
) -> str:
    """
    Given an LED, find the station it is representing.

    Args:
        index (int): [description]

    Returns:
        str: The identifier of the station.
    """
    for station in airport_render_config:
        if airport_render_config[station] == index:
            return station

    return "UNK"


def render_thread():
    """
    Main logic loop for rendering the lights.
    """

    safe_logging.safe_log(
        LOGGER,
        "Starting rendering thread")

    tic = time.perf_counter()
    toc = time.perf_counter()
    debug_pixels_timer = None

    last_visualizer = 0

    while True:
        try:
            delta_time = toc - tic

            tic = time.perf_counter()

            visualizer_index = configuration.get_visualizer_index(
                visualizers.VISUALIZERS)

            if visualizer_index != last_visualizer:
                renderer.clear()
                last_visualizer = visualizer_index

            visualizers.VISUALIZERS[visualizer_index].update(
                renderer,
                delta_time)

            show_debug_pixels = debug_pixels_timer is None or (
                datetime.utcnow() - debug_pixels_timer).total_seconds() > 60.0

            if show_debug_pixels:
                for index in range(renderer.pixel_count):
                    station = get_station_by_led(index)
                    LOGGER.log_info_message('[{}/{}]={}'.format(
                        station,
                        index,
                        renderer.pixels[index]))

                debug_pixels_timer = datetime.utcnow()

            toc = time.perf_counter()
        except KeyboardInterrupt:
            quit()
        except Exception as ex:
            safe_logging.safe_log(
                LOGGER,
                ex)


def wait_for_all_airports():
    """
    Waits for all of the airports to have been given a chance to initialize.
    If an airport had an error, then that still counts.
    """

    utc_offset = datetime.utcnow() - datetime.now()

    for airport in airport_render_config:
        metar = ""
        try:
            metar = weather.get_metar(airport, logger=LOGGER)
        except Exception as ex:
            safe_logging.safe_log_warning(
                LOGGER,
                "Error while initializing with airport={}, EX={}".format(airport, ex))

    return True


def __get_test_cycle_colors__() -> list:
    base_colors_test = [
        colors.MAGENTA,
        colors.RED,
        colors.BLUE,
        colors.GREEN,
        colors.YELLOW,
        colors.WHITE,
        colors.GRAY,
        colors.DARK_YELLOW
    ]

    colors_to_init = []

    for color in base_colors_test:
        is_global_dimming = configuration.get_brightness_proportion() < 1.0
        color_to_cycle = rgb_colors[color]
        colors_to_init.append(color_to_cycle)
        if is_global_dimming:
            colors_to_init.append(__get_dimmed_color__(color_to_cycle))

    colors_to_init.append(rgb_colors[colors.OFF])

    return colors_to_init


def __test_all_leds__(
    logger: Logger
):
    """
    Test all of the LEDs, independent of the configuration
    to make sure the wiring is correct and that none have failed.

    Arguments:
        logger {Logger} -- The logger being used.
    """
    for color in __get_test_cycle_colors__():
        safe_logging.safe_log(
            logger,
            "Setting to {}".format(color))
        __all_leds_to_color__(color)
        time.sleep(0.5)


if __name__ == '__main__':
    # Start loading the METARs in the background
    # while going through the self-test
    safe_logging.safe_log(
        LOGGER,
        "Initialize weather for all airports")

    weather.get_metars(airport_render_config.keys(), logger=LOGGER)

    __test_all_leds__(LOGGER)

    web_server = configuration_server.WeatherMapServer()

    all_airports(weather.OFF)

    RecurringTask(
        "rest_host",
        0.1,
        web_server.run,
        LOGGER,
        True)

    wait_for_all_airports()

    while True:
        try:
            render_thread()
        except KeyboardInterrupt:
            break

    if not local_debug.is_debug():
        GPIO.cleanup()
