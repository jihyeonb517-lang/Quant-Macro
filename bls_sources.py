"""Direct BLS seasonally adjusted employment levels, in thousands (rate in %).

Internal aliases retain the durable history originally distributed by FRED.
New downloads and revisions come directly from BLS; credentials never enter
URLs, source metadata, cached observations, or exception messages.
"""
import json
import math
import os
from datetime import datetime, timezone
from urllib.request import Request, urlopen

API_URL = 'https://api.bls.gov/publicAPI/v2/timeseries/data/'
SERIES = {
    'PAYEMS': 'CES0000000001',
    'USPRIV': 'CES0500000001',
    'USGOVT': 'CES9000000001',
    'UNRATE': 'LNS14000000',
}
NOTE = 'BLS 계절조정 월간 고용 증가분입니다. 비교값은 지난달 증가분과의 차이이며 과거 수치는 수정될 수 있습니다.'


def parse_points(payload, series_id):
    if not isinstance(payload, dict) or payload.get('status') != 'REQUEST_SUCCEEDED':
        raise ValueError('BLS API did not report a successful request')
    results = payload.get('Results', {})
    if isinstance(results, list):
        results = results[0] if len(results) == 1 else {}
    series = next((s for s in results.get('series', [])
                   if s.get('seriesID') == series_id), None)
    if not series or not series.get('data'):
        raise ValueError('BLS response omitted the requested monthly series')
    points = {}
    for row in series['data']:
        period = str(row.get('period', ''))
        if len(period) != 3 or not period.startswith('M') or not period[1:].isdigit():
            continue
        month = int(period[1:])
        if not 1 <= month <= 12:  # M13 is an annual average, not a month.
            continue
        try:
            year = int(row['year'])
            raw_value = str(row['value']).strip().replace(',', '')
            # BLS uses '-' for unavailable months (e.g. October 2025 CPS).
            # Keep that month empty so comparisons cannot bridge the gap.
            value = None if raw_value == '-' else float(raw_value)
            value = value if value is not None and math.isfinite(value) else None
        except (KeyError, ValueError, TypeError):
            raise ValueError('BLS response contains an invalid monthly observation') from None
        day = f'{year:04d}-{month:02d}-01'
        if day in points and points[day] != value:
            raise ValueError('BLS response contains conflicting monthly observations')
        points[day] = value
    if not points or not any(v is not None for v in points.values()):
        raise ValueError('BLS response contains no usable monthly observations')
    return [[d, v] for d, v in sorted(points.items())]


def fetch_points(alias):
    series_id = SERIES[alias]
    key = os.environ.get('BLS_API_KEY', '').strip()
    year = datetime.now(timezone.utc).year
    # Registered queries allow 20 inclusive years; basic access allows 10.
    # The refresh engine retains older cached history outside this window.
    payload = {'seriesid': [series_id], 'startyear': str(year - (19 if key else 9)),
               'endyear': str(year)}
    if key:
        payload['registrationkey'] = key
    request = Request(API_URL, data=json.dumps(payload).encode('utf-8'),
                      headers={'Content-Type': 'application/json',
                               'User-Agent': 'Quant-Macro/1.0'}, method='POST')
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode('utf-8'))
    except Exception as exc:
        # Do not expose provider responses or exception text containing a key.
        raise RuntimeError(f'BLS API request failed ({type(exc).__name__})') from None
    return parse_points(result, series_id)
