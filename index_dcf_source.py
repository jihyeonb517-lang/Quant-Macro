"""Quarterly multi-index DCF assumptions from manual/index_dcf.csv.

Rates are entered as percentages (6 means 6%). ``cf0`` is the current annual
shareholder cash flow per index unit, so the DCF result and ``index_level`` use
the same index-point unit.
"""
from __future__ import annotations

import csv
import math
from datetime import date, datetime, timezone
from pathlib import Path


CSV_PATH = Path(__file__).resolve().parent / 'manual' / 'index_dcf.csv'
INDEXES = {
    'sp500': 'S&P 500',
    'nasdaq100': 'NASDAQ-100',
    'nikkei225': 'Nikkei 225',
    'topix': 'TOPIX',
}
MEASURES = ('VALUE', 'MARKET', 'GAP', 'CF0', 'STAGE1_GROWTH',
            'STAGE1_YEARS', 'DISCOUNT_RATE', 'TERMINAL_GROWTH')


def source_id(index_id, measure):
    return f'INDEX_DCF_{index_id.upper()}_{measure}'


SOURCE_MAP = {source_id(index_id, measure): (index_id, measure)
              for index_id in INDEXES for measure in MEASURES}
SOURCES = set(SOURCE_MAP)
OPTIONAL_SOURCES = set(SOURCES)
FREQUENCIES = {sid: 'quarterly' for sid in SOURCES}
_ORIGIN = ('Index DCF 분기 가정 (수동 입력)',
           'https://github.com/jihyeonb517-lang/us-macro-dashboard/blob/main/manual/index_dcf.csv')
ORIGINS = {sid: _ORIGIN for sid in SOURCES}

SPECS = []
FORMULAS = {}
NOTES = {}
SECONDARY = {}
_COMMON_NOTE = ('분기마다 manual/index_dcf.csv에 직접 입력한 가정으로 계산합니다. '
                '예측치가 아니라 입력 가정에 민감한 시나리오 값입니다.')

for _index_id, _label in INDEXES.items():
    _prefix = f'index_dcf_{_index_id}'
    SPECS.extend([
        (_prefix, 'market', f'{_label} DCF 적정 지수', '지수',
         [source_id(_index_id, 'VALUE'), source_id(_index_id, 'MARKET')], 1, '직전 분기 대비'),
        (f'{_prefix}_gap', 'market', f'{_label} DCF 괴리율', '%',
         [source_id(_index_id, 'GAP')], 1, '직전 분기 대비'),
        (f'{_prefix}_cf0', 'market', f'{_label} 주주환원 현금흐름(CF₀)', '지수',
         [source_id(_index_id, 'CF0')], 1, '직전 분기 대비'),
        (f'{_prefix}_stage1_growth', 'market', f'{_label} 1단계 성장률(g₁)', '%',
         [source_id(_index_id, 'STAGE1_GROWTH')], 1, '직전 분기 대비'),
        (f'{_prefix}_stage1_years', 'market', f'{_label} 1단계 성장기간(N)', '년',
         [source_id(_index_id, 'STAGE1_YEARS')], 1, '직전 분기 대비'),
        (f'{_prefix}_discount_rate', 'market', f'{_label} 할인율(r)', '%',
         [source_id(_index_id, 'DISCOUNT_RATE')], 1, '직전 분기 대비'),
        (f'{_prefix}_terminal_growth', 'market', f'{_label} 영구성장률(g)', '%',
         [source_id(_index_id, 'TERMINAL_GROWTH')], 1, '직전 분기 대비'),
    ])
    SECONDARY[_prefix] = f'{_prefix}_market'
    FORMULAS.update({
        _prefix: ('Σ[t=1..N] CF₀×(1+g₁)^t/(1+r)^t + '
                  '[CF_N×(1+g)/(r−g)]/(1+r)^N; r=무위험금리+ERP'),
        f'{_prefix}_gap': '(DCF 적정 지수/입력한 실제 지수−1)×100',
        f'{_prefix}_cf0': 'manual/index_dcf.csv의 cf0; 현재 연환산 주주환원 현금흐름(지수 포인트)',
        f'{_prefix}_stage1_growth': 'manual/index_dcf.csv의 stage1_growth_pct',
        f'{_prefix}_stage1_years': 'manual/index_dcf.csv의 stage1_years',
        f'{_prefix}_discount_rate': 'risk_free_rate_pct+erp_pct',
        f'{_prefix}_terminal_growth': 'manual/index_dcf.csv의 terminal_growth_pct',
    })
    NOTES.update({
        _prefix: (_COMMON_NOTE + ' CF₀는 실제 지수와 같은 단위의 연환산 주주환원 현금흐름이어야 합니다.'),
        f'{_prefix}_gap': (_COMMON_NOTE + ' 양수는 적정 지수가 실제 지수보다 높다는 뜻입니다.'),
        f'{_prefix}_cf0': _COMMON_NOTE,
        f'{_prefix}_stage1_growth': _COMMON_NOTE,
        f'{_prefix}_stage1_years': _COMMON_NOTE,
        f'{_prefix}_discount_rate': (_COMMON_NOTE + ' 할인율은 무위험금리와 ERP의 합입니다.'),
        f'{_prefix}_terminal_growth': (_COMMON_NOTE + ' 영구성장률은 할인율보다 낮아야 합니다.'),
    })

