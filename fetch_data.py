"""Refresh dashboard observations. No API keys, interpolation, or forward filling.

Run: python fetch_data.py. --offline recomputes from the local observation cache.
Dates are observation dates, NOT historical release/vintage dates.
"""
from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import logging
import math
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
import japan_sources as jp
import estat_sources as es
import shunto_source as sh
import oecd_cli_source as cli

ROOT = Path(__file__).resolve().parent
FREQUENCIES = {
    # --- existing FRED / Yahoo sources ---
    'PAYEMS': 'monthly', 'UNRATE': 'monthly', 'PCEPILFE': 'monthly',
    'CPILFESL': 'monthly', 'ICSA': 'weekly', 'WALCL': 'weekly',
    'WTREGEN': 'weekly', 'WRESBAL': 'weekly', 'RRPONTSYD': 'daily',
    'M2SL': 'monthly', 'SOFR': 'daily', 'IORB': 'daily',
    'BAMLH0A0HYM2': 'daily', 'VIXCLS': 'daily', 'DFII10': 'daily',
    'DGS10': 'daily', 'DGS2': 'daily', '^GSPC': 'daily',
    'RSP': 'daily', 'SPY': 'daily',
    # --- phase 1: US (FRED) ---
    'RSAFS': 'monthly', 'DGORDER': 'monthly', 'CPIAUCSL': 'monthly',
    'PCEPI': 'monthly', 'PPIFES': 'monthly',
    'NFCI': 'weekly', 'STLFSI4': 'weekly',
    'DFEDTARL': 'daily', 'DFEDTARU': 'daily',
    # --- phase 1: FX, dollar index, commodity futures (Yahoo) ---
    'JPY=X': 'daily', 'KRW=X': 'daily', 'DX-Y.NYB': 'daily',
    'GC=F': 'daily', 'SI=F': 'daily', 'HG=F': 'daily',
    'CL=F': 'daily', 'BZ=F': 'daily',
    # --- cycle/bubble-fingerprint additions ---
    'BOGZ1FL883164113Q': 'quarterly', 'GDP': 'quarterly',
    'TDSP': 'quarterly', 'DRTSCILM': 'quarterly',
}

CLI_SOURCES = {
    'OECD_CLI_US': 'us',
    'OECD_CLI_JP': 'jp',
}

FREQUENCIES.update({
    sid: 'monthly'
    for sid in CLI_SOURCES
})

MAX_AGE = {'daily': 7, 'weekly': 18, 'monthly': 75, 'quarterly': 140}
YAHOO = {'^GSPC', 'RSP', 'SPY', 'JPY=X', 'KRW=X', 'DX-Y.NYB',
         'GC=F', 'SI=F', 'HG=F', 'CL=F', 'BZ=F'}
