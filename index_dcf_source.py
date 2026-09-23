"""Quarterly Index DCF assumptions from manual/index_dcf.csv.

Rates and payout ratios are entered as percentages (6 means 6%). The model
follows the EPS, shareholder-payout, discount-rate, and terminal-value structure
described in the supplied Index DCF guide.
"""
from __future__ import annotations

import csv
import math
from datetime import date, datetime, timezone
from pathlib import Path

CSV_PATH = Path(__file__).resolve().parent / 'manual' / 'index_dcf.csv'
INDEXES = {'sp500': 'S&P 500', 'nasdaq100': 'NASDAQ-100',
           'nikkei225': 'Nikkei 225', 'topix': 'TOPIX'}
MEASURES = (
    'VALUE', 'MARKET', 'GAP', 'EPS', 'STAGE1_GROWTH', 'STAGE1_YEARS',
    'DIVIDEND_PAYOUT', 'BUYBACK_PAYOUT', 'SHAREHOLDER_PAYOUT', 'CF0',
    'RISK_FREE_RATE', 'ERP', 'DISCOUNT_RATE', 'TERMINAL_GROWTH',
    'LONG_RISK_FREE_RATE', 'LONG_ERP', 'LONG_ROE', 'TERMINAL_PAYOUT',
    'TERMINAL_CF', 'EXPLICIT_PV', 'TERMINAL_PV', 'IMPLIED_PE')


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

_METRIC_DEFS = (
    ('', 'DCF 적정 지수', '지수', 'VALUE'),
    ('_gap', 'DCF 고평가율', '%', 'GAP'),
    ('_eps', 'EPS', '지수', 'EPS'),
    ('_stage1_growth', 'EPS 성장률', '%', 'STAGE1_GROWTH'),
    ('_stage1_years', '명시적 성장기간', '년', 'STAGE1_YEARS'),
    ('_dividend_payout', '배당성향', '%', 'DIVIDEND_PAYOUT'),
    ('_buyback_payout', '바이백성향', '%', 'BUYBACK_PAYOUT'),
    ('_shareholder_payout', '주주환원성향', '%', 'SHAREHOLDER_PAYOUT'),
    ('_cf0', '주주환원 현금흐름', '지수', 'CF0'),
    ('_risk_free_rate', '무위험수익률', '%', 'RISK_FREE_RATE'),
    ('_erp', '주식시장 위험 프리미엄(ERP)', '%', 'ERP'),
    ('_discount_rate', '할인율', '%', 'DISCOUNT_RATE'),
    ('_terminal_growth', '영구성장률', '%', 'TERMINAL_GROWTH'),
    ('_long_risk_free_rate', '장기 무위험수익률', '%', 'LONG_RISK_FREE_RATE'),
    ('_long_erp', '장기 ERP', '%', 'LONG_ERP'),
    ('_long_roe', '장기 ROE', '%', 'LONG_ROE'),
    ('_terminal_payout', '종료연도 주주환원성향', '%', 'TERMINAL_PAYOUT'),
    ('_terminal_cf', '종료연도 현금흐름', '지수', 'TERMINAL_CF'),
    ('_explicit_pv', '명시적 기간 현재가치', '지수', 'EXPLICIT_PV'),
    ('_terminal_pv', '종료가치 현재가치', '지수', 'TERMINAL_PV'),
    ('_implied_pe', '내재 PER', '배', 'IMPLIED_PE'))

SPECS, FORMULAS, NOTES, SECONDARY = [], {}, {}, {}
_NOTE = ('분기마다 manual/index_dcf.csv에 직접 입력한 가정으로 계산합니다. '
         '예측치가 아니라 입력 가정에 민감한 시나리오 값입니다.')
