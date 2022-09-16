"""
Handles fetching and decoding weather from www.checkwxapi.com  (needs API key)
"""

import csv
import os
import re
import sys
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from pprint import pprint

import requests
import json
from configuration import configuration
from lib.colors import clamp
from lib.safe_logging import safe_log, safe_log_warning

api_service_base = configuration.get_api_service_base()
api_key = configuration.get_api_key()
hdr = {"X-API-Key": api_key}

INVALID = 'INVALID'
INOP = 'INOP'
VFR = 'VFR'
MVFR = 'M' + VFR
IFR = 'IFR'
LIFR = 'L' + IFR
NIGHT = 'NIGHT'
NIGHT_DARK = 'DARK'
SMOKE = 'SMOKE'

LOW = 'LOW'
OFF = 'OFF'

DRIZZLE = 'DRIZZLE'
RAIN = 'RAIN'
HEAVY_RAIN = 'HEAVY {}'.format(RAIN)
SNOW = 'SNOW'
ICE = 'ICE'
UNKNOWN = 'UNKNOWN'

__cache_lock__ = threading.Lock()
__rest_session__ = requests.Session()
__daylight_cache__ = {}
__metar_report_cache__ = {}
__station_last_called__ = {}

DEFAULT_READ_SECONDS = 15
DEFAULT_METAR_LIFESPAN_MINUTES = 60
DEFAULT_METAR_INVALIDATE_MINUTES = DEFAULT_METAR_LIFESPAN_MINUTES * 1.5


def __get_utc_datetime__(
    datetime_string: str
) -> datetime:
    """
    Parses the RFC format datetime into something we can use.

    Arguments:
        datetime_string {string} -- The RFC encoded datetime string.

    Returns:
        datetime -- The parsed date time.
    """
    if len(datetime_string) == 19:
        return datetime.strptime(datetime_string, "%Y-%m-%dT%H:%M:%S")
    elif len(datetime_string) == 8:     # need to calc the date portion
        current_dt = datetime.utcnow()
        in_time = datetime.strptime(datetime_string, "%H:%M:%S")
        new_dt = current_dt + timedelta(hours=in_time.hour)
        new_dt += timedelta(seconds=in_time.minute)
        new_dt += timedelta(seconds=in_time.second)
        return new_dt
    else:
        return None



# fetch from api: lat/lon/sunset/sunrise/etc
def __fetch_airport_data__(
    airports: list
):
    """
    Loads all of the station data
    then places it into a dictionary for easy use. (lat/lon/sunrise/sunset/twilight)

    Keyword Arguments:
        airports {list} -- a list of configured airport icao codes

    Returns:
        dictionary -- A map of the airport data keyed by ICAO code.
    """
    si = {}     # station info

    # checkwx allows up to 20 requests on a single call
    for i in range(0, len(airports), 20):
        partial_list = ",".join(airports[i:i+20])
        req = requests.get("{}/station/{}/suntimes".format(api_service_base, partial_list), headers=hdr)
        try:
            req.raise_for_status()
            resp = json.loads(req.text)
        except requests.exceptions.HTTPError as e:
            print(e)

        for a in resp['data']:
            # current = __get_utc_datetime__(a['sunrise_sunset']['utc']['current']) # 2022-09-14T00:10:21
            dawn = __get_utc_datetime__(a['sunrise_sunset']['utc']['dawn'])         # 11:42:00
            sunrise = __get_utc_datetime__(a['sunrise_sunset']['utc']['sunrise'])   # 12:06:29
            sunset = __get_utc_datetime__(a['sunrise_sunset']['utc']['sunset'])     # 00:28:29
            dusk = __get_utc_datetime__(a['sunrise_sunset']['utc']['dusk'])         # 00:52:54
            sunrise_length = sunrise - dawn
            sunset_length = dusk - sunset
            avg_transition_time = timedelta(seconds=(sunrise_length.seconds + sunset_length.seconds) / 2)

            si[a['icao']] = {
                "long": a['geometry']['coordinates'][0],
                "lat": a['geometry']['coordinates'][1],
                "dawn": dawn,
                "sunrise": sunrise,
                "sunset": sunset,
                "dusk": dusk,
                "full_light_start": sunrise + avg_transition_time,
                "full_light_end": sunset - avg_transition_time
            }
    return si


airports_config = configuration.get_airport_configs()
__station_info__ = __fetch_airport_data__(list(airports_config.keys()))

# print("number of airports in config: {}".format(len(airports_config)))
# print("number of airports with locations: {}".format(len(__airport_locations__)))