# id, section, title, unit, dependencies, delta lag, comparison label
SPECS = [
    # First 16 keep their original positions (tests index metrics by position); new metrics are appended.
    ('jobs', 'economy', '비농업 고용 증가', '천 명', ['PAYEMS'], 3, '직전 3개월 평균 대비'),
    ('unemployment', 'economy', '실업률', '%', ['UNRATE'], 3, '3개월 전 대비'),
    ('pce', 'economy', '근원 PCE 상승률', '%', ['PCEPILFE'], 3, '3개월 전 대비'),
    ('cpi', 'economy', '근원 CPI 상승률', '%', ['CPILFESL'], 3, '3개월 전 대비'),
    ('claims', 'economy', '신규 실업수당 청구', '천 건', ['ICSA'], 4, '4주 전 대비'),
    ('netliq', 'conditions', '연준 순유동성 참고치', '십억 달러', ['WALCL', 'WTREGEN', 'RRPONTSYD'], 4, '4주 전 대비'),
    ('reserves', 'conditions', '은행 지준금', '십억 달러', ['WRESBAL'], 4, '4주 전 대비'),
    ('tga', 'conditions', '재무부 일반계좌(TGA) 잔고', '십억 달러', ['WTREGEN'], 4, '4주 전 대비'),
    ('m2', 'conditions', 'M2 증가율', '%', ['M2SL'], 3, '3개월 전 대비'),
    ('repo', 'conditions', 'SOFR − IORB', 'bp', ['SOFR', 'IORB'], 20, '20관측일 전 대비'),
    ('credit', 'conditions', '하이일드 신용 스프레드', 'bp', ['BAMLH0A0HYM2'], 20, '20관측일 전 대비'),
    ('trend', 'market', 'S&P 500 · 200일선 이격', '%', ['^GSPC'], 20, '20거래일 전 대비'),
    ('vix', 'market', 'VIX', '지수', ['VIXCLS'], 20, '20관측일 전 대비'),
    ('participation', 'market', '동일가중 상대강도', '%', ['RSP', 'SPY'], 20, '20거래일 전 대비'),
    ('real', 'market', '미국 10년 실질금리', '%', ['DFII10'], 20, '20관측일 전 대비'),
    ('curve', 'market', '미국 10년 − 2년 금리', '%p', ['DGS10', 'DGS2'], 20, '20관측일 전 대비'),
    ('pce_headline', 'economy', '헤드라인 PCE 상승률', '%', ['PCEPI'], 3, '3개월 전 대비'),
    ('cpi_headline', 'economy', '헤드라인 CPI 상승률', '%', ['CPIAUCSL'], 3, '3개월 전 대비'),
    ('ppi_core', 'economy', '근원 PPI 상승률', '%', ['PPIFES'], 3, '3개월 전 대비'),
    ('retail', 'economy', '소매판매 증가율', '%', ['RSAFS'], 3, '3개월 전 대비'),
    ('durable', 'economy', '내구재 수주 증가율', '%', ['DGORDER'], 3, '3개월 전 대비'),
    ('nfci', 'conditions', '시카고 연은 금융여건지수(NFCI)', '지수', ['NFCI'], 4, '4주 전 대비'),
    ('stlfsi', 'conditions', '세인트루이스 연은 금융스트레스지수', '지수', ['STLFSI4'], 4, '4주 전 대비'),
    ('ust2', 'market', '미국 국채 2년 금리', '%', ['DGS2'], 20, '20관측일 전 대비'),
    ('ust10', 'market', '미국 국채 10년 금리', '%', ['DGS10'], 20, '20관측일 전 대비'),
    ('fedtarget', 'market', '연방기금 목표금리', '%', ['DFEDTARU', 'DFEDTARL'], 20, '20관측일 전 대비'),
    ('usdjpy', 'fx', 'USD/JPY', '엔', ['JPY=X'], 20, '20거래일 전 대비'),
    ('usdkrw', 'fx', 'USD/KRW', '원', ['KRW=X'], 20, '20거래일 전 대비'),
    ('dxy', 'fx', '달러인덱스(DXY)', '지수', ['DX-Y.NYB'], 20, '20거래일 전 대비'),
    ('gold', 'fx', '금 선물', '달러/트로이온스', ['GC=F'], 20, '20거래일 전 대비'),
    ('silver', 'fx', '은 선물', '달러/트로이온스', ['SI=F'], 20, '20거래일 전 대비'),
    ('copper', 'fx', '구리 선물', '달러/파운드', ['HG=F'], 20, '20거래일 전 대비'),
    ('wti', 'fx', 'WTI 선물', '달러/배럴', ['CL=F'], 20, '20거래일 전 대비'),
    ('brent', 'fx', 'Brent 선물', '달러/배럴', ['BZ=F'], 20, '20거래일 전 대비'),
    # --- cycle/bubble-fingerprint additions ---
    ('buffett', 'market', '버핏 지표(상장주식 시가총액/GDP)', '%', ['BOGZ1FL883164113Q', 'GDP'], 4, '1년 전 대비'),
    ('household_debt_service', 'economy', '가계부채 상환비율', '%', ['TDSP'], 4, '1년 전 대비'),
    ('bank_lending', 'conditions', '은행 대출태도(순%, 긴축)', '%p', ['DRTSCILM'], 4, '1년 전 대비'),
]
FORMULAS = {
    'jobs': '(PAYEMS[t] − PAYEMS[t−3 calendar months]) / 3; thousands',
    'unemployment': 'UNRATE (%)',
    'pce': '(PCEPILFE[t]/PCEPILFE[t−12 months]−1)×100; secondary: ((PCEPILFE[t]/PCEPILFE[t−3 months])^4−1)×100',
    'pce_headline': '(PCEPI[t]/PCEPI[t−12 months]−1)×100; no interpolation',
    'cpi': '(CPILFESL[t]/CPILFESL[t−12 months]−1)×100; no interpolation',
    'cpi_headline': '(CPIAUCSL[t]/CPIAUCSL[t−12 months]−1)×100; seasonally adjusted index, no interpolation',
    'ppi_core': '(PPIFES[t]/PPIFES[t−12 months]−1)×100; final demand less foods and energy, seasonally adjusted',
    'claims': 'Mean of 4 consecutive calendar weeks of ICSA / 1000',
    'retail': '(RSAFS[t]/RSAFS[t−12 months]−1)×100; advance retail sales, seasonally adjusted, nominal',
    'durable': '(DGORDER[t]/DGORDER[t−12 months]−1)×100; new orders for durable goods, nominal',
    'netliq': 'WALCL/1000 − WTREGEN/1000 − RRPONTSYD; exact same observation date, no forward fill',
    'reserves': 'WRESBAL (millions) / 1000 = billions',
    'tga': 'WTREGEN (millions) / 1000 = billions; Wednesday level',
    'm2': '(M2SL[t]/M2SL[t−12 months]−1)×100',
    'repo': '(SOFR−IORB)×100; exact same observation date; bp',
    'credit': 'BAMLH0A0HYM2 (%) × 100 = bp',
    'nfci': 'FRED NFCI weekly level; 0 = average conditions, positive = tighter than average',
    'stlfsi': 'FRED STLFSI4 weekly level; 0 = average stress, positive = above-average stress',
    'trend': '(Yahoo ^GSPC Close / 200-session mean Close−1)×100',
    'vix': 'FRED VIXCLS closing value',
    'participation': '((RSP Close/SPY Close)/125-common-session mean of ratio−1)×100; auto_adjust=False',
    'real': 'FRED DFII10 (%)',
    'curve': 'DGS10−DGS2; exact same observation date; percentage points',
    'ust2': 'FRED DGS2 (%)',
    'ust10': 'FRED DGS10 (%)',
    'fedtarget': 'FRED DFEDTARL~DFEDTARU (%); target range shown as lower~upper, chart uses the upper bound (secondary line = lower bound)',
    'usdjpy': 'Yahoo JPY=X Close (yen per US dollar); auto_adjust=False',
    'usdkrw': 'Yahoo KRW=X Close (won per US dollar); auto_adjust=False',
    'dxy': 'Yahoo DX-Y.NYB Close (ICE US Dollar Index); auto_adjust=False',
    'gold': 'Yahoo GC=F Close; COMEX futures, continuous front month (not spot)',
    'silver': 'Yahoo SI=F Close; COMEX futures, continuous front month (not spot)',
    'copper': 'Yahoo HG=F Close; COMEX futures, continuous front month (not spot)',
    'wti': 'Yahoo CL=F Close; NYMEX futures, continuous front month (not spot)',
    'brent': 'Yahoo BZ=F Close; ICE futures, continuous front month (not spot)',
    'buffett': 'BOGZ1FL883164113Q / (GDP × 1000) × 100; 미국 국내 상장주식 시가총액의 분기말 시장가치를 명목 GDP로 나눈 대용 지표',
    'household_debt_service': 'FRED TDSP 분기 수준; 가처분소득 대비 가계부채(모기지+소비자부채) 원리금 상환비율',
    'bank_lending': 'FRED DRTSCILM 분기 수준; SLOOS 설문 기준 대형·중견기업 상업대출에 대한 은행의 순(긴축−완화) 응답 비율',
}
# Shown under the chart for metrics that have no stored note yet.
FUTURES_NOTE = '선물 근월물 연속 시세이며 현물이 아닙니다. 만기 교체 시점에 가격이 불연속으로 움직일 수 있습니다.'
FX_NOTE = 'Yahoo Finance 시장 종가 기준입니다. FRED 고시환율(뉴욕 정오)과 값이 다를 수 있습니다.'
NOTES = {
    'ppi_core': '식품·에너지를 제외한 최종수요 생산자물가(계절조정)의 전년 대비 상승률입니다.',
    'durable': '항공기 등 대형 수주의 영향으로 월별 변동이 큽니다.',
    'usdjpy': FX_NOTE, 'usdkrw': FX_NOTE,
    'dxy': 'Yahoo Finance의 ICE 달러인덱스 종가입니다.',
    'gold': FUTURES_NOTE, 'silver': FUTURES_NOTE, 'copper': FUTURES_NOTE,
    'wti': FUTURES_NOTE, 'brent': FUTURES_NOTE,
    'buffett': '워런 버핏이 언급한 시가총액/GDP 비율입니다. 절대 임계값(예: 100%)보다 자체 역사 대비 상대적 위치(퍼센타일)로 해석하는 것이 안전합니다. 분기 지표라 갱신이 느립니다.',
    'household_debt_service': '가계가 가처분소득 중 부채 원리금 상환에 쓰는 비율입니다. 모기지·소비자부채를 합산한 값입니다.',
    'bank_lending': '연준 SLOOS 설문 기준입니다. 양수(+)는 순 긴축, 음수(-)는 순 완화를 의미하며, 2001년·2008-09년 침체 전 뚜렷한 긴축이 관측된 바 있습니다.',
}

