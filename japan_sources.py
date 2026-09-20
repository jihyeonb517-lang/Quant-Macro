"""Official Japanese market and Bank of Japan data sources.

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
BOJ_API = 'https://www.stat-search.boj.or.jp/api/v1/getDataCode'
BOJ_PAGE = 'https://www.stat-search.boj.or.jp/'
BOJ_SOURCES = {
    'BOJ_CALL_RATE': ('FM01', ('STRDCLUCON',), 'daily'),
    'BOJ_CGPI': ('PR01', ('PRCG20_2200000000',), 'monthly'),
    'BOJ_TANKAN_MFG_NOW': ('CO', ('TK99F1000601GCQ01000',), 'quarterly'),
    'BOJ_TANKAN_MFG_FCST': ('CO', ('TK99F1000601GCQ11000',), 'quarterly'),
    'BOJ_TANKAN_NONMFG_NOW': ('CO', ('TK99F2000601GCQ01000',), 'quarterly'),
    'BOJ_TANKAN_NONMFG_FCST': ('CO', ('TK99F2000601GCQ11000',), 'quarterly'),
    # Four annual-by-fiscal-year series are combined into one observation per Tankan survey.
    'BOJ_TANKAN_CAPEX': ('CO', (
        'TK99G0000109CFY51000',  # March survey
        'TK99G0000109CFY41000',  # June survey
        'TK99G0000109CFY31000',  # September survey
        'TK99G0000109CFY21000',  # December survey
    ), 'quarterly'),
}
SOURCES = set(MOF_TENORS) | set(CFTC_SOURCES) | set(BOJ_SOURCES)
FREQUENCIES = {
    'JGB2': 'daily', 'JGB10': 'daily',
    'CFTC_JPY_LEGACY': 'weekly', 'CFTC_JPY_TFF': 'weekly',
    **{sid: spec[2] for sid, spec in BOJ_SOURCES.items()},
}
ORIGINS = {
    'JGB2': ('Japan Ministry of Finance', MOF_PAGE),
    'JGB10': ('Japan Ministry of Finance', MOF_PAGE),
    'CFTC_JPY_LEGACY': ('CFTC (Legacy, futures only)', 'https://publicreporting.cftc.gov/'),
    'CFTC_JPY_TFF': ('CFTC (TFF, futures only)', 'https://publicreporting.cftc.gov/'),
    **{sid: ('Bank of Japan Time-Series Data Search', BOJ_PAGE) for sid in BOJ_SOURCES},
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
    ('boj_rate', 'japan', '일본 무담보 익일물 콜금리', '%', ['BOJ_CALL_RATE'], 20, '20관측일 전 대비'),
    ('jp_cgpi', 'japan', '기업물가지수(CGPI) 상승률', '%', ['BOJ_CGPI'], 3, '3개월 전 대비'),
    ('tankan_mfg_now', 'japan', '단칸 대기업 제조업 업황 DI', 'DI', ['BOJ_TANKAN_MFG_NOW'], 1, '직전 분기 대비'),
    ('tankan_mfg_fcst', 'japan', '단칸 대기업 제조업 전망 DI', 'DI', ['BOJ_TANKAN_MFG_FCST'], 1, '직전 분기 대비'),
    ('tankan_nonmfg_now', 'japan', '단칸 대기업 비제조업 업황 DI', 'DI', ['BOJ_TANKAN_NONMFG_NOW'], 1, '직전 분기 대비'),
    ('tankan_nonmfg_fcst', 'japan', '단칸 대기업 비제조업 전망 DI', 'DI', ['BOJ_TANKAN_NONMFG_FCST'], 1, '직전 분기 대비'),
    ('tankan_capex', 'japan', '단칸 대기업 전산업 설비투자 계획', '%', ['BOJ_TANKAN_CAPEX'], 1, '직전 조사 대비'),
]
FORMULAS = {
    'jgb2': 'MOF constant-maturity JGB yield, 2 years (%); published the next business day',
    'jgb10': 'MOF constant-maturity JGB yield, 10 years (%); published the next business day',
    'jp_curve': 'JGB10 − JGB2; exact same observation date; percentage points',
    'us_jp_2y': 'DGS2 − JGB2 (percentage points); anchored to US dates, the latest JGB value up to 5 days old is used when Japan was closed',
    'us_jp_10y': 'DGS10 − JGB10 (percentage points); anchored to US dates, the latest JGB value up to 5 days old is used when Japan was closed',
    'cftc_legacy': '(Non-commercial long − short, all, futures only) / 1000; CFTC Legacy report, Japanese Yen (code 097741); as-of Tuesday',
    'cftc_tff': '(Leveraged funds long − short, futures only) / 1000; CFTC TFF report, Japanese Yen (code 097741); as-of Tuesday',
    'boj_rate': 'BOJ uncollateralized overnight call rate, daily average (%); actual market rate, not the basic loan rate',
    'jp_cgpi': '(BOJ CGPI[t]/CGPI[t−12 calendar months]−1)×100; 2020-base producer price index, all commodities',
    'tankan_mfg_now': 'BOJ Tankan business conditions DI; large manufacturing enterprises; actual result',
    'tankan_mfg_fcst': 'BOJ Tankan business conditions DI; large manufacturing enterprises; forecast',
    'tankan_nonmfg_now': 'BOJ Tankan business conditions DI; large nonmanufacturing enterprises; actual result',
    'tankan_nonmfg_fcst': 'BOJ Tankan business conditions DI; large nonmanufacturing enterprises; forecast',
    'tankan_capex': 'BOJ Tankan fixed investment plan; large enterprises, all industries; software and R&D included, land excluded; year-on-year percent change',
}
NOTES = {
    'us_jp_2y': '미국과 일본의 휴장일이 달라, 일본 휴장일에는 최대 5일 이전의 일본 금리를 사용합니다.',
    'us_jp_10y': '미국과 일본의 휴장일이 달라, 일본 휴장일에는 최대 5일 이전의 일본 금리를 사용합니다.',
    'cftc_legacy': '비상업 투기 포지션(롱−숏)입니다. TFF의 레버리지 펀드와는 다른 분류라 값이 다릅니다. 기준일은 화요일이며 금요일에 발표됩니다.',
    'cftc_tff': '레버리지 펀드 포지션(롱−숏)입니다. Legacy의 비상업과는 다른 분류라 값이 다릅니다. 기준일은 화요일이며 금요일에 발표됩니다.',
    'boj_rate': '일본은행의 정책 유도 대상인 무담보 익일물 콜금리의 실제 시장 평균입니다. 정책결정문의 목표값 자체와는 소폭 다를 수 있습니다.',
    'jp_cgpi': '일본은행 2020년 기준 기업물가지수 중 국내 생산자물가 전체 품목의 전년 대비 상승률입니다.',
    'tankan_mfg_fcst': '각 단칸 조사 시점에 기업이 응답한 다음 조사기의 전망 DI입니다.',
    'tankan_nonmfg_fcst': '각 단칸 조사 시점에 기업이 응답한 다음 조사기의 전망 DI입니다.',
    'tankan_capex': '대기업 전산업의 해당 회계연도 설비투자 계획입니다. 소프트웨어·연구개발을 포함하고 토지는 제외하며 조사 때마다 수정됩니다.',
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


def _boj_date(value, frequency):
    """Convert BOJ survey-date integers to ISO observation dates."""
    text = str(value)
    try:
        if frequency == 'daily' and len(text) == 8:
            return date(int(text[:4]), int(text[4:6]), int(text[6:8])).isoformat()
        if frequency == 'monthly' and len(text) == 6:
            return date(int(text[:4]), int(text[4:6]), 1).isoformat()
        if frequency == 'quarterly' and len(text) == 6:
            month = {1: 3, 2: 6, 3: 9, 4: 12}[int(text[4:6])]
            return date(int(text[:4]), month, 1).isoformat()
    except (ValueError, KeyError):
        pass
    return None


def _boj_rows(payload, expected_codes):
    if payload.get('STATUS') != 200:
        raise ValueError(f"BOJ API error: {payload.get('MESSAGE', 'unknown error')}")
    rows = payload.get('RESULTSET')
    if not isinstance(rows, list):
        raise ValueError('BOJ API returned no result set')
    found = {row.get('SERIES_CODE') for row in rows}
    missing = set(expected_codes) - found
    if missing:
        raise ValueError(f'BOJ API omitted series: {sorted(missing)}')
    return rows


def fetch_boj_points(sid):
    db, codes, frequency = BOJ_SOURCES[sid]
    response = _get(BOJ_API, 30, params={
        'format': 'json', 'lang': 'en', 'db': db, 'code': ','.join(codes),
    })
    rows = _boj_rows(response.json(), codes)

    if sid == 'BOJ_TANKAN_CAPEX':
        survey_month = {
            'TK99G0000109CFY51000': 3,
            'TK99G0000109CFY41000': 6,
            'TK99G0000109CFY31000': 9,
            'TK99G0000109CFY21000': 12,
        }
        points = {}
        for row in rows:
            month = survey_month[row['SERIES_CODE']]
            values = row.get('VALUES', {})
            for fiscal_year, value in zip(values.get('SURVEY_DATES', []),
                                          values.get('VALUES', [])):
                try:
                    day = date(int(fiscal_year), month, 1).isoformat()
                except ValueError:
                    continue
                points[day] = float(value) if value is not None else None
        return _clean(points.items())

    row = rows[0]
    values = row.get('VALUES', {})
    points = []
    for survey_date, value in zip(values.get('SURVEY_DATES', []),
                                  values.get('VALUES', [])):
        day = _boj_date(survey_date, frequency)
        if day:
            points.append((day, float(value) if value is not None else None))
    return _clean(points)


def fetch_points(sid):
    if sid in BOJ_SOURCES:
        return fetch_boj_points(sid)
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
    monthly = lambda sid: calendar(s(sid))
    yoy = lambda sid: _lagged_monthly(monthly(sid), 12)
    return {
        'jgb2': s('JGB2'), 'jgb10': s('JGB10'),
        'jp_curve': aligned([s('JGB10'), s('JGB2')], lambda a, b: a - b),
        'us_jp_2y': aligned_fill([s('DGS2'), s('JGB2')], lambda a, b: a - b),
        'us_jp_10y': aligned_fill([s('DGS10'), s('JGB10')], lambda a, b: a - b),
        'cftc_legacy': net('CFTC_JPY_LEGACY'), 'cftc_tff': net('CFTC_JPY_TFF'),
        'boj_rate': s('BOJ_CALL_RATE'), 'jp_cgpi': yoy('BOJ_CGPI'),
        'tankan_mfg_now': s('BOJ_TANKAN_MFG_NOW'),
        'tankan_mfg_fcst': s('BOJ_TANKAN_MFG_FCST'),
        'tankan_nonmfg_now': s('BOJ_TANKAN_NONMFG_NOW'),
        'tankan_nonmfg_fcst': s('BOJ_TANKAN_NONMFG_FCST'),
        'tankan_capex': s('BOJ_TANKAN_CAPEX'),
    }


def _lagged_monthly(points, months):
    """Year-over-year percentage change without skipping missing calendar months."""
    result = []
    for index, (day, value) in enumerate(points):
        previous = points[index - months][1] if index >= months else None
        result.append([day, (value / previous - 1) * 100
                       if _finite(value) and _finite(previous) and previous > 0 else None])
    return result