def __set_cache__(
    station_icao_code: str,
    cache: dict,
    value
):
    """
    Sets the given cache to have the given value.
    Automatically sets the cache saved time.

    Arguments:
        airport_icao_code {str} -- The code of the station to cache the results for.
        cache {dictionary} -- The cache keyed by airport code.
        value {object} -- The value to store in the cache.
    """

    __cache_lock__.acquire()
    try:
        cache[station_icao_code] = (datetime.utcnow(), value)
    finally:
        __cache_lock__.release()


def __is_cache_valid__(
    station_icao_code: str,
    cache: dict,
    cache_life_in_minutes: int = 8
) -> bool:
    """
    Returns TRUE and the cached value if the cached value
    can still be used.

    Arguments:
        airport_icao_code {str} -- The airport code to get from the cache.
        cache {dictionary} -- Tuple of last update time and value keyed by airport code.
        cache_life_in_minutes {int} -- How many minutes until the cached value expires

    Returns:
        [type] -- [description]
    """

    __cache_lock__.acquire()

    if cache is None:
        return (False, None)

    now = datetime.utcnow()

    try:
        if station_icao_code in cache:
            time_since_last_fetch = now - cache[station_icao_code][0]

            if time_since_last_fetch is not None and (((time_since_last_fetch.total_seconds()) / 60.0) < cache_life_in_minutes):
                return (True, cache[station_icao_code][1])
            else:
                return (False, cache[station_icao_code][1])
    except Exception:
        pass
    finally:
        __cache_lock__.release()

    return (False, None)


def is_daylight(
    station_icao_code: str,
    current_utc_time: datetime = datetime.utcnow().replace(tzinfo=timezone.utc),
    use_cache: bool = True
) -> bool:
    """
    Returns TRUE if the airport is currently in daylight

    Arguments:
        airport_icao_code {string} -- The airport code to test.

    Returns:
        boolean -- True if the airport is currently in daylight.
    """

    si = __station_info__[station_icao_code]
    # Deal with day old data...
    hours_since_sunrise = (
        current_utc_time - si['sunrise']).total_seconds() / 3600

    if hours_since_sunrise < 0:
        x=''
        # how to refetch for this airport
        # light_times = get_civil_twilight(
        #     station_icao_code,
        #     current_utc_time - timedelta(hours=24),
        #     use_cache)

    if hours_since_sunrise > 24:
        return True

    # Make sure the time between takes into account
    # The amount of time sunrise or sunset takes
    is_after_sunrise = si['full_light_start'] < current_utc_time
    is_before_sunset = current_utc_time < si['full_light_end']

    return is_after_sunrise and is_before_sunset

    return True


def is_night(
    station_icao_code: str,
    light_times: list,
    current_utc_time: datetime = datetime.utcnow().replace(tzinfo=timezone.utc),
    use_cache: bool = True
) -> bool:
    """
    Returns TRUE if the airport is currently in night

    Arguments:
        airport_icao_code {string} -- The airport code to test.

    Returns:
        boolean -- True if the airport is currently in night.
    """

    if light_times is not None:
        # Deal with day old data...
        hours_since_sunrise = (
            current_utc_time - light_times[1]).total_seconds() / 3600

        if hours_since_sunrise < 0:
            light_times = get_civil_twilight(
                station_icao_code,
                current_utc_time - timedelta(hours=24),
                use_cache)

        if hours_since_sunrise > 24:
            return False

        # Make sure the time between takes into account
        # The amount of time sunrise or sunset takes
        is_before_sunrise = current_utc_time < light_times[0]
        is_after_sunset = current_utc_time > light_times[5]

        return is_before_sunrise or is_after_sunset

    return False


def get_proportion_between_times(
    start: datetime,
    current: datetime,
    end: datetime
) -> float:
    """
    Gets the "distance" (0.0 to 1.0) between the start and the end where the current time is.
    IE:
        If the CurrentTime is the same as StartTime, then the result will be 0.0
        If the CurrentTime is the same as the EndTime, then the result will be 1.0
        If the CurrentTime is halfway between StartTime and EndTime, then the result will be 0.5


    Arguments:
        start {datetime} -- The starting time.
        current {datetime} -- The time we want to get the proportion for.
        end {datetime} -- The end time to calculate the interpolaton for.

    Returns:
        float -- The amount of interpolaton for Current between Start and End
    """

    if current < start:
        return 0.0

    if current > end:
        return 1.0

    total_delta = (end - start).total_seconds()
    time_in = (current - start).total_seconds()

    return time_in / total_delta