FREQUENCIES.update(jp.FREQUENCIES)
SPECS.extend(jp.SPECS)
FORMULAS.update(jp.FORMULAS)
NOTES.update(jp.NOTES)
FREQUENCIES.update(es.FREQUENCIES)
SPECS.extend(es.SPECS)
FORMULAS.update(es.FORMULAS)
NOTES.update(es.NOTES)
FREQUENCIES.update(sh.FREQUENCIES)
SPECS.extend(sh.SPECS)
FORMULAS.update(sh.FORMULAS)
NOTES.update(sh.NOTES)
MAX_AGE.setdefault('annual', 430)



def utcnow():
    return datetime.now(timezone.utc).isoformat()


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def clean(rows, today=None):
    """Preserve explicit gaps and all available history; reject future dates."""
    today = today or datetime.now(timezone.utc).date()
    result = {}
    for day, value in rows:
        try:
            day = str(day)[:10]
            if date.fromisoformat(day) > today:
                continue
        except (ValueError, TypeError):
            continue
        try:
            value = float(value)
            value = value if math.isfinite(value) else None
        except (ValueError, TypeError):
            value = None
        result[day] = value
    return [[d, v] for d, v in sorted(result.items())]


def fetch_series(sid):
    if sid in YAHOO:
        import yfinance as yf
        frame = yf.download(sid, period='max', interval='1d', auto_adjust=False,
                            back_adjust=False, repair=False, keepna=True,
                            progress=False, threads=False, timeout=30,
                            multi_level_index=False)
        if frame is None or frame.empty:
            raise ValueError('Yahoo returned no prices')
        # US equities may use today's close after 16:00 New York time.
        # FX and futures retain the previous-day cutoff.
        now_ny = datetime.now(ZoneInfo('America/New_York'))
        completed_day = (now_ny.date()
                         if sid in {'^GSPC', 'SPY', 'RSP'} and now_ny.hour >= 16
                         else now_ny.date() - timedelta(days=1))
        points = clean(((d.strftime('%Y-%m-%d'), v) for d, v in frame['Close'].items()),
                       today=completed_day)
    elif sid in CLI_SOURCES:
        points = cli.fetch_points(CLI_SOURCES[sid])
    elif sid in es.SOURCES:
        points = es.fetch_points(sid)
    elif sid in jp.SOURCES:
        points = jp.fetch_points(sid)
    elif sid in sh.SOURCES:
        points = sh.fetch_points(sid)
    else:
        url = f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}'
        # Bound connection setup separately and avoid unusable IPv6 routes on
        # hosted runners. curl also supplies a hard total request deadline.
        # HTTP/2 is forced down to HTTP/1.1 because hosted runners intermittently
        # see "HTTP/2 stream ... INTERNAL_ERROR (err 2)" against fred.stlouisfed.org;
        # HTTP/1.1 avoids that failure mode entirely.
        from curl_cffi import requests
        from curl_cffi.const import CurlHttpVersion
        response = requests.get(
            url, impersonate='chrome', timeout=12,
            http_version=CurlHttpVersion.V1_1,
        )
        response.raise_for_status()
        rows = list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))
        if not rows or len(rows[0]) != 2 or rows[0][1] != sid:
            raise ValueError('Unexpected FRED CSV header')
        points = clean(row for row in rows[1:] if len(row) == 2)
    if not any(finite(v) for _, v in points):
        raise ValueError('No finite observations')
    return {'points': points, 'retrieved': utcnow(), 'error': None}


