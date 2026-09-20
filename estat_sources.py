"""Pinned, metadata-validated e-Stat monthly series. Credentials stay in env."""
import os
import re
import math
import unicodedata
from io import BytesIO
from urllib.request import urlopen
from datetime import date

API = 'https://api.e-stat.go.jp/rest/3.0/app/json/getStatsData'
FILE_URL = 'https://www.e-stat.go.jp/stat-search/file-download?fileKind={}&statInfId={}'
FILES = {
    'jp_wage_sched': FILE_URL.format(4, '000032189732'),
    'jp_real_wage': FILE_URL.format(4, '000032189738'),
    'jp_retail': FILE_URL.format(0, '000031387992'),
    'jp_hh_consumption': 'https://www.stat.go.jp/data/kakei/sokuhou/tsuki/zuhyou/fies_t1.xlsx',
}
CONFIG = {
    'jp_wage_sched': ('소정내급여 상승률', '', {}, False),
    'jp_real_wage': ('실질임금 상승률', '', {}, False),
    'jp_retail': ('소매판매 증가율', '', {}, False),
    'jp_hh_consumption': ('가계 실질 소비지출 증가율', '', {}, False),
    'jp_cpi_all': ('전국 CPI 종합', '0003427113', {'tab': '3', 'cat01': '0001', 'area': '00000'}, False),
    'jp_cpi_exfresh': ('전국 CPI 신선식품 제외', '0003427113', {'tab': '3', 'cat01': '0161', 'area': '00000'}, False),
    'jp_cpi_svc': ('전국 CPI 서비스', '0003427113', {'tab': '3', 'cat01': '0220', 'area': '00000'}, False),
    'tokyo_cpi_all': ('도쿄 CPI 종합', '0003427113', {'tab': '3', 'cat01': '0001', 'area': '13A01'}, False),
    'tokyo_cpi_exfresh': ('도쿄 CPI 신선식품 제외', '0003427113', {'tab': '3', 'cat01': '0161', 'area': '13A01'}, False),
    'jp_ip': ('광공업생산 증가율', '0004052181', {'cat01': '0001000'}, True),
    'jp_inventory': ('광공업 재고 증가율', '0004052183', {'cat01': '0001000'}, True),
}
SOURCES = {'ESTAT_' + key: key for key in CONFIG}
FREQUENCIES = {sid: 'monthly' for sid in SOURCES}
SPECS = [(key, 'japan', item[0], '%', ['ESTAT_' + key], 3, '3개월 전 대비')
         for key, item in CONFIG.items()]
ORIGINS = {sid: ('e-Stat / Japanese government statistics',
                FILES.get(key) or 'https://www.e-stat.go.jp/dbview?sid=' + CONFIG[key][1])
           for sid, key in SOURCES.items()}
FORMULAS = {key: ('(original index[t]/index[t−12 calendar months]−1)×100'
                  if item[3] else 'Official published year-on-year percentage change')
            for key, item in CONFIG.items()}
NOTES = {key: 'e-Stat 제공 자료를 사용했습니다. 월별 전년 동월 대비이며 결측값은 보간하지 않습니다.'
         for key in CONFIG}
NOTES.update({
 'jp_wage_sched': '후생노동성 매월근로통계: 사업소 5인 이상, 취업형태계·조사산업계의 소정내급여 전년비입니다.',
 'jp_real_wage': '후생노동성: 사업소 5인 이상·조사산업계의 현금급여 총액 실질 전년비입니다. 자가주택 귀속임대료 제외 CPI로 실질화합니다.',
 'jp_hh_consumption': '총무성 가계조사: 2인 이상 가구의 실질 소비지출 전년 동월비입니다. 최신 보고서의 약 25개월을 제공하며 전체 가구나 GDP 소비와 범위가 다릅니다.',
 'jp_retail': '경제산업성 소매업 전체의 명목 판매액 전년 동월비입니다. 공식 전년 동월=100 비율에서 100을 뺍니다.',
})


