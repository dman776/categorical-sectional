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

# cache elements
TIMESTAMP = 0
DATA = 1

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

    return datetime.strptime(datetime_string, "%Y-%m-%dT%H:%M:%S")


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
        req = requests.get("{}/station/{}/suntimes?iso=1".format(api_service_base, partial_list), headers=hdr)
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


__airports_config__ = configuration.get_airport_configs()
__station_info__ = __fetch_airport_data__(list(__airports_config__.keys()))

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
                return (True, cache[station_icao_code][DATA])
            else:
                return (False, cache[station_icao_code][DATA])
    except Exception:
        pass
    finally:
        __cache_lock__.release()

    return (False, None)


def is_daylight(
    station: str,
    current_utc_time: datetime = datetime.utcnow().replace(tzinfo=timezone.utc),
    use_cache: bool = True
) -> bool:
    """
    Returns TRUE if the airport is currently in daylight

    Arguments:
        station {string} -- The airport code to test.

    Returns:
        boolean -- True if the airport is currently in daylight.
    """

    lsi = __station_info__[station]

    if lsi is not None:
        # Deal with day old data...
        hours_since_sunrise = (current_utc_time - lsi['sunrise'].replace(tzinfo=timezone.utc)).total_seconds() / 3600
        if hours_since_sunrise < 0:
            one_station = __fetch_airport_data__([station])
            __set_cache__(station, __station_info__, one_station[station])
            lsi = __station_info__[station]

        if hours_since_sunrise > 24:
            return True

        # Make sure the time between takes into account
        # The amount of time sunrise or sunset takes

        is_after_sunrise = lsi['full_light_start'].replace(tzinfo=timezone.utc) < current_utc_time
        is_before_sunset = current_utc_time < lsi['full_light_end'].replace(tzinfo=timezone.utc)

        return is_after_sunrise and is_before_sunset

    return True


def is_night(
    station: str,
    current_utc_time: datetime = datetime.utcnow().replace(tzinfo=timezone.utc),
    use_cache: bool = True
) -> bool:
    """
    Returns TRUE if the airport is currently in night

    Arguments:
        station {string} -- The airport code to test.

    Returns:
        boolean -- True if the airport is currently in night.
    """

    si = __station_info__[station]

    if si is not None:
        # Deal with day old data...
        hours_since_sunrise = (current_utc_time - si['sunrise'].replace(tzinfo=timezone.utc)).total_seconds() / 3600

        if hours_since_sunrise < 0:
            one_station = __fetch_airport_data__([station])
            __set_cache__(station, __station_info__, one_station[station])
            si = __station_info__[station]

        if hours_since_sunrise > 24:
            return False

        # Make sure the time between takes into account
        # The amount of time sunrise or sunset takes
        is_before_sunrise = current_utc_time < si['sunrise'].replace(tzinfo=timezone.utc)
        is_after_sunset = current_utc_time > si['dusk'].replace(tzinfo=timezone.utc)

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
    station,
    current_utc_time=None,
    use_cache=True
):
    """
    Returns the mix of dark & color fade for twilight transitions.

    Arguments:
        station {string} -- The ICAO code of the weather station.

    Keyword Arguments:
        current_utc_time {datetime} -- The time in UTC to calculate the mix for. (default: {None})
        use_cache {bool} -- Should the cache be used to determine the sunrise/sunset/transition data. (default: {True})

    Returns:
        tuple -- (proportion_off_to_night, proportion_night_to_category)
    """

    if current_utc_time is None:
        current_utc_time = datetime.utcnow().replace(tzinfo=timezone.utc)

    # light_times = get_civil_twilight(station, current_utc_time, use_cache)
    #
    # if light_times is None or len(light_times) < 5:
    #     return 0.0, 1.0

    if is_daylight(station, current_utc_time, use_cache):
        return 0.0, 1.0

    if is_night(station, current_utc_time, use_cache):
        return 0.0, 0.0

    proportion_off_to_night = 0.0
    proportion_night_to_color = 0.0

    try:
        asi = __station_info__[station][DATA]
    except KeyError as e:
        return (0.0, 1.0)


    # Sunsetting: Night to off
    if current_utc_time >= asi['sunset']:
        proportion_off_to_night = 1.0 - \
            get_proportion_between_times(
                asi['sunset'],
                current_utc_time, asi['dusk'])
    # Sunsetting: Color to night
    elif current_utc_time >= asi['full_light_ends']:
        proportion_night_to_color = 1.0 - \
            get_proportion_between_times(
                asi['full_light_ends'],
                current_utc_time, asi['sunset'])
    # Sunrising: Night to color
    elif current_utc_time >= asi['sunrise']:
        proportion_night_to_color = get_proportion_between_times(
            asi['sunrise'],
            current_utc_time, asi['full_light_starts'])
    # Sunrising: off to night
    else:
        proportion_off_to_night = get_proportion_between_times(
            asi['dawn'],
            current_utc_time, asi['sunrise'])

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
    airport_icao_codes: list,
    use_cache=True
) -> dict:
    """
    Returns the METAR data from the web for the list of stations

    Arguments:
        airport_icao_code {string} -- The list of ICAO code for the weather station.

    Returns:
        dictionary - A dictionary (keyed by airport code) of the metars.
        Returns INVALID as the value for the key if an error occurs.
    """

    metars = {}
    ids_to_fetch = []

    for id in airport_icao_codes:
        # If we did not get a report, but do
        # still have an old report, then use the old
        # report.

        cache_valid, report = __is_cache_valid__(id, __metar_report_cache__)
        is_ready_to_call = __is_station_ok_to_call__(id)
        print("get_metars: id={} cache_valid={} is_ready={}".format(id, cache_valid, is_ready_to_call))

        if use_cache and cache_valid and report is not None and not is_ready_to_call:
            # Falling back to cached METAR for rate limiting
            metars[id] = __metar_report_cache__[id][1]
            safe_log("Cached WX for {}={}".format(id, metars[id]['raw_text']))
        else:
            # this one needs to be fetched.  Add it to the list.
            ids_to_fetch.append(id)

    # process the needed metars
    try:
        new_metars = fetch_metars(ids_to_fetch)

        for nm in new_metars.keys():
            # IF INVALID, DO SOMETHING ELSE
            if isinstance(new_metars[nm], str): # and new_metars[nm] == 'INVALID':
                safe_log("Invalid WX for {}".format(nm))
            else:
                icao = new_metars[nm]['icao']
                safe_log("New WX for {}={}".format(icao, new_metars[nm]['raw_text']))

                metars[icao] = new_metars[nm]
                # cache it
                __station_last_called__[icao] = datetime.utcnow()     # why not use __metar_report_cache__[0] ???
                __set_cache__(icao, __metar_report_cache__, new_metars[nm])
    except Exception as e:
        # Fall back to an "INVALID" if everything else failed.
        safe_log_warning(
            'get_metars, being set to INVALID EX:{}'.format(e))
        metars[icao] = INVALID
    return metars


