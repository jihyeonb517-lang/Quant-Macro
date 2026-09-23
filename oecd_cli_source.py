"""Download OECD amplitude-adjusted CLI data directly from OECD SDMX."""

import math
import xml.etree.ElementTree as ET
from datetime import date
from urllib.error import HTTPError
from urllib.request import Request, urlopen


SERIES = {
    'us': 'USA',
    'jp': 'JPN',
    'kr': 'KOR',
}

OECD_DATA_URL = (
    'https://sdmx.oecd.org/public/rest/data/'
    'OECD.SDD.STES,DSD_STES@DF_CLI,{country}.M.LI...AA...H'
    '?startPeriod=1950-01'
)


def _next_month(day):
    """Return the first day of the month following YYYY-MM-01."""
    current = date.fromisoformat(day)

    if current.month == 12:
        return f'{current.year + 1:04d}-01-01'

    return f'{current.year:04d}-{current.month + 1:02d}-01'


def _consecutive_month(previous_day, current_day):
    """Return True only when current_day is exactly one month later."""
    return _next_month(previous_day) == current_day


def parse_oecd_sdmx_xml(text, country):
    """Parse SDMX generic-data XML for a single country's monthly CLI."""
    try:
        root = ET.fromstring(text.lstrip('\ufeff'))
    except ET.ParseError as exc:
        raise ValueError('Unexpected OECD SDMX response') from exc

    def local_name(element):
        return element.tag.rsplit('}', 1)[-1]

    points = []
    for series in (element for element in root.iter() if local_name(element) == 'Series'):
        key_values = {
            item.attrib.get('id'): item.attrib.get('value')
            for item in series.iter()
            if local_name(item) == 'Value'
        }
        if key_values.get('REF_AREA') != country.upper():
            continue
        for observation in (element for element in series.iter()
                            if local_name(element) == 'Obs'):
            dimension = next((child for child in observation.iter()
                              if local_name(child) == 'ObsDimension'), None)
            value_node = next((child for child in observation.iter()
                               if local_name(child) == 'ObsValue'), None)
            if dimension is None:
                continue
            period = dimension.attrib.get('value', '')
            try:
                parsed = date.fromisoformat(period + '-01')
            except ValueError as exc:
                raise ValueError(f'Invalid OECD CLI month: {period}') from exc
            raw_value = value_node.attrib.get('value', '') if value_node is not None else ''
            if raw_value in ('', '.', 'NA', 'NaN'):
                value = None
            else:
                try:
                    value = float(raw_value)
                except ValueError as exc:
                    raise ValueError(f'Invalid OECD CLI value: {raw_value}') from exc
                if not math.isfinite(value):
                    value = None
            points.append([parsed.replace(day=1).isoformat(), value])

    by_date = {}
    for day, value in points:
        if day in by_date and by_date[day] != value:
            raise ValueError(f'Duplicate OECD CLI observation: {day}')
        by_date[day] = value
    if not by_date:
        raise ValueError(f'OECD returned no CLI observations for {country.upper()}')
    return [[day, value] for day, value in sorted(by_date.items())]


def classify_month(previous_value, current_value):
    """Classify one month using the OECD CLI clock definition."""
    if previous_value is None or current_value is None:
        return None

    # Strict inequalities follow the dashboard's selected definition.
    # Exactly 100 or an unchanged value receives no regime.
    if current_value == 100 or current_value == previous_value:
        return None

    rising = current_value > previous_value

    if current_value > 100:
        return 'expansion' if rising else 'slowdown'

    return 'recovery' if rising else 'contraction'


def calculate_regimes(points):
    """
    Convert monthly CLI observations into half-open regime intervals.

    Each interval has the form:
        [start_date, end_date_exclusive, regime]

    A CLI value applies only to its own calendar month. The last available
    observation is never extended beyond the end of that month.
    """
    regimes = []

    for index in range(1, len(points)):
        previous_day, previous_value = points[index - 1]
        current_day, current_value = points[index]

        # Do not compare across a missing calendar month.
        if not _consecutive_month(previous_day, current_day):
            continue

        regime = classify_month(previous_value, current_value)

        if regime is None:
            continue

        end_day = _next_month(current_day)

        # Merge adjacent months only when both their regime and dates connect.
        if (
            regimes
            and regimes[-1][2] == regime
            and regimes[-1][1] == current_day
        ):
            regimes[-1][1] = end_day
        else:
            regimes.append([current_day, end_day, regime])

    return regimes


def fetch_points(country):
    """Download one country's current OECD CLI history from OECD SDMX."""
    if country not in SERIES:
        raise KeyError(f'Unsupported OECD CLI country: {country}')

    country_code = SERIES[country]
    url = OECD_DATA_URL.format(country=country_code)
    request = Request(url, headers={
        'Accept': 'application/vnd.sdmx.data+generic-2.1+xml',
        'User-Agent': 'macro-observer/1.0',
    })
    try:
        with urlopen(request, timeout=20) as response:
            payload = response.read().decode('utf-8-sig')
    except HTTPError as exc:
        raise RuntimeError(f'OECD CLI request failed (HTTP {exc.code})') from None
    except Exception as exc:
        raise RuntimeError(f'OECD CLI request failed ({type(exc).__name__})') from None

    points = parse_oecd_sdmx_xml(payload, country_code)

    if not any(value is not None for _, value in points):
        raise ValueError(f'No usable OECD CLI observations for {country}')

    return points


def fetch_regimes(country):
    """Download one country and return its calculated regime intervals."""
    return calculate_regimes(fetch_points(country))