for _index_id, _label in INDEXES.items():
    _prefix = f'index_dcf_{_index_id}'
    for _suffix, _title, _unit, _measure in _METRIC_DEFS:
        _metric_id = f'{_prefix}{_suffix}'
        SPECS.append((_metric_id, 'market', f'{_label} {_title}', _unit,
                      [source_id(_index_id, _measure)], 1, '직전 분기 대비'))
        NOTES[_metric_id] = _NOTE
    SECONDARY[_prefix] = f'{_prefix}_market'
    FORMULAS.update({
        _prefix: ('Σ EPS₀×(1+g₁)^t×(배당성향+바이백성향)/(1+r)^t '
                  '+ [EPS_N×(1+g∞)×(1−g∞/장기ROE)/(r∞−g∞)]/(1+r)^N'),
        f'{_prefix}_gap': '(실제 지수/DCF 적정 지수−1)×100; 양수는 고평가',
        f'{_prefix}_eps': 'CSV의 eps',
        f'{_prefix}_stage1_growth': 'CSV의 stage1_growth_pct',
        f'{_prefix}_stage1_years': 'CSV의 stage1_years',
        f'{_prefix}_dividend_payout': 'CSV의 dividend_payout_pct',
        f'{_prefix}_buyback_payout': 'CSV의 buyback_payout_pct',
        f'{_prefix}_shareholder_payout': '배당성향+바이백성향',
        f'{_prefix}_cf0': 'EPS×(배당성향+바이백성향)',
        f'{_prefix}_risk_free_rate': 'CSV의 risk_free_rate_pct',
        f'{_prefix}_erp': 'CSV의 erp_pct',
        f'{_prefix}_discount_rate': '무위험수익률+ERP',
        f'{_prefix}_terminal_growth': 'CSV의 terminal_growth_pct',
        f'{_prefix}_long_risk_free_rate': 'CSV의 long_term_risk_free_rate_pct',
        f'{_prefix}_long_erp': 'CSV의 long_term_erp_pct',
        f'{_prefix}_long_roe': 'CSV의 long_term_roe_pct',
        f'{_prefix}_terminal_payout': '1−영구성장률/장기ROE',
        f'{_prefix}_terminal_cf': '종료연도 EPS×종료연도 주주환원성향',
        f'{_prefix}_explicit_pv': '명시적 기간 현금흐름 현재가치 합계',
        f'{_prefix}_terminal_pv': '종료가치/(1+명시적 기간 할인율)^N',
        f'{_prefix}_implied_pe': 'DCF 적정 지수/EPS'})
    NOTES[f'{_prefix}_gap'] += ' 양수는 실제 지수가 적정 가치보다 높다는 뜻입니다.'

_REQUIRED = (
    'date', 'index_id', 'index_level', 'eps', 'stage1_growth_pct',
    'stage1_years', 'dividend_payout_pct', 'buyback_payout_pct',
    'risk_free_rate_pct', 'erp_pct', 'terminal_growth_pct',
    'long_term_risk_free_rate_pct', 'long_term_erp_pct', 'long_term_roe_pct')