def numeric(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def file_points(key):
    import pandas as pd
    with urlopen(FILES[key], timeout=25) as response:
        book = pd.ExcelFile(BytesIO(response.read()))
    sheet = 'TL' if key in ('jp_wage_sched', 'jp_real_wage') else (
        '前年比（change）(月次M)' if key == 'jp_retail' else '表１')
    rows = book.parse(sheet, header=None).fillna('').values.tolist()
    points = []
    if key in ('jp_wage_sched', 'jp_real_wage'):
        header = ' '.join(str(x) for row in rows[:4] for x in row)
        if not all(x in header for x in ('5 or more', 'Industries covered', 'Total')):
            raise ValueError('Unexpected wage population')
        active = False
        for row in rows:
            if '前年比' in str(row[0]):
                active = True
            year = numeric(row[0])
            if active and year and 1950 <= year <= date.today().year:
                for month in range(1, 13):
                    value = numeric(row[7 + month])
                    if value is not None:
                        points.append([f'{int(year):04d}-{month:02d}-01', value])
    elif key == 'jp_retail':
        columns = [i for i, label in enumerate(rows[5]) if label == '小売業計']
        if len(columns) != 1:
            raise ValueError('Retail total column changed')
        for row in rows:
            day = month_date(str(row[1]))
            value = numeric(row[columns[0]])
            if day:
                points.append([day, value - 100 if value is not None else None])
    else:
        if rows[7][8] != '消費支出':
            raise ValueError('Household consumption column changed')
        active, year = False, None
        for row in rows:
            if 'Change over the year in % (Real)' in str(row[7]):
                active = True
            if not active:
                continue
            year_match = re.fullmatch(r'(\d{4})年', str(row[3]))
            if year_match:
                year = int(year_match[1])
            month = numeric(unicodedata.normalize('NFKC', str(row[4])).strip())
            if year and month and 1 <= month <= 12:
                points.append([f'{year:04d}-{int(month):02d}-01', numeric(row[8])])
    if not points:
        raise ValueError('Official file has no matching monthly observations')
    return [p for p in points if p[0] <= date.today().isoformat()]


def many(value):
    return value if isinstance(value, list) else [value] if isinstance(value, dict) else []


def month_date(label):
    match = re.fullmatch(r'(\d{4})年(\d{1,2})月', label) or re.fullmatch(r'(\d{4})(\d{2})', label)
    if not match:
        return None
    try:
        return date(int(match[1]), int(match[2]), 1).isoformat()
    except ValueError:
        return None


def parse(payload, selection):
    root = payload.get('GET_STATS_DATA', {})
    status = root.get('RESULT', {}).get('STATUS')
    if status != 0:
        raise ValueError(f'e-Stat API status {status}')
    data = root['STATISTICAL_DATA']
    classes = {c['@id']: many(c.get('CLASS')) for c in many(data['CLASS_INF']['CLASS_OBJ'])}
    for dimension, code in selection.items():
        if code not in {c['@code'] for c in classes.get(dimension, [])}:
            raise ValueError(f'e-Stat dimension changed: {dimension}')
    dates = {c['@code']: month_date(c['@name']) for c in classes['time']}
    points = {}
    for row in many(data.get('DATA_INF', {}).get('VALUE')):
        if any(row.get('@' + dim) != code for dim, code in selection.items()):
            raise ValueError('e-Stat returned an unrequested series')
        day = dates.get(row.get('@time'))
        if not day or day > date.today().isoformat():
            continue
        if day in points:
            raise ValueError('e-Stat returned multiple observations for one month')
        try:
            value = float(row['$'])
            value = value if math.isfinite(value) else None
        except (ValueError, TypeError):
            value = None
        points[day] = value
    if data.get('RESULT_INF', {}).get('NEXT_KEY'):
        raise ValueError('e-Stat result truncated; narrow the series selection')
    return sorted([day, value] for day, value in points.items())


def fetch_points(sid):
    if SOURCES[sid] in FILES:
        return file_points(SOURCES[sid])
    key = os.environ.get('ESTAT_APP_ID', '').strip()
    if not key:
        raise ValueError('ESTAT_APP_ID is not configured')
    _, table, selection, _ = CONFIG[SOURCES[sid]]
    params = {'appId': key, 'statsDataId': table, 'limit': 100000, 'metaGetFlg': 'Y'}
    params.update({'cd' + dim[0].upper() + dim[1:]: code for dim, code in selection.items()})
    from curl_cffi import requests
    try:
        response = requests.get(API, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        # HTTP exceptions can contain the credential-bearing request URL.
        raise ValueError('e-Stat request failed') from None
    return parse(payload, selection)


def calculate(raw, calendar):
    result = {}
    for sid, key in SOURCES.items():
        points = calendar(raw.get(sid, {}).get('points', []))
        if CONFIG[key][3]:
            values = dict(points)
            converted = []
            for day, value in points:
                previous = values.get(f'{int(day[:4])-1:04d}' + day[4:])
                converted.append([day, (value / previous - 1) * 100
                                  if value is not None and previous is not None and previous > 0 else None])
            points = converted
        result[key] = points
    return result
