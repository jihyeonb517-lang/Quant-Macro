"""Treasury DTS daily TGA closing balances, in millions of US dollars.

Fiscal Data is public and requires no API key. Account labels and value fields
changed in 2021/2022; never combine these daily levels with WTREGEN averages.
"""
import json
import math
from datetime import date
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SERIES_ID = 'TREASURY_TGA'
API_URL = ('https://api.fiscaldata.treasury.gov/services/api/fiscal_service/'
           'v1/accounting/dts/operating_cash_balance')
DATASET_URL = 'https://fiscaldata.treasury.gov/datasets/daily-treasury-statement/operating-cash-balance'
VALUE_FIELDS = {
    'Federal Reserve Account': 'close_today_bal',
    'Treasury General Account (TGA)': 'close_today_bal',
    # Modern DTS rows have a separate Closing Balance label, whose amount is
    # in open_today_bal. close_today_bal is null on these rows.
    'Treasury General Account (TGA) Closing Balance': 'open_today_bal',
}
NOTE = ('미국 재무부 DTS의 일별 TGA 마감 잔고입니다. 전 관측 영업일과 비교하며 '
        '휴일은 보간하지 않습니다. 기존 FRED 주간 평균과는 기준이 다릅니다.')
NETLIQ_NOTE = ('연준 자산에서 재무부 일별 TGA 마감 잔고와 역레포를 차감한 참고치입니다. '
              '세 자료의 관측일이 같은 경우만 계산하므로 주간 주기를 유지합니다.')


def parse_points(rows):
    if not isinstance(rows, list):
        raise ValueError('Treasury response omitted data rows')
    points = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Treasury response contains an invalid row')
        field = VALUE_FIELDS.get(row.get('account_type'))
        if not field:  # Opening balance, deposits, withdrawals, and other accounts.
            continue
        try:
            day = date.fromisoformat(row['record_date']).isoformat()
            amount = str(row[field]).strip()
            value = None if amount in {'null', '', 'None'} else float(amount.replace(',', ''))
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError('Invalid balance')
        except (KeyError, ValueError, TypeError):
            raise ValueError('Treasury response contains an invalid closing balance') from None
        if day in points and points[day] != value:
            raise ValueError('Treasury response contains conflicting closing balances')
        points[day] = value
    if not points or not any(v is not None for v in points.values()):
        raise ValueError('Treasury response contains no usable TGA closing balances')
    return [[d, v] for d, v in sorted(points.items())]


def fetch_points():
    # The filtered history has about 5,000 rows. Pagination remains supported
    # as the dataset grows; no full-history FRED cache is reused here.
    rows, page, total_pages = [], 1, 1
    while page <= total_pages:
        params = {
            'fields': 'record_date,account_type,close_today_bal,open_today_bal',
            'filter': 'account_type:in:(' + ','.join(VALUE_FIELDS) + ')',
            'sort': 'record_date,account_type',
            'page[size]': 10000,
            'page[number]': page,
        }
        request = Request(API_URL + '?' + urlencode(params),
                          headers={'Accept': 'application/json', 'User-Agent': 'Quant-Macro/1.0'})
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode('utf-8'))
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
            raise ValueError('Unexpected Treasury JSON response')
        formats = payload.get('meta', {}).get('dataFormats', {})
        if any(formats.get(field) != '$1,000,000' for field in set(VALUE_FIELDS.values())):
            raise ValueError('Unexpected Treasury balance units; expected millions of USD')
        try:
            total_pages = int(payload['meta']['total-pages'])
        except (KeyError, ValueError, TypeError):
            raise ValueError('Treasury response omitted valid pagination metadata') from None
        if not 1 <= total_pages <= 20 or not payload['data']:
            raise ValueError('Treasury response contains an empty or excessive page range')
        rows.extend(payload['data'])
        page += 1
    return parse_points(rows)