def get_twilight_transition(
    airport_icao_code,
    current_utc_time=None,
    use_cache=True
):
    """
    Returns the mix of dark & color fade for twilight transitions.

    Arguments:
        airport_icao_code {string} -- The ICAO code of the weather station.

    Keyword Arguments:
        current_utc_time {datetime} -- The time in UTC to calculate the mix for. (default: {None})
        use_cache {bool} -- Should the cache be used to determine the sunrise/sunset/transition data. (default: {True})

    Returns:
        tuple -- (proportion_off_to_night, proportion_night_to_category)
    """

    if current_utc_time is None:
        current_utc_time = datetime.utcnow()

    light_times = get_civil_twilight(
        airport_icao_code,
        current_utc_time, use_cache)

    if light_times is None or len(light_times) < 5:
        return 0.0, 1.0

    if is_daylight(airport_icao_code, light_times, current_utc_time, use_cache):
        return 0.0, 1.0

    if is_night(airport_icao_code, light_times, current_utc_time, use_cache):
        return 0.0, 0.0

    proportion_off_to_night = 0.0
    proportion_night_to_color = 0.0

    # Sunsetting: Night to off
    if current_utc_time >= light_times[4]:
        proportion_off_to_night = 1.0 - \
            get_proportion_between_times(
                light_times[4],
                current_utc_time, light_times[5])
    # Sunsetting: Color to night
    elif current_utc_time >= light_times[3]:
        proportion_night_to_color = 1.0 - \
            get_proportion_between_times(
                light_times[3],
                current_utc_time, light_times[4])
    # Sunrising: Night to color
    elif current_utc_time >= light_times[1]:
        proportion_night_to_color = get_proportion_between_times(
            light_times[1],
            current_utc_time, light_times[2])
    # Sunrising: off to night
    else:
        proportion_off_to_night = get_proportion_between_times(
            light_times[0],
            current_utc_time, light_times[1])

    proportion_off_to_night = clamp(-1.0, proportion_off_to_night, 1.0)
    proportion_night_to_color = clamp(-1.0, proportion_night_to_color, 1.0)

    return proportion_off_to_night, proportion_night_to_color


def __is_station_ok_to_call__(
    icao_code: str
) -> bool:
    """
    Tells us if a station is OK to make a call to.
    This rate limits calls when a METAR is expired
    but the station has not yet updated.

    Args:
        icao_code (str): The station identifier code.

    Returns:
        bool: True if that station is OK to call.
    """

    if icao_code not in __station_last_called__:
        return True

    try:
        delta_time = datetime.utcnow() - __station_last_called__[icao_code]
        time_since_last_call = (delta_time.total_seconds()) / 60.0

        return time_since_last_call > 1.0
    except Exception:
        return True


def get_metars(
    airport_icao_codes: list
) -> list:
    """
    Returns the METAR data from the web for the list of stations

    Arguments:
        airport_icao_code {string} -- The list of ICAO code for the weather station.

    Returns:
        dictionary - A dictionary (keyed by airport code) of the metars.
        Returns INVALID as the value for the key if an error occurs.
    """

    metars = {}

    # For the airports and identifiers that we were not able to get
    # a result for, see if we can fill in the results.
    for identifier in airport_icao_codes:
        # If we did not get a report, but do
        # still have an old report, then use the old
        # report.
        cache_valid, report = __is_cache_valid__(
            identifier,
            __metar_report_cache__)

        is_ready_to_call = __is_station_ok_to_call__(identifier)

        # DLQ debug
        # pprint("sta: {}, cache_valid: {}, report: {}, ready? {}".format(identifier, cache_valid, report, is_ready_to_call))

        if cache_valid and report is not None and not is_ready_to_call:
            # Falling back to cached METAR for rate limiting
            metars[identifier] = __metar_report_cache__[identifier][1]
            safe_log("Return Cached WX for {}={}".format(identifier, metars[identifier]['raw_text']))
        # Fall back to an "INVALID" if everything else failed.
        else:
            try:
                new_metar = fetch_metars([identifier])  # this caches it too
                new_report = new_metar[identifier]
                safe_log("New WX for {}={}".format(identifier, new_report['raw_text']))

                if new_report is None or len(new_report) < 1:
                    continue

                metars[identifier] = new_report
                safe_log('{}:{}'.format(identifier, new_report['raw_text']))

            except Exception as e:
                safe_log_warning(
                    'get_metars, being set to INVALID EX:{}'.format(e))
                metars[identifier] = INVALID
    return metars