def fetch_metars(
    airport_icao_codes: list
) -> dict:
    """
    Calls to the web an attempts to gets the METARs for the requested station list.

    Arguments:
        airport_icao_code {string[]} -- Array of stations to get METARs for.

    Returns:
        dictionary -- Returns a dict of METARs keyed by the station code.
    """

    ret_metars = {}

    # checkwx allows up to 20 requests on a single call
    for i in range(0, len(airport_icao_codes), 20):
        resp = {}
        partial_list = ",".join(airport_icao_codes[i:i + 20])
        print("fetch_metars: get batch of {}. {}".format(len(airport_icao_codes[i:i + 20]), partial_list))
        req = requests.get("{}/metar/{}/decoded".format(api_service_base, partial_list), headers=hdr)
        try:
            req.raise_for_status()
            resp = json.loads(req.text)

            for airport in resp['data']:
                icao = airport['icao']
                ret_metars[icao] = airport
        except requests.exceptions.HTTPError as e:
            print(e)

    # check if all codes were retrieved
    if len(ret_metars) != len(airport_icao_codes):
        for a in airport_icao_codes:
            if a not in ret_metars.keys():
                ret_metars[a] = INVALID
    return ret_metars


# Initial load
# fetch_metars(list(airports_config.keys()))


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
        metars = get_metars([airport_icao_code], use_cache)

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
    station: str
) -> bool:
    """
    Checks if the metar contains a report for lightning.

    Args:
        station (str): The station to see if it contains lightning.

    Returns:
        bool: True if the metar contains lightning.
    """
    if __metar_report_cache__[station] is None:
        return None

    contains_lightning = re.search('.* LTG.*', __metar_report_cache__[station][DATA]['raw_text']) is not None
    return contains_lightning


def get_visibility(
    station: str
):
    """
    Returns the flight rules classification based on visibility from a RAW metar.

    Arguments:
        station {string} -- The station to get the visibility for.

    Returns:
        string -- The flight rules classification, or INVALID in case of an error.
    """
    if __metar_report_cache__[station] is None:
        return None

    return __metar_report_cache__[station][DATA]['visibility']['miles']


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

    return __metar_report_cache__[station][DATA]['temperature']['celsius']


# TODO: metar.barometer.hg
def get_pressure(
    station: str
) -> float:
    """
    Get the inches of mercury from a METAR.
    This **DOES NOT** extract the Sea Level Pressure
    from the remarks section.

    Args:
        station (str): the station to get the pressure from.

    Returns:
        float: None if not found, otherwise the inches of mercury. EX:29.92
    """

    if __metar_report_cache__[station] is None:
        return None

    return __metar_report_cache__[station][DATA]['barometer']['hg']


def get_precipitation(
    station: str
) -> str:

    if __metar_report_cache__[station] is None:
        return None

    try:
        # 'conditions': [{'code': 'RA', 'prefix': '-', 'text': 'Light Rain'}],
        for cond in __metar_report_cache__[station][DATA]['conditions']:
            if 'UP' == cond['code']:
                return UNKNOWN
            elif 'RA' == cond['code']:
                return HEAVY_RAIN if '+' == cond['prefix'] else RAIN
            elif 'GR' == cond['code'] or 'GS' == cond['code'] or 'IC' == cond['code'] or 'PL' == cond['code']:
                return ICE
            elif 'SN' == cond['code'] or 'SG' == cond['code']:
                return SNOW
            elif 'DZ' == cond['code']:
                return DRIZZLE
    except KeyError as e:
        return None


