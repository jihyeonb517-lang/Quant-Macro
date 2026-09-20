"""Japan (Ministry of Finance JGB yields) and CFTC yen positioning sources.

No API keys. Imported by fetch_data.py, which merges these definitions into its own tables.
"""
from __future__ import annotations

import csv
import io
import math
import re
from datetime import date, datetime, timezone

MOF_PAGE = 'https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/index.htm'
MOF_URLS = (
    'https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv',  # full history
    'https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv',           # current month
)
MOF_TENORS = {'JGB2': 2, 'JGB10': 10}
CFTC_YEN_CODE = '097741'  # Japanese Yen, CME
CFTC_SOURCES = {
    # dataset id, long-column candidates, short-column candidates
    'CFTC_JPY_LEGACY': ('6dca-aqww', ('noncomm_positions_long_all',), ('noncomm_positions_short_all',)),
    'CFTC_JPY_TFF': ('gpe5-46if', ('lev_money_positions_long', 'lev_money_positions_long_all'),
                     ('lev_money_positions_short', 'lev_money_positions_short_all')),
}
SOURCES = set(MOF_TENORS) | set(CFTC_SOURCES)
FREQUENCIES = {'JGB2': 'daily', 'JGB10': 'daily', 'CFTC_JPY_LEGACY': 'weekly', 'CFTC_JPY_TFF': 'weekly'}
ORIGINS = {
    'JGB2': ('Japan Ministry of Finance', MOF_PAGE),
    'JGB10': ('Japan Ministry of Finance', MOF_PAGE),
    'CFTC_JPY_LEGACY': ('CFTC (Legacy, futures only)', 'https://publicreporting.cftc.gov/'),
    'CFTC_JPY_TFF': ('CFTC (TFF, futures only)', 'https://publicreporting.cftc.gov/'),
}
# id, section, title, unit, dependencies, delta lag, comparison label
SPECS = [
    ('jgb2', 'japan', '일본 국채 2년 금리', '%', ['JGB2'], 20, '20관측일 전 대비'),
    ('jgb10', 'japan', '일본 국채 10년 금리', '%', ['JGB10'], 20, '20관측일 전 대비'),
    ('jp_curve', 'japan', '일본 10년 − 2년 금리', '%p', ['JGB10', 'JGB2'], 20, '20관측일 전 대비'),
    ('us_jp_2y', 'japan', '미국 2년 − 일본 2년 금리', '%p', ['DGS2', 'JGB2'], 20, '20관측일 전 대비'),
    ('us_jp_10y', 'japan', '미국 10년 − 일본 10년 금리', '%p', ['DGS10', 'JGB10'], 20, '20관측일 전 대비'),
    ('cftc_legacy', 'japan', 'CFTC 엔화 비상업 순포지션 (Legacy)', '천 계약', ['CFTC_JPY_LEGACY'], 4, '4주 전 대비'),
    ('cftc_tff', 'japan', 'CFTC 엔화 레버리지 펀드 순포지션 (TFF)', '천 계약', ['CFTC_JPY_TFF'], 4, '4주 전 대비'),
]
FORMULAS = {
    'jgb2': 'MOF constant-maturity JGB yield, 2 years (%); published the next business day',
    'jgb10': 'MOF constant-maturity JGB yield, 10 years (%); published the next business day',
    'jp_curve': 'JGB10 − JGB2; exact same observation date; percentage points',
    'us_jp_2y': 'DGS2 − JGB2 (percentage points); anchored to US dates, the latest JGB value up to 5 days old is used when Japan was closed',
    'us_jp_10y': 'DGS10 − JGB10 (percentage points); anchored to US dates, the latest JGB value up to 5 days old is used when Japan was closed',
    'cftc_legacy': '(Non-commercial long − short, all, futures only) / 1000; CFTC Legacy report, Japanese Yen (code 097741); as-of Tuesday',
    'cftc_tff': '(Leveraged funds long − short, futures only) / 1000; CFTC TFF report, Japanese Yen (code 097741); as-of Tuesday',
}
NOTES = {
    'us_jp_2y': '미국과 일본의 휴장일이 달라, 일본 휴장일에는 최대 5일 이전의 일본 금리를 사용합니다.',
    'us_jp_10y': '미국과 일본의 휴장일이 달라, 일본 휴장일에는 최대 5일 이전의 일본 금리를 사용합니다.',
    'cftc_legacy': '비상업 투기 포지션(롱−숏)입니다. TFF의 레버리지 펀드와는 다른 분류라 값이 다릅니다. 기준일은 화요일이며 금요일에 발표됩니다.',
    'cftc_tff': '레버리지 펀드 포지션(롱−숏)입니다. Legacy의 비상업과는 다른 분류라 값이 다릅니다. 기준일은 화요일이며 금요일에 발표됩니다.',
}


def _finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def _clean(items):
    today = datetime.now(timezone.utc).date()
    result = {}
    for day, value in items:
        try:
            if date.fromisoformat(day) > today:
                continue
        except (ValueError, TypeError):
            continue
        result[day] = value if _finite(value) else None
    return [[d, v] for d, v in sorted(result.items())]