def fetch_retry(sid):
    for attempt in range(3):
        try:
            return sid, fetch_series(sid), None
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
            if attempt < 2:
                time.sleep(2 ** attempt)
    return sid, None, error


def fetch_bounded(sid, timeout=60):
    """A separate process makes the entire provider download deadline enforceable."""
    logging.info('%s: starting download (maximum %ss)', sid, timeout)
    try:
        result = subprocess.run(
            [sys.executable, '-u', str(Path(__file__).resolve()), '--source', sid],
            capture_output=True, text=True, encoding='utf-8', timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        if result.returncode:
            return sid, None, result.stderr[-1000:] or 'Source worker failed'
        return tuple(json.loads(result.stdout))
    except subprocess.TimeoutExpired:
        return sid, None, f'Download exceeded {timeout}s deadline'
    except (OSError, ValueError) as exc:
        return sid, None, str(exc)


def download_all():
    # Process isolation also prevents Yahoo shared-state conflicts.
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_bounded, sid) for sid in FREQUENCIES]
        for future in as_completed(futures):
            yield future.result()


def calendar(points, weekly=False):
    """Insert null calendar slots, never invented values."""
    if not points:
        return []
    mapped = {d if weekly else d[:7] + '-01': v for d, v in points}
    current, end = date.fromisoformat(min(mapped)), date.fromisoformat(max(mapped))
    result = []
    while current <= end:
        result.append([current.isoformat(), mapped.get(current.isoformat())])
        current = (current + timedelta(days=7) if weekly else
                   date(current.year + (current.month == 12), current.month % 12 + 1, 1))
    return result