def fetch_metars(
    airport_icao_codes: list
) -> dict:
    """
    Calls to the web an attempts to gets the METARs for the requested station list.

    Arguments:
        airport_icao_code {string[]} -- Array of stations to get METARs for.

    Returns:
        dictionary -- Returns a map of METARs keyed by the station code.
    """

    metars = {}

    # checkwx allows up to 20 requests on a single call
    for i in range(0, len(airport_icao_codes), 20):
        partial_list = ",".join(airport_icao_codes[i:i + 20])
        print("fetching metars: get batch of {}. {}".format(len(airport_icao_codes[i:i + 20]), partial_list))
        req = requests.get("{}/metar/{}/decoded".format(api_service_base, partial_list), headers=hdr)
        try:
            req.raise_for_status()
            resp = json.loads(req.text)
        except requests.exceptions.HTTPError as e:
            print(e)

        for airport in resp['data']:
            icao = airport['icao']
            metars[icao] = airport
            __station_last_called__[icao] = datetime.utcnow()
            __set_cache__(icao, __metar_report_cache__, airport)

    return metars


# Initial load
fetch_metars(list(airports_config.keys()))


def get_metar(
    airport_icao_code: str,
    use_cache: bool = True
) -> str:
    """
    Returns the METAR for the given station

    Arguments:
        airport_icao_code {string} -- The ICAO code for the weather station.

    Keyword Arguments:
        use_cache {bool} -- Should we use the cache? Set to false to bypass the cache. (default: {True})
    """

    if airport_icao_code is None or len(airport_icao_code) < 1:
        safe_log('Invalid or empty airport code')

    is_cache_valid, cached_metar = __is_cache_valid__(
        airport_icao_code,
        __metar_report_cache__)

    # Make sure that we used the most recent reports we can.
    # Metars are normally updated hourly.
    if is_cache_valid and cached_metar != INVALID:
        metar_age = get_metar_age(cached_metar).total_seconds() / 60.0

        if use_cache and metar_age < DEFAULT_METAR_LIFESPAN_MINUTES:
            return cached_metar

    try:
        # get a new metar (non-cached)
        metars = get_metars([airport_icao_code])

        if metars is None:
            safe_log(
                'Get a None while attempting to get METAR for {}'.format(
                    airport_icao_code))
            return None

        if airport_icao_code not in metars:
            safe_log(
                'Got a result, but {} was not in results package'.format(
                    airport_icao_code))

            return None

        return metars[airport_icao_code]

    except Exception as e:
        safe_log('get_metar got EX:{}'.format(e))
        safe_log("")

        return None


