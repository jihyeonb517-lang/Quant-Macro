"""Download OECD amplitude-adjusted CLI data and calculate monthly regimes."""

import csv
import io
from datetime import date


SERIES = {
    'us': 'USALOLITOAASTSAM',
    'jp': 'JPNLOLITOAASTSAM',
    'kr': 'KORLOLITOAASTSAM',
}

FRED_CSV_URL = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id={}'


def _next_month(day):
    """Return the first day of the month following YYYY-MM-01."""
    current = date.fromisoformat(day)

    if current.month == 12:
        return f'{current.year + 1:04d}-01-01'

    return f'{current.year:04d}-{current.month + 1:02d}-01'


def _consecutive_month(previous_day, current_day):
    """Return True only when current_day is exactly one month later."""
    return _next_month(previous_day) == current_day


def parse_fred_csv(text, series_id):
    """Parse a FRED CSV response without interpolating missing observations."""
    rows = list(csv.reader(io.StringIO(text.lstrip('\ufeff'))))

    if not rows or len(rows[0]) != 2:
        raise ValueError('Unexpected FRED CSV header')

    if rows[0][1] != series_id:
        raise ValueError(
            f'Unexpected FRED series: expected {series_id}, got {rows[0][1]}'
        )

    points = []
    seen_dates = set()

    for line_number, row in enumerate(rows[1:], 2):
        if len(row) != 2:
            raise ValueError(f'Unexpected FRED CSV row on line {line_number}')

        day = row[0].strip()
        raw_value = row[1].strip()

        if not day:
            continue

        try:
            parsed_day = date.fromisoformat(day)
        except ValueError as exc:
            raise ValueError(
                f'Invalid FRED date on line {line_number}: {day}'
            ) from exc

        # OECD CLI is monthly and represented on the first day of each month.
        if parsed_day.day != 1:
            raise ValueError(
                f'Unexpected non-monthly observation date: {day}'
            )

        if day in seen_dates:
            raise ValueError(f'Duplicate OECD CLI observation: {day}')

        seen_dates.add(day)

        # FRED uses "." for missing observations. Keep the month missing.
        if raw_value in ('', '.', 'NA', 'NaN'):
            points.append([day, None])
            continue

        try:
            value = float(raw_value)
        except ValueError as exc:
            raise ValueError(
                f'Invalid OECD CLI value on line {line_number}: {raw_value}'
            ) from exc

        points.append([day, value])

    return sorted(points)


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
    """Download one country's current OECD CLI history from FRED."""
    if country not in SERIES:
        raise KeyError(f'Unsupported OECD CLI country: {country}')

    series_id = SERIES[country]
    url = FRED_CSV_URL.format(series_id)

    from curl_cffi import requests
    from curl_cffi.const import CurlHttpVersion

    response = requests.get(
        url,
        impersonate='chrome',
        timeout=15,
        http_version=CurlHttpVersion.V1_1,
    )
    response.raise_for_status()

    points = parse_fred_csv(
        response.content.decode('utf-8-sig'),
        series_id,
    )

    if not any(value is not None for _, value in points):
        raise ValueError(f'No usable OECD CLI observations for {country}')

    return points


def fetch_regimes(country):
    """Download one country and return its calculated regime intervals."""
    return calculate_regimes(fetch_points(country))