def quarterly_match(daily_points, anchor_dates):
    """For each anchor date (e.g. GDP quarter dates), return the most recent
    daily observation on or before that date. No forward-filling beyond that:
    an anchor date earlier than any daily observation stays null.
    Assumes both daily_points and anchor_dates are sorted ascending."""
    result = []
    i, last_val = 0, None
    for anchor in anchor_dates:
        while i < len(daily_points) and daily_points[i][0] <= anchor:
            last_val = daily_points[i][1]
            i += 1
        result.append([anchor, last_val])
    return result


def transform(points, fn):
    return [[d, fn(v) if finite(v) else None] for d, v in points]


def lagged(points, lag, fn):
    return [[d, fn(v, points[i-lag][1]) if i >= lag and finite(v)
             and finite(points[i-lag][1]) else None] for i, (d, v) in enumerate(points)]


def average(points, count):
    output = []
    for i, (d, _) in enumerate(points):
        values = [v for _, v in points[max(0, i-count+1):i+1]]
        output.append([d, sum(values)/count if len(values) == count
                       and all(finite(v) for v in values) else None])
    return output


def aligned(series, fn):
    """Anchor to first series; missing same-date inputs stay null."""
    if not series:
        return []
    maps = [dict(points) for points in series]
    result = []
    for d, _ in series[0]:
        values = [m.get(d) for m in maps]
        result.append([d, fn(*values) if all(finite(v) for v in values) else None])
    return result