def is_station_inoperative(
    station: str
) -> bool:
    """
    Tells you if the weather station is operative or inoperative.
    Inoperative is mostly defined as not having an updated METAR
    in the allowable time period.

    Args:
        station (str): The station to check.

    Returns:
        bool: True if the station is INOPERATIVE. This means the METAR should be ignored.
    """

    try:
        if __metar_report_cache__[station] is None:
            return True

        metar_age = get_metar_age(station)

        if metar_age is not None:
            metar_age_minutes = metar_age.total_seconds() / 60.0
            metar_inactive_threshold = configuration.get_metar_station_inactive_minutes()
            is_inactive = metar_age_minutes > metar_inactive_threshold

            return is_inactive
    except KeyError as e:
        return True
    return False


# TODO: metar.flight_category
def get_category(
    station: str
) -> str:
    """
    Returns the flight rules classification based on the entire RAW metar.

    Arguments:
        station -- The airport or weather station that we want to get a category for.

    Returns:
        string -- The flight rules classification, or INVALID in case of an error.
    """
    try:
        if __metar_report_cache__[station] is None:
            return INVALID
        return __metar_report_cache__[station][DATA]['flight_category']
    except KeyError as e:
        return INVALID



if __name__ == '__main__':
    print('Starting self-test...')
    TESTSTATION="KDWH"
    # airports = [TESTSTATION, 'KSEZ', 'KHOU', 'KIAH', 'KEFD', 'KDWH', 'NOTAGOODCODE']
    airports = list(__airports_config__.keys())
    # airports = __airports_config__

    starting_date_time = datetime.utcnow().replace(tzinfo=timezone.utc)
    utc_offset = starting_date_time - datetime.now().replace(tzinfo=timezone.utc)

    metars = get_metars(airports, False)


    print("# metars returned: {}".format(len(metars)))
    print("# in cache: {}".format(len(__metar_report_cache__)))
    # pprint(get_metars(airports, True))

    # pprint(__metar_report_cache__['KDWH'][TIMESTAMP])
    # pprint(__metar_report_cache__['KDWH'][DATA])

    print("flight category: {}".format(get_category(TESTSTATION)))
    # print("metar age: {}  current: {}   metar_date: {}".format(get_metar_age(metars[TESTSTATION]), datetime.utcnow(), metars[TESTSTATION]['observed']))
    # # print(get_metar(TESTSTATION, use_cache=False))
    print("Temp: {}C".format(get_temperature(TESTSTATION)))
    print("Precip: {}".format(get_precipitation(TESTSTATION)))
    print("Pressure: {}".format(get_pressure(TESTSTATION)))
    print("Visibility: {} miles".format(get_visibility(TESTSTATION)))
    print("Lightning: {}".format(is_lightning(TESTSTATION)))
    print("INOP?: {}".format(is_station_inoperative(TESTSTATION)))

    # asi = __station_info__[TESTSTATION]
    # pprint(asi, indent=4)    # should be 2 elements, datetime & dict
    # print('Sunrise start/dawn:{0}'.format(asi['dawn'] - utc_offset))
    # print('Sunrise:{0}'.format(asi['sunrise'] - utc_offset))
    # print('Full light:{0}'.format(asi['full_light_start'] - utc_offset))
    # print('Sunset start:{0}'.format(asi['full_light_end'] - utc_offset))
    # print('Sunset:{0}'.format(asi['sunset'] - utc_offset))
    # print('Full dark/dusk:{0}'.format(asi['dusk'] - utc_offset))
    # print('is Daylight? {}'.format(is_daylight(TESTSTATION)))
    # print('is night? {}'.format(is_night(TESTSTATION)))
    # print('twilight_transition: {}'.format(get_twilight_transition(TESTSTATION)))

    for id in airports:
        # metar = get_metar(id)
        # age = get_metar_age(metar)
        flight_category = get_category(id)
        print('{}: {}'.format(id, flight_category))

    for hours_ahead in range(0, 240):
        hours_ahead *= 0.1
        time_to_fetch = starting_date_time + timedelta(hours=hours_ahead)
        local_fetch_time = time_to_fetch - utc_offset

        for airport in ['KDWH']:
            is_lit = is_daylight(airport, time_to_fetch)
            is_dark = is_night(airport, time_to_fetch)
            transition = get_twilight_transition(airport, time_to_fetch)

            print(
                "DELTA=+{0:.1f}, LOCAL={1}, AIRPORT={2}: is_day={3}, is_night={4}, p_dark:{5:.1f}, p_color:{6:.1f}".format(
                    hours_ahead,
                    local_fetch_time,
                    airport,
                    is_lit,
                    is_dark,
                    transition[0],
                    transition[1]))