def parse_era_date(text):
    """MOF dates look like 'R8.9.17' (Reiwa 8 = 2026), 'H30.1.4' or 'S49.9.24'."""
    text = (text or '').strip()
    try:
        m = re.fullmatch(r'([SHR])(\d+)\.(\d+)\.(\d+)', text)
        if m:
            year = {'S': 1925, 'H': 1988, 'R': 2018}[m.group(1)] + int(m.group(2))
            return date(year, int(m.group(3)), int(m.group(4))).isoformat()
        m = re.fullmatch(r'(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})', text)
        if m:
            return date(*map(int, m.groups())).isoformat()
    except ValueError:
        return None
    return None


def parse_mof_csv(text, tenor):
    labels = {f'{tenor}年', f'{tenor}Y', f'{tenor}y'}
    rows = list(csv.reader(io.StringIO(text)))
    header = next((i for i, r in enumerate(rows) if any(c.strip() in labels for c in r)), None)
    if header is None:
        raise ValueError(f'MOF CSV has no {tenor}-year column')
    col = next(i for i, c in enumerate(rows[header]) if c.strip() in labels)
    points = {}
    for row in rows[header + 1:]:
        if len(row) <= col:
            continue
        day = parse_era_date(row[0])
        if not day:
            continue
        try:
            points[day] = float(row[col])
        except ValueError:
            points[day] = None  # '-' means no yield published for that tenor/date
    return points


def cftc_points(rows, long_keys, short_keys):
    def pick(row, keys):
        for key in keys:
            value = row.get(key)
            if value not in (None, ''):
                try:
                    return float(value)
                except ValueError:
                    pass
        return None
    points = {}
    for row in rows:
        day = str(row.get('report_date_as_yyyy_mm_dd', ''))[:10]
        if not day:
            continue
        long_, short = pick(row, long_keys), pick(row, short_keys)
        points[day] = long_ - short if long_ is not None and short is not None else None
    return points


def _get(url, timeout, params=None):
    from curl_cffi import requests
    from curl_cffi.const import CurlHttpVersion
    response = requests.get(url, params=params, impersonate='chrome', timeout=timeout,
                            http_version=CurlHttpVersion.V1_1)
    response.raise_for_status()
    return response


def fetch_points(sid):
    if sid in MOF_TENORS:
        merged = {}
        for url in MOF_URLS:
            text = _get(url, 15).content.decode('cp932', errors='replace')
            merged.update(parse_mof_csv(text, MOF_TENORS[sid]))
        return _clean(merged.items())
    dataset, long_keys, short_keys = CFTC_SOURCES[sid]
    response = _get(f'https://publicreporting.cftc.gov/resource/{dataset}.json', 25,
                    params={'cftc_contract_market_code': CFTC_YEN_CODE,
                            '$limit': 50000, '$order': 'report_date_as_yyyy_mm_dd'})
    rows = response.json()
    if not isinstance(rows, list) or not rows:
        raise ValueError('CFTC returned no rows')
    points = cftc_points(rows, long_keys, short_keys)
    if not any(_finite(v) for v in points.values()):
        raise ValueError(f'CFTC rows lack the expected columns: {sorted(rows[-1])[:8]}')
    return _clean(points.items())


def aligned_fill(series, fn, max_gap_days=5):
    """Anchor to the first series; the other series use their latest value if at most max_gap_days old."""
    if not series:
        return []
    others = [[(date.fromisoformat(d), v) for d, v in points if _finite(v)] for points in series[1:]]
    position = [0] * len(others)
    result = []
    for day_text, first in series[0]:
        day = date.fromisoformat(day_text)
        values = [first]
        for k, valid in enumerate(others):
            while position[k] + 1 < len(valid) and valid[position[k] + 1][0] <= day:
                position[k] += 1
            if valid and valid[position[k]][0] <= day and (day - valid[position[k]][0]).days <= max_gap_days:
                values.append(valid[position[k]][1])
            else:
                values.append(None)
        result.append([day_text, fn(*values) if all(_finite(v) for v in values) else None])
    return result


def calculate(raw, aligned, calendar, transform):
    s = lambda sid: raw.get(sid, {}).get('points', [])
    net = lambda sid: transform(calendar(s(sid), weekly=True), lambda v: v / 1000)
    return {
        'jgb2': s('JGB2'), 'jgb10': s('JGB10'),
        'jp_curve': aligned([s('JGB10'), s('JGB2')], lambda a, b: a - b),
        'us_jp_2y': aligned_fill([s('DGS2'), s('JGB2')], lambda a, b: a - b),
        'us_jp_10y': aligned_fill([s('DGS10'), s('JGB10')], lambda a, b: a - b),
        'cftc_legacy': net('CFTC_JPY_LEGACY'), 'cftc_tff': net('CFTC_JPY_TFF'),
    }