def calculate_base(raw):
    s = lambda sid: raw.get(sid, {}).get('points', [])
    m = lambda sid: calendar(s(sid))
    yoy = lambda sid: lagged(m(sid), 12, lambda a, b: (a/b-1)*100 if b > 0 else None)
    ratio = aligned([s('RSP'), s('SPY')], lambda a, b: a/b if b > 0 else None)

    # Keep only common trading dates, retaining explicit nulls on common dates.
    spy_dates = dict(s('SPY'))
    ratio = [p for p in ratio if p[0] in spy_dates]

    # Buffett indicator: match Wilshire 5000 to each GDP quarter date.
    gdp_points = s('GDP')
    return {
        'jobs': transform(lagged(m('PAYEMS'), 3, lambda a, b: a-b), lambda v: v/3),
        'unemployment': m('UNRATE'), 'pce': yoy('PCEPILFE'), 'cpi': yoy('CPILFESL'),
        'pce_headline': yoy('PCEPI'), 'cpi_headline': yoy('CPIAUCSL'),
        'ppi_core': yoy('PPIFES'), 'retail': yoy('RSAFS'), 'durable': yoy('DGORDER'),
        'claims': transform(average(calendar(s('ICSA'), weekly=True), 4), lambda v: v/1000),
        'netliq': aligned([s('WALCL'), s('WTREGEN'), s('RRPONTSYD')], lambda a, t, r: a/1000-t/1000-r),
        'reserves': transform(calendar(s('WRESBAL'), weekly=True), lambda v: v/1000),
        'tga': transform(calendar(s('WTREGEN'), weekly=True), lambda v: v/1000),
        'm2': yoy('M2SL'),
        'repo': aligned([s('SOFR'), s('IORB')], lambda a, b: (a-b)*100),
        'credit': transform(s('BAMLH0A0HYM2'), lambda v: v*100),
        'nfci': calendar(s('NFCI'), weekly=True), 'stlfsi': calendar(s('STLFSI4'), weekly=True),
        'trend': aligned([s('^GSPC'), average(s('^GSPC'), 200)], lambda v, ma: (v/ma-1)*100 if ma > 0 else None),
        'vix': s('VIXCLS'),
        'participation': aligned([ratio, average(ratio, 125)], lambda v, ma: (v/ma-1)*100 if ma > 0 else None),
        'real': s('DFII10'), 'curve': aligned([s('DGS10'), s('DGS2')], lambda a, b: a-b),
        'ust2': s('DGS2'), 'ust10': s('DGS10'),
        'fedtarget': aligned([s('DFEDTARU'), s('DFEDTARL')], lambda u, l: u),
        'fedtarget_low': aligned([s('DFEDTARU'), s('DFEDTARL')], lambda u, l: l),
        'usdjpy': s('JPY=X'), 'usdkrw': s('KRW=X'), 'dxy': s('DX-Y.NYB'),
        'gold': s('GC=F'), 'silver': s('SI=F'), 'copper': s('HG=F'),
        'wti': s('CL=F'), 'brent': s('BZ=F'),
        'pce_secondary': lagged(m('PCEPILFE'), 3, lambda a, b: ((a/b)**4-1)*100 if b > 0 else None),
        'buffett': aligned([s('BOGZ1FL883164113Q'), gdp_points],lambda market_cap, gdp:
        market_cap/(gdp*1000)*100 if g > 0 else None
),
        'household_debt_service': s('TDSP'),
        'bank_lending': s('DRTSCILM'),
    }


