"""Spring wage offensive (shunto) series read from manual/shunto.csv.

MHLW "民間主要企業春季賃上げ要求・妥結状況" (companies with 1bn+ yen capital, 1,000+ employees, with a union).
One row per year: year,rate  (rate in %, e.g. 5.18). The observation date is July 1 of that year.
"""
from __future__ import annotations

import csv
import math
from datetime import date, datetime, timezone
from pathlib import Path

CSV_PATH = Path(__file__).resolve().parent / 'manual' / 'shunto.csv'
SOURCES = {'SHUNTO_MHLW'}
FREQUENCIES = {'SHUNTO_MHLW': 'annual'}
ORIGINS = {'SHUNTO_MHLW': (
    'MHLW 민간 주요기업 춘계 임금 인상 집계 (수동 입력)',
    'https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/koyou_roudou/roudouseisaku/shuntou/roushi-c1.html')}
# id, section, title, unit, dependencies, delta lag, comparison label
SPECS = [('shunto', 'japan', '춘투 임금 인상률 (후생노동성 주요기업)', '%', ['SHUNTO_MHLW'], 1, '전년 대비')]
FORMULAS = {'shunto': 'MHLW 민간 주요기업 춘계 임금 인상률(정기승급 포함 타결액 ÷ 현행 기준임금 × 100, 조합원 수 가중평균); '
                      '자본금 10억 엔 이상·종업원 1,000명 이상·노조 있는 기업; manual/shunto.csv에서 연 1회 수동 입력; '
                      '기준일은 해당 연도 7월 1일'}
NOTES = {'shunto': '후생노동성이 매년 7~8월에 발표하는 값을 직접 입력합니다. 렌고(연합) 집계와는 대상이 달라 값이 다릅니다.'}


def fetch_points(sid):
    if not CSV_PATH.exists():
        raise FileNotFoundError(f'{CSV_PATH} not found')
    today = datetime.now(timezone.utc).date()
    points = {}
    with CSV_PATH.open(encoding='utf-8-sig', newline='') as handle:
        for row in csv.DictReader(handle):
            try:
                year, rate = int(str(row['year']).strip()), float(str(row['rate']).strip())
            except (KeyError, TypeError, ValueError):
                continue
            day = date(year, 7, 1)
            if day <= today and math.isfinite(rate):
                points[day.isoformat()] = rate
    if not points:
        raise ValueError('shunto.csv has no valid rows (expected header: year,rate)')
    return [[d, v] for d, v in sorted(points.items())]


def calculate(raw):
    return {'shunto': raw.get('SHUNTO_MHLW', {}).get('points', [])}