def get_metar_age(
    metar: dict,
    current_time: datetime = datetime.utcnow().replace(tzinfo=timezone.utc)
) -> timedelta:
    """
    Returns the age of the METAR

    Arguments:
        metar {string} -- The METAR to get the age from.

    Returns:
        timedelta -- The age of the metar, None if it can not be determined.
    """

    try:
        # ex. 2022-09-12T21:53Z
        metar_date = datetime.strptime(metar["observed"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
        return current_time - metar_date

    except Exception as e:
        safe_log_warning("Exception while getting METAR age:{}".format(e))
        return None


def is_lightning(
    metar: dict
) -> bool:
    """
    Checks if the metar contains a report for lightning.

    Args:
        metar (str): The metar to see if it contains lightning.

    Returns:
        bool: True if the metar contains lightning.
    """
    if metar is None:
        return False

    contains_lightning = re.search('.* LTG.*', metar['raw_text']) is not None

    return contains_lightning


def get_visibility(
    metar: dict
):
    """
    Returns the flight rules classification based on visibility from a RAW metar.

    Arguments:
        metar {string} -- The RAW weather report in METAR format.

    Returns:
        string -- The flight rules classification, or INVALID in case of an error.
    """

    return metar['visibility']['miles']


# # TODO: metar.ceiling.base_feet_agl
# def get_ceiling(
#     metar: dict
# ):
#     """
#     Returns the flight rules classification based on ceiling from a RAW metar.
#
#     Arguments:
#         metar {string} -- The RAW weather report in METAR format.
#
#     Returns:
#         string -- The ceiling, or INVALID in case of an error.
#     """
#
#     return metar['ceiling']['feet']


# TODO: metar.temperature.celsius
def get_temperature(
    station: str
) -> int:
    """
    Returns the temperature (celsius) from the given metar string.

    Args:
        metar (string): The metar to extract the temperature reading from.

    Returns:
        int: The temperature in celsius.
    """
    if __metar_report_cache__[station] is None:
        return None

    return __metar_report_cache__[station][1]['temperature']['celsius']


# TODO: metar.barometer.hg
def get_pressure(
    metar: str
) -> float:
    """
    Get the inches of mercury from a METAR.
    This **DOES NOT** extract the Sea Level Pressure
    from the remarks section.

    Args:
        metar (str): The metar to extract the pressure from.

    Returns:
        float: None if not found, otherwise the inches of mercury. EX:29.92
    """

    return metar['barometer']['hg']


def get_precipitation(
    metar: str
) -> bool:
    if metar is None:
        return None

    components = get_main_metar_components(metar)

    for component in components:
        if 'UP' in component:
            return UNKNOWN
        elif 'RA' in component:
            return HEAVY_RAIN if '+' in component else RAIN
        elif 'GR' in component or 'GS' in component or 'IC' in component or 'PL' in component:
            return ICE
        elif 'SN' in component or 'SG' in component:
            return SNOW
        elif 'DZ' in component:
            return DRIZZLE

    return None





def is_station_inoperative(
    metar: str
) -> bool:
    """
    Tells you if the weather station is operative or inoperative.
    Inoperative is mostly defined as not having an updated METAR
    in the allowable time period.

    Args:
        metar (str): The METAR to check.

    Returns:
        bool: True if the station is INOPERATIVE. This means the METAR should be ignored.
    """
    if metar is None or metar == INVALID:
        return True

    metar_age = get_metar_age(metar)

    if metar_age is not None:
        metar_age_minutes = metar_age.total_seconds() / 60.0
        metar_inactive_threshold = configuration.get_metar_station_inactive_minutes()
        is_inactive = metar_age_minutes > metar_inactive_threshold

        return is_inactive

    return False


# TODO: metar.flight_category
def get_category(
    metar: dict
) -> str:
    """
    Returns the flight rules classification based on the entire RAW metar.

    Arguments:
        airport_icao_code -- The airport or weather station that we want to get a category for.
        metar {string} -- The RAW weather report in METAR format.
        return_night {boolean} -- Should we return a category for NIGHT?

    Returns:
        string -- The flight rules classification, or INVALID in case of an error.
    """
    if metar is None or metar == INVALID:
        return INVALID

    return metar['flight_category']

if __name__ == '__main__':
    print('Starting self-test')

    # airports = ['KHOU', 'KIAH', 'KEFD', 'KDWH', 'NOTAGOODCODE']
    airports = list(airports_config.keys())

    starting_date_time = datetime.utcnow()
    utc_offset = starting_date_time - datetime.now()

    metars = get_metars(airports)
    pprint(get_metar("KIAH", False))
    print("total number of metars: {}".format(len(metars)))

    print("flight category for KIAH: {}".format(get_category(metars['KIAH'])))
    print("metar age for KIAH: {}  current: {}   metar_date: {}".format(get_metar_age(metars['KIAH']), datetime.utcnow(), metars['KIAH']['observed']))
    # print(get_metar('KIAH', metars, use_cache=False))
    # print("Temp: {}C".format(get_temperature('KIAH')))

    # si = __station_info__['KIAH']
    # pprint(si, indent=4)
    # print('Sunrise start/dawn:{0}'.format(si['dawn'] - utc_offset))
    # print('Sunrise:{0}'.format(si['sunrise'] - utc_offset))
    # print('Full light:{0}'.format(si['full_light_start'] - utc_offset))
    # print('Sunset start:{0}'.format(si['full_light_end'] - utc_offset))
    # print('Sunset:{0}'.format(si['sunset'] - utc_offset))
    # print('Full dark/dusk:{0}'.format(si['dusk'] - utc_offset))

    # for id in airports:
    #     metar = get_metar(id)
    #     # age = get_metar_age(metar)
    #     flight_category = get_category(metars[id])
    #     print('{}: {}'.format(id, flight_category))

    # for hours_ahead in range(0, 240):
    #     hours_ahead *= 0.1
    #     time_to_fetch = starting_date_time + timedelta(hours=hours_ahead)
    #     local_fetch_time = time_to_fetch - utc_offset

        # for airport in ['KDWH', 'KIAH']:
        #     is_lit = is_daylight(airport, time_to_fetch)
        #     print("{} is lit? {}".format(airport, is_lit))
        #     # is_dark = is_night(airport, time_to_fetch)
            # transition = get_twilight_transition(airport, time_to_fetch)

            # print(
            #     "DELTA=+{0:.1f}, LOCAL={1}, AIRPORT={2}: is_day={3}, is_night={4}, p_dark:{5:.1f}, p_color:{6:.1f}".format(
            #         hours_ahead,
            #         local_fetch_time,
            #         airport,
            #         is_lit,
            #         is_dark,
            #         transition[0],
            #         transition[1]))