def calculate(raw):
    result = calculate_base(raw)
    result.update(jp.calculate(raw, aligned, calendar, transform))
    result.update(sh.calculate(raw))
    result.update(es.calculate(raw, calendar))
    return result


def source_metadata(sid, raw, today):
    entry = raw.get(sid, {})
    points = entry.get('points', [])
    usable = [p for p in points if finite(p[1])]
    observed = usable[-1][0] if usable else None
    frequency = FREQUENCIES[sid]
    age = (today-date.fromisoformat(observed)).days if observed else None
    stale = bool(observed and (age > MAX_AGE[frequency] or points[-1][0] > observed))
    failed = bool(entry.get('error'))
    origin, url = es.ORIGINS.get(sid) or jp.ORIGINS.get(sid) or sh.ORIGINS.get(sid) or (
        ('Yahoo Finance', f'https://finance.yahoo.com/quote/{quote(sid, safe="")}/history/')
        if sid in YAHOO else ('FRED', f'https://fred.stlouisfed.org/series/{sid}'))
    return dict(id=sid, url=url,
                observed=observed, retrieved=entry.get('retrieved'),
                origin=origin, frequency=frequency,
                age=age, maxAge=MAX_AGE[frequency],
                status='missing' if not observed else 'stale' if failed or stale else 'ok',
                fallback=bool(observed and (failed or stale)))


def build_regimes(raw, previous):
    """Build each country's regimes, retaining its prior result on failure."""
    previous_regimes = previous.get('regimes', {})
    regimes = {}
    for sid, country in CLI_SOURCES.items():
        entry = raw.get(sid, {})
        points = entry.get('points', [])
        if entry.get('error') or not points:
            regimes[country] = copy.deepcopy(previous_regimes.get(country, []))
            continue
        regimes[country] = cli.calculate_regimes(points)
    return regimes


def build(raw, previous, generated_at, today=None):
    today = today or datetime.now(timezone.utc).date()
    computed = calculate(raw)
    old_metrics = {m['id']: m for m in previous.get('metrics', [])}
    metrics = []
    for mid, section, title, unit, deps, lag, period in SPECS:
        old = old_metrics.get(mid, {})
        sources = [source_metadata(sid, raw, today) for sid in deps]
        points = copy.deepcopy(computed[mid])
        usable = [p for p in points if finite(p[1])]
        # Preserve a whole previously calculated metric if a dependency download fails.
        # This avoids combining revised fresh inputs with an unavailable source's cache.
        dependency_failed = any(raw.get(sid, {}).get('error') for sid in deps)
        use_old = finite(old.get('value')) and (dependency_failed or not usable or
                  (old.get('date') and old['date'] > usable[-1][0]))
        if use_old:
            metric = copy.deepcopy(old)
            # Keep provenance of the values actually displayed, not today's unused inputs.
            held_sources = copy.deepcopy(old.get('sources', sources))
            for source in held_sources:
                source['age'] = ((today-date.fromisoformat(source['observed'])).days
                                 if source.get('observed') else None)
                source.update(status='stale', fallback=True)
            metric.update(status='stale', sources=held_sources)
            metrics.append(metric)
            continue
        if usable:
            points = [p for p in points if p[0] >= usable[0][0]]
            last = usable[-1]
            index = next(i for i, p in enumerate(points) if p[0] == last[0])
            older = points[index-lag][1] if index >= lag else None
            delta = last[1]-older if finite(older) else None
        else:
            last, delta = [None, None], None
        missing_tail = bool(usable and points[-1][0] > last[0])
        # Joint series may lag even when each individual dependency is current.
        joint_age = (today-date.fromisoformat(last[0])).days if last[0] else None
        joint_limit = max(MAX_AGE[FREQUENCIES[sid]] for sid in deps)
        stale = missing_tail or (joint_age is not None and joint_age > joint_limit)
        if stale:
            for source in sources:
                if source['status'] == 'ok':
                    source.update(status='stale', fallback=True)
        secondary = ({'label': '3개월 연율', 'points': computed['pce_secondary']}
                     if mid == 'pce' else
                     {'label': '하단', 'primaryLabel': '상단', 'points': computed['fedtarget_low']}
                     if mid == 'fedtarget' else None)
        text = None
        if mid == 'fedtarget' and usable:
            low = dict(computed['fedtarget_low']).get(last[0])
            text = f'{low:.2f}~{last[1]:.2f}' if finite(low) else None
        metrics.append(dict(id=mid, section=section, title=title, unit=unit,
                            points=points, date=last[0], value=last[1], delta=delta,
                            period=period, note=old.get('note') or NOTES.get(mid, ''),
                            formula=FORMULAS[mid],
                            status='missing' if not usable else 'stale' if stale or
                            any(s['status'] != 'ok' for s in sources) else 'ok',
                            sources=sources, secondary=secondary,
                            **({'text': text} if text else {})))
    return dict(
        generatedAt=generated_at,
        metrics=metrics,
        regimes=build_regimes(raw, previous),
        method=(
            '차트는 관측 기준일과 현재 제공되는 수정자료를 사용합니다. '
            '당시 공개정보를 복원한 백테스트가 아닙니다. '
            'OECD 경기국면도 현재 제공되는 수정 자료로 판정하며 '
            '당시 공개정보를 복원하지 않습니다. '
            '다운로드 시각은 발표 시각과 다릅니다. '
            '결측값은 보간하지 않으며 마지막 유효값의 실제 관측일을 유지합니다.'
        ),
    )


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':'))+'\n', encoding='utf-8')
    os.replace(tmp, path)