_REQUIRED = ('date', 'index_id', 'index_level', 'cf0', 'stage1_growth_pct',
             'stage1_years', 'terminal_growth_pct', 'risk_free_rate_pct', 'erp_pct')


def dcf_value(cf0, stage1_growth_pct, stage1_years,
              terminal_growth_pct, risk_free_rate_pct, erp_pct):
    values = (cf0, stage1_growth_pct, terminal_growth_pct,
              risk_free_rate_pct, erp_pct)
    if not all(math.isfinite(value) for value in values):
        raise ValueError('DCF inputs must be finite numbers')
    if cf0 <= 0:
        raise ValueError('cf0 must be positive')
    if isinstance(stage1_years, bool) or int(stage1_years) != stage1_years:
        raise ValueError('stage1_years must be a whole number')
    years = int(stage1_years)
    if not 1 <= years <= 30:
        raise ValueError('stage1_years must be between 1 and 30')
    growth = stage1_growth_pct / 100
    terminal_growth = terminal_growth_pct / 100
    discount = (risk_free_rate_pct + erp_pct) / 100
    if growth <= -1:
        raise ValueError('stage1_growth_pct must be greater than -100')
    if discount <= terminal_growth:
        raise ValueError('risk_free_rate_pct + erp_pct must exceed terminal_growth_pct')
    if discount <= -1:
        raise ValueError('discount rate must be greater than -100%')
    explicit = sum(cf0 * (1 + growth) ** year / (1 + discount) ** year
                   for year in range(1, years + 1))
    cash_flow_n = cf0 * (1 + growth) ** years
    terminal = cash_flow_n * (1 + terminal_growth) / (discount - terminal_growth)
    return explicit + terminal / (1 + discount) ** years


def _rows(today=None):
    if not CSV_PATH.exists():
        raise FileNotFoundError(f'{CSV_PATH} not found')
    today = today or datetime.now(timezone.utc).date()
    output = {}
    with CSV_PATH.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or any(name not in reader.fieldnames for name in _REQUIRED):
            raise ValueError('index_dcf.csv has an invalid header')
        for line_number, row in enumerate(reader, start=2):
            if not any(str(value or '').strip() for value in row.values()):
                continue
            try:
                day = date.fromisoformat(str(row['date']).strip())
                index_id = str(row['index_id']).strip().lower()
                if index_id not in INDEXES:
                    raise ValueError('index_id must be sp500, nasdaq100, nikkei225, or topix')
                index_level = float(row['index_level'])
                cf0 = float(row['cf0'])
                growth = float(row['stage1_growth_pct'])
                years_raw = float(row['stage1_years'])
                terminal_growth = float(row['terminal_growth_pct'])
                risk_free = float(row['risk_free_rate_pct'])
                erp = float(row['erp_pct'])
                if not math.isfinite(index_level) or index_level <= 0:
                    raise ValueError('index_level must be positive')
                value = dcf_value(cf0, growth, years_raw, terminal_growth, risk_free, erp)
            except (TypeError, ValueError) as exc:
                raise ValueError(f'index_dcf.csv line {line_number}: {exc}') from exc
            if day > today:
                continue
            output[(index_id, day.isoformat())] = {
                'VALUE': value, 'MARKET': index_level,
                'GAP': (value/index_level - 1) * 100, 'CF0': cf0,
                'STAGE1_GROWTH': growth, 'STAGE1_YEARS': int(years_raw),
                'DISCOUNT_RATE': risk_free + erp,
                'TERMINAL_GROWTH': terminal_growth,
            }
    return output


def fetch_points(sid):
    if sid not in SOURCE_MAP:
        raise KeyError(sid)
    index_id, measure = SOURCE_MAP[sid]
    return [[day, values[measure]]
            for (row_index, day), values in sorted(_rows().items())
            if row_index == index_id]


def calculate(raw):
    result = {}
    for index_id in INDEXES:
        prefix = f'index_dcf_{index_id}'
        result[prefix] = raw.get(source_id(index_id, 'VALUE'), {}).get('points', [])
        result[f'{prefix}_market'] = raw.get(source_id(index_id, 'MARKET'), {}).get('points', [])
        result[f'{prefix}_gap'] = raw.get(source_id(index_id, 'GAP'), {}).get('points', [])
        result[f'{prefix}_cf0'] = raw.get(source_id(index_id, 'CF0'), {}).get('points', [])
        result[f'{prefix}_stage1_growth'] = raw.get(source_id(index_id, 'STAGE1_GROWTH'), {}).get('points', [])
        result[f'{prefix}_stage1_years'] = raw.get(source_id(index_id, 'STAGE1_YEARS'), {}).get('points', [])
        result[f'{prefix}_discount_rate'] = raw.get(source_id(index_id, 'DISCOUNT_RATE'), {}).get('points', [])
        result[f'{prefix}_terminal_growth'] = raw.get(source_id(index_id, 'TERMINAL_GROWTH'), {}).get('points', [])
    return result