def dcf_breakdown(eps, stage1_growth_pct, stage1_years,
                  dividend_payout_pct, buyback_payout_pct,
                  risk_free_rate_pct, erp_pct, terminal_growth_pct,
                  long_term_risk_free_rate_pct, long_term_erp_pct,
                  long_term_roe_pct):
    values = (eps, stage1_growth_pct, dividend_payout_pct, buyback_payout_pct,
              risk_free_rate_pct, erp_pct, terminal_growth_pct,
              long_term_risk_free_rate_pct, long_term_erp_pct,
              long_term_roe_pct)
    if not all(math.isfinite(value) for value in values):
        raise ValueError('DCF inputs must be finite numbers')
    if eps <= 0:
        raise ValueError('eps must be positive')
    if isinstance(stage1_years, bool) or int(stage1_years) != stage1_years:
        raise ValueError('stage1_years must be a whole number')
    years = int(stage1_years)
    if not 1 <= years <= 30:
        raise ValueError('stage1_years must be between 1 and 30')
    if dividend_payout_pct < 0 or buyback_payout_pct < 0:
        raise ValueError('payout ratios cannot be negative')
    shareholder_payout_pct = dividend_payout_pct + buyback_payout_pct
    if not 0 < shareholder_payout_pct <= 200:
        raise ValueError('dividend payout + buyback payout must be between 0 and 200%')

    growth = stage1_growth_pct / 100
    discount = (risk_free_rate_pct + erp_pct) / 100
    terminal_growth = terminal_growth_pct / 100
    terminal_discount = (long_term_risk_free_rate_pct + long_term_erp_pct) / 100
    long_roe = long_term_roe_pct / 100
    payout = shareholder_payout_pct / 100
    if growth <= -1:
        raise ValueError('stage1_growth_pct must be greater than -100')
    if discount <= -1:
        raise ValueError('risk-free rate + ERP must be greater than -100%')
    if terminal_discount <= terminal_growth:
        raise ValueError('long-term risk-free rate + long-term ERP must exceed terminal growth')
    if long_roe <= terminal_growth:
        raise ValueError('long_term_roe_pct must exceed terminal_growth_pct')

    explicit_pv = sum(eps * (1 + growth) ** year * payout / (1 + discount) ** year
                      for year in range(1, years + 1))
    terminal_eps = eps * (1 + growth) ** years * (1 + terminal_growth)
    terminal_payout = 1 - terminal_growth / long_roe
    terminal_cf = terminal_eps * terminal_payout
    terminal_value = terminal_cf / (terminal_discount - terminal_growth)
    terminal_pv = terminal_value / (1 + discount) ** years
    value = explicit_pv + terminal_pv
    return {
        'VALUE': value, 'EPS': eps, 'STAGE1_GROWTH': stage1_growth_pct,
        'STAGE1_YEARS': years, 'DIVIDEND_PAYOUT': dividend_payout_pct,
        'BUYBACK_PAYOUT': buyback_payout_pct,
        'SHAREHOLDER_PAYOUT': shareholder_payout_pct, 'CF0': eps * payout,
        'RISK_FREE_RATE': risk_free_rate_pct, 'ERP': erp_pct,
        'DISCOUNT_RATE': risk_free_rate_pct + erp_pct,
        'TERMINAL_GROWTH': terminal_growth_pct,
        'LONG_RISK_FREE_RATE': long_term_risk_free_rate_pct,
        'LONG_ERP': long_term_erp_pct, 'LONG_ROE': long_term_roe_pct,
        'TERMINAL_PAYOUT': terminal_payout * 100, 'TERMINAL_CF': terminal_cf,
        'EXPLICIT_PV': explicit_pv, 'TERMINAL_PV': terminal_pv,
        'IMPLIED_PE': value / eps}


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
                if not math.isfinite(index_level) or index_level <= 0:
                    raise ValueError('index_level must be positive')
                values = dcf_breakdown(
                    float(row['eps']), float(row['stage1_growth_pct']),
                    float(row['stage1_years']), float(row['dividend_payout_pct']),
                    float(row['buyback_payout_pct']), float(row['risk_free_rate_pct']),
                    float(row['erp_pct']), float(row['terminal_growth_pct']),
                    float(row['long_term_risk_free_rate_pct']),
                    float(row['long_term_erp_pct']), float(row['long_term_roe_pct']))
            except (TypeError, ValueError) as exc:
                raise ValueError(f'index_dcf.csv line {line_number}: {exc}') from exc
            if day > today:
                continue
            values['MARKET'] = index_level
            values['GAP'] = (index_level / values['VALUE'] - 1) * 100
            output[(index_id, day.isoformat())] = values
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
        result[f'{prefix}_market'] = raw.get(source_id(index_id, 'MARKET'), {}).get('points', [])
        for suffix, _title, _unit, measure in _METRIC_DEFS:
            result[f'{prefix}{suffix}'] = raw.get(source_id(index_id, measure), {}).get('points', [])
    return result