def read_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def refresh(output=ROOT/'data.json', cache=ROOT/'cache'/'observations.json', offline=False):
    previous = read_json(output, {})
    raw = read_json(cache, {})
    successes = 0
    if not offline:
        for sid, entry, error in download_all():
            if entry:
                existing = raw.get(sid, {}).get('points', [])
                # Preserve older history when providers limit their downloadable window.
                # New observations (including nulls/revisions) are authoritative within it.
                first, last = entry['points'][0][0], entry['points'][-1][0]
                old_valid = [d for d, v in existing if finite(v)]
                new_valid = [d for d, v in entry['points'] if finite(v)]
                if old_valid and new_valid[-1] < old_valid[-1]:
                    entry['error'] = 'Provider response ends before retained observations'
                merged = {d: v for d, v in existing if d < first or d > last}
                merged.update(dict(entry['points']))
                entry['points'] = [[d, v] for d, v in sorted(merged.items())]
                raw[sid] = entry
                successes += 1
                logging.info('%s: downloaded', sid)
            else:
                raw.setdefault(sid, {'points': [], 'retrieved': None})['error'] = error
                logging.warning('%s: download failed; retaining previous observations: %s', sid, error)
        atomic_json(cache, raw)
    generated_at = max((raw[s]['retrieved'] for s in FREQUENCIES
                        if raw.get(s, {}).get('retrieved') and not raw[s].get('error')), default=None)
    if not successes:
        generated_at = previous.get('generatedAt', generated_at)
    result = build(raw, previous, generated_at)
    atomic_json(output, result)
    logging.info('Wrote %s; %s/%s sources downloaded; %s metrics usable', output, successes,
                 len(FREQUENCIES), sum(m['value'] is not None for m in result['metrics']))
    return successes


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--source', choices=list(FREQUENCIES), help=argparse.SUPPRESS)
    parser.add_argument('--output', type=Path, default=ROOT/'data.json')
    parser.add_argument('--cache', type=Path, default=ROOT/'cache'/'observations.json')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    if args.source:
        print(json.dumps(fetch_retry(args.source), ensure_ascii=True, allow_nan=False))
        raise SystemExit(0)
    count = refresh(args.output, args.cache, args.offline)
    if not args.offline and (count < len(FREQUENCIES) or any(
            v.get('error') for v in read_json(args.cache, {}).values())):
        logging.error('One or more sources failed. Fallback status saved; inspect per-source errors.')
        raise SystemExit(2)
