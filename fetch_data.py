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
from threading import Semaphore
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
import japan_sources as jp
import estat_sources as es
import shunto_source as sh
import oecd_cli_source as cli
import korea_sources as kr

DCF_INDEXES = (
    ('sp500', 'S&P 500', '^GSPC', 'DGS10'),
    ('nikkei225', 'Nikkei 225', '^N225', 'JGB10'),
)
ROOT = Path(__file__).resolve().parent
CENSUS_SOURCES = {
    'CENSUS_MARTS_SM': 'SM',
    'CENSUS_MARTS_MPCSM': 'MPCSM',
}
FREQUENCIES = {
    # --- existing FRED / Yahoo sources ---
    'PAYEMS': 'monthly', 'USPRIV': 'monthly', 'USGOVT': 'monthly', 'UNRATE': 'monthly', 'PCEPILFE': 'monthly',
    'CPILFESL': 'monthly', 'ICSA': 'weekly', 'WALCL': 'weekly',
    'WTREGEN': 'weekly', 'WRESBAL': 'weekly', 'RRPONTSYD': 'daily',
    'M2SL': 'monthly', 'SOFR': 'daily', 'IORB': 'daily',
    'BAMLH0A0HYM2': 'daily', 'VIXCLS': 'daily', 'DFII10': 'daily',
    'DGS10': 'daily', 'DGS2': 'daily', '^GSPC': 'daily',
    '^N225': 'daily',
    'RSP': 'daily', 'SPY': 'daily',
    # --- phase 1: US macro sources ---
    'DGORDER': 'monthly', 'CPIAUCSL': 'monthly',
    'CENSUS_MARTS_SM': 'monthly', 'CENSUS_MARTS_MPCSM': 'monthly',
    'PCEPI': 'monthly', 'PPIFES': 'monthly',
    'NFCI': 'weekly', 'STLFSI4': 'weekly',
    'DFEDTARL': 'daily', 'DFEDTARU': 'daily',
    # --- additional US growth, demand, inflation and survey indicators ---
    'GDPC1': 'quarterly', 'GACDFSA066MSFRBPHI': 'monthly',
    'UMCSENT': 'monthly', 'PCECC96': 'quarterly',
    'PCNDGC96': 'quarterly', 'PCDGCC96': 'quarterly', 'PCESVC96': 'quarterly',
    'CORESTICKM159SFRBATL': 'monthly', 'PCETRIM12M159SFRBDAL': 'monthly',
    # --- Japan real GDP (Cabinet Office series distributed by FRED) ---
    'JPNRGDPEXP': 'quarterly',
    # --- Korea official sources (Bank of Korea ECOS / Statistics Korea KOSIS) ---
    # --- phase 1: FX, dollar index, commodity futures (Yahoo) ---
    'JPY=X': 'daily', 'KRW=X': 'daily', 'DX-Y.NYB': 'daily',
    'EURUSD=X': 'daily', 'GBPUSD=X': 'daily', 'EURGBP=X': 'daily', 'CNY=X': 'daily',
    'GBPJPY=X': 'daily', 'EURJPY=X': 'daily', 'GBPKRW=X': 'daily', 'EURKRW=X': 'daily',
    'JPYKRW=X': 'daily',
    'GC=F': 'daily', 'SI=F': 'daily', 'HG=F': 'daily',
    'CL=F': 'daily', 'BZ=F': 'daily',
    # --- cycle/bubble-fingerprint additions ---
    'BOGZ1FL883164113Q': 'quarterly', 'GDP': 'quarterly',
    'TDSP': 'quarterly', 'DRTSCILM': 'quarterly',
}

CLI_SOURCES = {
    'OECD_CLI_US': 'us',
    'OECD_CLI_JP': 'jp',
    'OECD_CLI_KR': 'kr',
}

FREQUENCIES.update({
    sid: 'monthly'
    for sid in CLI_SOURCES
})
FREQUENCIES.update({
    sid: ('daily' if kr.ECOS[sid]['cycle'] == 'D' else
          'quarterly' if kr.ECOS[sid]['cycle'] == 'Q' else 'monthly')
    for sid in kr.ECOS
})
FREQUENCIES.update({sid: 'monthly' for sid in kr.KOSIS})

MAX_AGE = {'daily': 7, 'weekly': 18, 'monthly': 75, 'quarterly': 310}
YAHOO = {'^GSPC', '^N225', 'RSP', 'SPY', 'JPY=X', 'KRW=X', 'DX-Y.NYB', 'EURUSD=X', 'GBPUSD=X', 'EURGBP=X', 'CNY=X',
         'GBPJPY=X', 'EURJPY=X', 'GBPKRW=X', 'EURKRW=X', 'JPYKRW=X',
         'GC=F', 'SI=F', 'HG=F', 'CL=F', 'BZ=F'}
# id, section, title, unit, dependencies, delta lag, comparison label
SPECS = [
    # First 16 keep their original positions (tests index metrics by position); new metrics are appended.
    ('jobs', 'economy', '비농업 고용 증가(전체)', '천 명', ['PAYEMS'], 3, '직전 3개월 평균 대비'),
    ('unemployment', 'economy', '실업률', '%', ['UNRATE'], 3, '3개월 전 대비'),
    ('pce', 'economy', '근원 PCE 상승률', '%', ['PCEPILFE'], 3, '3개월 전 대비'),
    ('cpi', 'economy', '근원 CPI 상승률', '%', ['CPILFESL'], 3, '3개월 전 대비'),
    ('claims', 'economy', '신규 실업수당 청구(4주 평균)', '천 건', ['ICSA'], 4, '4주 전 대비'),
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
    ('retail', 'economy', '소매판매 증가율', '%', ['CENSUS_MARTS_SM'], 3, '3개월 전 대비'),
    ('durable', 'economy', '내구재 수주 증가율', '%', ['DGORDER'], 3, '3개월 전 대비'),
    ('nfci', 'conditions', '시카고 연은 금융여건지수(NFCI)', '지수', ['NFCI'], 4, '4주 전 대비'),
    ('stlfsi', 'conditions', '세인트루이스 연은 금융스트레스지수', '지수', ['STLFSI4'], 4, '4주 전 대비'),
    ('ust2', 'market', '미국 국채 2년 금리', '%', ['DGS2'], 20, '20관측일 전 대비'),
    ('ust10', 'market', '미국 국채 10년 금리', '%', ['DGS10'], 20, '20관측일 전 대비'),
    ('fedtarget', 'market', '연방기금 목표금리', '%', ['DFEDTARU', 'DFEDTARL'], 20, '20관측일 전 대비'),
    ('usdjpy', 'fx', 'USD/JPY', '엔/달러', ['JPY=X'], 20, '20거래일 전 대비'),
    ('usdkrw', 'fx', 'USD/KRW', '원/달러', ['KRW=X'], 20, '20거래일 전 대비'),
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
    ('us_real_gdp', 'economy', '미국 실질 GDP 증가율', '%', ['GDPC1'], 4, '1년 전 대비'),
    ('philly_fed', 'economy', '필라델피아 연은 제조업 경기', '지수', ['GACDFSA066MSFRBPHI'], 1, '전월 대비'),
    ('umich_sentiment', 'economy', '미시간대 소비자심리지수', '지수', ['UMCSENT'], 1, '전월 대비'),
    ('real_pce', 'economy', '실질 개인소비지출 증가율', '%', ['PCECC96'], 4, '1년 전 대비'),
    ('real_pce_nondurable', 'economy', '실질 비내구재 소비 증가율', '%', ['PCNDGC96'], 4, '1년 전 대비'),
    ('real_pce_durable', 'economy', '실질 내구재 소비 증가율', '%', ['PCDGCC96'], 4, '1년 전 대비'),
    ('real_pce_services', 'economy', '실질 서비스 소비 증가율', '%', ['PCESVC96'], 4, '1년 전 대비'),
    ('sticky_cpi', 'economy', '애틀랜타 연은 Sticky CPI', '%', ['CORESTICKM159SFRBATL'], 3, '3개월 전 대비'),
    ('trimmed_pce', 'economy', '댈러스 연은 절사평균 PCE', '%', ['PCETRIM12M159SFRBDAL'], 3, '3개월 전 대비'),
    ('jp_real_gdp', 'japan', '일본 실질 GDP 증가율', '%', ['JPNRGDPEXP'], 4, '1년 전 대비'),
    ('kr_exports', 'korea', '한국 수출금액지수 증가율(ECOS)', '%', ['ECOS_KR_EXPORT_VALUE'], 12, '1년 전 대비'),
    ('kr_industrial_production', 'korea', '한국 광공업 생산 증가율(KOSIS)', '%', ['KOSIS_KR_INDUSTRIAL_PRODUCTION'], 12, '1년 전 대비'),
    ('kr_retail', 'korea', '한국 소매판매 증가율(KOSIS)', '%', ['KOSIS_KR_RETAIL'], 12, '1년 전 대비'),
    ('kr_unemployment', 'korea', '한국 실업률(KOSIS)', '%', ['KOSIS_KR_UNEMPLOYMENT'], 3, '3개월 전 대비'),
    ('kr_real_gdp', 'korea', '한국 실질 GDP 증가율(ECOS)', '%', ['ECOS_KR_REAL_GDP'], 4, '1년 전 대비'),
    ('kr_semiconductor_exports', 'korea', '한국 반도체 수출금액지수 증가율', '%', ['ECOS_KR_SEMICONDUCTOR_EXPORT_VALUE'], 12, '1년 전 대비'),
    ('kr_cpi_ecos', 'korea', '한국 소비자물가 상승률(ECOS)', '%', ['ECOS_KR_CPI'], 12, '1년 전 대비'),
    ('kr_core_cpi_ecos', 'korea', '한국 근원물가 상승률(ECOS)', '%', ['ECOS_KR_CORE_CPI'], 12, '1년 전 대비'),
    ('kr_house_prices_ecos', 'korea', '한국 주택매매가격지수(ECOS·KB)', '지수', ['ECOS_KR_HOUSE_PRICES'], 12, '1년 전 대비'),
    ('kr_base_rate', 'korea', '한국은행 기준금리', '%', ['ECOS_KR_BASE_RATE'], 20, '20관측일 전 대비'),
    ('kr_reserves_bok', 'korea', '한국은행 외환보유액(금 포함)', '억 달러', ['ECOS_KR_RESERVES'], 3, '3개월 전 대비'),
    ('kr_household_credit_bok', 'korea', '한국은행 가계신용 잔액', '조 원', ['ECOS_KR_HOUSEHOLD_CREDIT'], 4, '1년 전 대비'),
    ('kr_bsi_bok', 'korea', '한국은행 기업경기실사지수(BSI)', '지수', ['ECOS_KR_BSI'], 1, '전월 대비'),
    ('kr_ccsi', 'korea', '한국은행 소비자심리지수(CCSI)', '지수', ['ECOS_KR_CCSI'], 1, '전월 대비'),
    ('jobs_private', 'economy', '비농업 고용 증가(민간)', '천 명', ['USPRIV'], 3, '직전 3개월 평균 대비'),
    ('jobs_government', 'economy', '비농업 고용 증가(정부)', '천 명', ['USGOVT'], 3, '직전 3개월 평균 대비'),
    ('claims_weekly', 'economy', '신규 실업수당 청구(주간)', '천 건', ['ICSA'], 1, '전주 대비'),
    ('retail_mom', 'economy', '미국 소매판매 증가율 (MoM)', '%', ['CENSUS_MARTS_MPCSM'], 1, '1개월 전 대비'),
    ('eurusd', 'fx', 'EUR/USD', '달러/유로', ['EURUSD=X'], 20, '20거래일 전 대비'),
    ('gbpusd', 'fx', 'GBP/USD', '달러/파운드', ['GBPUSD=X'], 20, '20거래일 전 대비'),
    ('eurgbp', 'fx', 'EUR/GBP', '파운드/유로', ['EURGBP=X'], 20, '20거래일 전 대비'),
    ('usdcny', 'fx', 'USD/CNY', '위안/달러', ['CNY=X'], 20, '20거래일 전 대비'),
    ('gbpjpy', 'fx', 'GBP/JPY', '엔/파운드', ['GBPJPY=X'], 20, '20거래일 전 대비'),
    ('eurjpy', 'fx', 'EUR/JPY', '엔/유로', ['EURJPY=X'], 20, '20거래일 전 대비'),
    ('gbpkrw', 'fx', 'GBP/KRW', '원/파운드', ['GBPKRW=X'], 20, '20거래일 전 대비'),
    ('eurkrw', 'fx', 'EUR/KRW', '원/유로', ['EURKRW=X'], 20, '20거래일 전 대비'),
    ('jpykrw_100', 'fx', 'JPY/KRW · 100엔', '원/100엔', ['JPYKRW=X'], 20, '20거래일 전 대비'),
    ('cnykrw', 'fx', 'CNY/KRW', '원/위안', ['KRW=X', 'CNY=X'], 20, '20거래일 전 대비'),
]
FORMULAS = {
    'jobs': '(PAYEMS[t] − PAYEMS[t−3 calendar months]) / 3; thousands',
    'jobs_private': '(USPRIV[t] − USPRIV[t−3 calendar months]) / 3; thousands',
    'jobs_government': '(USGOVT[t] − USGOVT[t−3 calendar months]) / 3; thousands',
    'unemployment': 'UNRATE (%)',
    'pce': '(PCEPILFE[t]/PCEPILFE[t−12 months]−1)×100; secondary: ((PCEPILFE[t]/PCEPILFE[t−3 months])^4−1)×100',
    'pce_headline': '(PCEPI[t]/PCEPI[t−12 months]−1)×100; no interpolation',
    'cpi': '(CPILFESL[t]/CPILFESL[t−12 months]−1)×100; no interpolation',
    'cpi_headline': '(CPIAUCSL[t]/CPIAUCSL[t−12 months]−1)×100; seasonally adjusted index, no interpolation',
    'ppi_core': '(PPIFES[t]/PPIFES[t−12 months]−1)×100; final demand less foods and energy, seasonally adjusted',
    'claims_weekly': 'ICSA / 1000; weekly initial claims, thousands',
    'claims': 'Mean of 4 consecutive calendar weeks of ICSA / 1000',
    'retail': '(CENSUS_MARTS_SM[t]/CENSUS_MARTS_SM[t−12 months]−1)×100; MARTS 44X72 retail and food services sales, seasonally adjusted, nominal',
    'retail_mom': 'CENSUS_MARTS_MPCSM; official MARTS 44X72 monthly percent change (MPCSM), seasonally adjusted, nominal',
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
    'eurusd': 'Yahoo EURUSD=X Close; USD per EUR',
    'gbpusd': 'Yahoo GBPUSD=X Close; USD per GBP',
    'eurgbp': 'Yahoo EURGBP=X Close; GBP per EUR',
    'usdcny': 'Yahoo CNY=X Close; CNY per USD',
    'gbpjpy': 'Yahoo GBPJPY=X Close; JPY per GBP',
    'eurjpy': 'Yahoo EURJPY=X Close; JPY per EUR',
    'gbpkrw': 'Yahoo GBPKRW=X Close; KRW per GBP',
    'eurkrw': 'Yahoo EURKRW=X Close; KRW per EUR',
    'jpykrw_100': 'Yahoo JPYKRW=X Close × 100; KRW per 100 JPY',
    'cnykrw': 'KRW=X / CNY=X; same-date ratio = KRW per CNY',
    'gold': 'Yahoo GC=F Close; COMEX futures, continuous front month (not spot)',
    'silver': 'Yahoo SI=F Close; COMEX futures, continuous front month (not spot)',
    'copper': 'Yahoo HG=F Close; COMEX futures, continuous front month (not spot)',
    'wti': 'Yahoo CL=F Close; NYMEX futures, continuous front month (not spot)',
    'brent': 'Yahoo BZ=F Close; ICE futures, continuous front month (not spot)',
    'buffett': 'BOGZ1FL883164113Q / (GDP × 1000) × 100; 미국 국내 상장주식 시가총액의 분기말 시장가치를 명목 GDP로 나눈 대용 지표',
    'household_debt_service': 'FRED TDSP 분기 수준; 가처분소득 대비 가계부채(모기지+소비자부채) 원리금 상환비율',
    'bank_lending': 'FRED DRTSCILM 분기 수준; SLOOS 설문 기준 대형·중견기업 상업대출에 대한 은행의 순(긴축−완화) 응답 비율',
    'us_real_gdp': '(GDPC1[t]/GDPC1[t−4 quarters]−1)×100; 실질 GDP 전년동기 대비',
    'philly_fed': 'GACDFSA066MSFRBPHI; 필라델피아 연은 제조업 일반 경기활동 확산지수, 0 초과는 개선 응답 우세',
    'umich_sentiment': 'UMCSENT; 미시간대 소비자심리지수 원계열',
    'real_pce': '(PCECC96[t]/PCECC96[t−4 quarters]−1)×100; 실질 PCE 전년동기 대비',
    'real_pce_nondurable': '(PCNDGC96[t]/PCNDGC96[t−4 quarters]−1)×100; 실질 비내구재 PCE 전년동기 대비',
    'real_pce_durable': '(PCDGCC96[t]/PCDGCC96[t−4 quarters]−1)×100; 실질 내구재 PCE 전년동기 대비',
    'real_pce_services': '(PCESVC96[t]/PCESVC96[t−4 quarters]−1)×100; 실질 서비스 PCE 전년동기 대비',
    'sticky_cpi': 'CORESTICKM159SFRBATL; 애틀랜타 연은 Sticky Price CPI 전년 대비 상승률(원자료)',
    'trimmed_pce': 'PCETRIM12M159SFRBDAL; 댈러스 연은 Trimmed Mean PCE 전년 대비 상승률(원자료)',
    'jp_real_gdp': '(JPNRGDPEXP[t]/JPNRGDPEXP[t−4 quarters]−1)×100; Cabinet Office real GDP, FRED 배포 계열의 전년동기 대비',
    'kr_exports': '(ECOS 월별 수출금액지수[t]/ECOS 월별 수출금액지수[t−12개월]−1)×100',
    'kr_industrial_production': '(KOSIS 광공업생산지수[t]/KOSIS 광공업생산지수[t−12개월]−1)×100',
    'kr_retail': '(KOSIS 전국 소매판매액지수[t]/KOSIS 전국 소매판매액지수[t−12개월]−1)×100',
    'kr_unemployment': '국가데이터처 경제활동인구조사 전국 실업률(원계열)',
    'kr_real_gdp': '(ECOS 실질 GDP[t]/ECOS 실질 GDP[t−4분기]−1)×100; 정확히 4분기 전 관측값과 비교',
    'kr_semiconductor_exports': '(ECOS 반도체 수출금액지수[t]/12개월 전 지수−1)×100; 403Y001/3091AA',
    'kr_cpi_ecos': 'ECOS 소비자물가지수 총지수 전년동월 대비 상승률',
    'kr_core_cpi_ecos': 'ECOS 농산물·석유류 제외 소비자물가지수 전년동월 대비 상승률',
    'kr_house_prices_ecos': 'ECOS 901Y062 전국 주택매매가격지수(KB); 월간 명목 지수',
    'kr_base_rate': 'ECOS 722Y001 / 0101000; 한국은행 기준금리, 일별',
    'kr_reserves_bok': '한국은행 ECOS 외환보유액 월말 금 포함 잔액',
    'kr_household_credit_bok': '한국은행 ECOS 가계신용 분기 잔액',
    'kr_bsi_bok': '한국은행 ECOS 기업경기조사 제조업 업황 BSI',
    'kr_ccsi': '한국은행 ECOS 소비자동향조사 소비자심리지수(CCSI)',
}
# Shown under the chart for metrics that have no stored note yet.
FUTURES_NOTE = '선물 근월물 연속 시세이며 현물이 아닙니다. 만기 교체 시점에 가격이 불연속으로 움직일 수 있습니다.'
FX_NOTE = 'Yahoo Finance 시장 종가 기준입니다. FRED 고시환율(뉴욕 정오)과 값이 다를 수 있습니다.'
NOTES = {
    'ppi_core': '식품·에너지를 제외한 최종수요 생산자물가(계절조정)의 전년 대비 상승률입니다.',
    'durable': '항공기 등 대형 수주의 영향으로 월별 변동이 큽니다.',
    'retail': '미국 Census Bureau MARTS의 소매·음식서비스 전체(44X72) 계절조정 판매액(SM) 전년 대비 증가율입니다. 명목 지표이며 개정될 수 있습니다.',
    'retail_mom': '미국 Census Bureau MARTS의 소매·음식서비스 전체(44X72) 공식 전월 대비 증가율(MPCSM)입니다. 계절조정 명목 지표이며 개정될 수 있습니다.',
    'usdjpy': FX_NOTE, 'usdkrw': FX_NOTE,
    'eurusd': FX_NOTE, 'gbpusd': FX_NOTE, 'eurgbp': FX_NOTE, 'usdcny': FX_NOTE,
    'gbpjpy': FX_NOTE, 'eurjpy': FX_NOTE, 'gbpkrw': FX_NOTE, 'eurkrw': FX_NOTE,
    'jpykrw_100': 'JPY/KRW Yahoo 환율에 100을 곱한 원/100엔입니다.', 'cnykrw': FX_NOTE,
    'dxy': 'Yahoo Finance의 ICE 달러인덱스 종가입니다.',
    'gold': FUTURES_NOTE, 'silver': FUTURES_NOTE, 'copper': FUTURES_NOTE,
    'wti': FUTURES_NOTE, 'brent': FUTURES_NOTE,
    'buffett': ('연준 금융계정의 미국 국내 상장주식 시가총액을 명목 GDP로 나눈 '
                '버핏지수 대용치입니다. 기존 Wilshire 5000 기반 계열과 정의가 달라 '
                '과거 수치가 완전히 같지는 않습니다. 분기 자료이며 개정될 수 있습니다.'),
    'household_debt_service': '가계가 가처분소득 중 부채 원리금 상환에 쓰는 비율입니다. 모기지·소비자부채를 합산한 값입니다.',
    'bank_lending': '연준 SLOOS 설문 기준입니다. 양수(+)는 순 긴축, 음수(-)는 순 완화를 의미하며, 2001년·2008-09년 침체 전 뚜렷한 긴축이 관측된 바 있습니다.',
    'philly_fed': '월간 필라델피아 연은 제조업 설문 일반 경기활동 확산지수입니다. 0을 웃돌면 개선 응답이 악화 응답보다 많습니다.',
    'umich_sentiment': '미시간대 조사로 측정한 소비자심리지수입니다. 수준과 추세를 함께 보세요.',
    'real_pce': 'BEA 실질 PCE 연쇄달러 기준의 전년동기 대비 증가율입니다. 분기 자료이며 개정될 수 있습니다.',
    'real_pce_nondurable': 'BEA 실질 비내구재 상품 소비의 전년동기 대비 증가율입니다. 분기 자료이며 개정될 수 있습니다.',
    'real_pce_durable': 'BEA 실질 내구재 상품 소비의 전년동기 대비 증가율입니다. 분기 자료이며 변동성이 큽니다.',
    'real_pce_services': 'BEA 실질 서비스 소비의 전년동기 대비 증가율입니다. 분기 자료이며 개정될 수 있습니다.',
    'sticky_cpi': '애틀랜타 연은이 가격 조정 빈도가 낮은 항목을 묶은 물가지수의 전년 대비 상승률입니다.',
    'trimmed_pce': '댈러스 연은이 월별 극단값을 절사해 계산한 근원 PCE 물가의 전년 대비 상승률입니다.',
    'jp_real_gdp': '일본 내각부 국민계정 기반 실질 GDP의 FRED 배포 계열입니다. 분기 자료이며 개정될 수 있습니다.',
    'kr_industrial_production': '국가데이터처 KOSIS 광공업생산지수의 전년 동월 대비 증가율입니다.',
    'kr_exports': '한국은행 ECOS 수출금액지수 총지수의 전년 동월 대비 증가율입니다. 통관 수출액 자체와 단위가 다른 지수입니다.',
    'kr_retail': '국가데이터처 KOSIS 전국 소매판매액지수의 전년 동월 대비 증가율입니다.',
    'kr_unemployment': '국가데이터처 경제활동인구조사에서 공표하는 전국 실업률입니다.',
    'kr_real_gdp': '한국은행 ECOS 국민계정 실질 GDP의 전년동기 대비 증가율입니다. 분기 자료이며 개정될 수 있습니다.',
    'kr_semiconductor_exports': '한국은행 ECOS 반도체 수출금액지수의 전년 동월 대비 증가율입니다. 수출액 자체가 아닌 지수로 계산합니다.',
    'kr_cpi_ecos': '한국은행 ECOS에 수록된 총 CPI로 계산한 전년동월 대비 상승률입니다.',
    'kr_core_cpi_ecos': '한국은행 ECOS의 농산물·석유류 제외 CPI를 사용합니다.',
    'kr_house_prices_ecos': '한국은행 ECOS가 제공하는 KB 전국 주택매매가격지수입니다. 기준연도 개편 시 과거 값이 수정될 수 있습니다.',
    'kr_base_rate': '한국은행이 결정·공표하는 정책 기준금리의 일별 계열입니다.',
    'kr_reserves_bok': '한국은행 ECOS에서 제공하는 월말 외환보유액입니다.',
    'kr_household_credit_bok': '한국은행 가계신용 분기 잔액으로, 가계대출과 판매신용을 포함합니다.',
    'kr_bsi_bok': '한국은행 기업경기실사지수(BSI)입니다. 통상 100을 중심으로 경기 판단을 읽습니다.',
    'kr_ccsi': '한국은행 소비자심리지수(CCSI)입니다. 통상 100을 장기 평균 기준으로 해석합니다.',
}

SOURCE_ORIGINS = {
    'OECD_CLI_KR': 'OECD Composite Leading Indicator (OECD SDMX API)',
    'CENSUS_MARTS_SM': 'U.S. Census Bureau Economic Indicators Time Series API (MARTS)',
    'CENSUS_MARTS_MPCSM': 'U.S. Census Bureau Economic Indicators Time Series API (MARTS)',
    **{sid: 'Bank of Korea ECOS Open API' for sid in kr.ECOS},
    **{sid: 'Statistics Korea KOSIS Open API' for sid in kr.KOSIS},
}
SOURCE_URLS = {
    'OECD_CLI_KR': 'https://data-explorer.oecd.org/vis?df%5Bag%5D=OECD.SDD.STES&df%5Bds%5D=dsDisseminateFinalDMZ&df%5Bid%5D=DSD_STES%40DF_CLI',
    'CENSUS_MARTS_SM': 'https://www.census.gov/retail/marts/',
    'CENSUS_MARTS_MPCSM': 'https://www.census.gov/retail/marts/',
    **{sid: 'https://ecos.bok.or.kr/' for sid in kr.ECOS},
    **{sid: 'https://kosis.kr/' for sid in kr.KOSIS},
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


def parse_census_marts(payload, expected_data_type):
    """Parse a Census EITS/MARTS JSON table into clean monthly observations."""
    if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[0], list):
        raise ValueError('Unexpected Census MARTS response')
    headers = [str(item).strip().lower() for item in payload[0]]
    required = {'cell_value', 'category_code', 'data_type_code',
                'seasonally_adj', 'error_data'}
    if not required.issubset(headers):
        raise ValueError('Census MARTS response is missing required fields')
    date_field = next((field for field in ('time_slot_date', 'time')
                       if field in headers), None)
    if date_field is None:
        raise ValueError('Census MARTS response is missing a date field')
    indexes = {name: headers.index(name) for name in required}
    date_index = headers.index(date_field)
    rows = []
    for row in payload[1:]:
        if not isinstance(row, list) or len(row) < len(headers):
            continue
        values = {name: str(row[index]).strip()
                  for name, index in indexes.items()}
        if (values['category_code'] != '44X72'
                or values['data_type_code'].upper() != expected_data_type
                or values['seasonally_adj'].lower() != 'yes'
                or values['error_data'].lower() != 'no'):
            continue
        period = str(row[date_index]).strip()
        # EITS time values are YYYY-MM; time_slot_date is usually ISO date.
        if len(period) == 7 and period[4] == '-':
            period += '-01'
        try:
            day = date.fromisoformat(period[:10]).replace(day=1).isoformat()
        except (ValueError, TypeError):
            continue
        rows.append((day, row[indexes['cell_value']]))
    points = clean(rows)
    if not any(finite(value) for _, value in points):
        raise ValueError('Census MARTS returned no usable observations')
    return points


def census_error_detail(response, api_key):
    """Return a short Census error body with direct and URL-encoded keys redacted."""
    try:
        detail = str(response.text).strip()
    except Exception:
        return ''
    for secret in (api_key, quote(api_key, safe='')):
        if secret:
            detail = detail.replace(secret, '[REDACTED]')
    return detail[:240]


def fetch_census_marts(data_type_code):
    api_key = os.environ.get('CENSUS_API_KEY', '').strip()
    if not api_key:
        raise ValueError('CENSUS_API_KEY is not configured')
    from curl_cffi import requests

    params = {
        'get': 'data_type_code,seasonally_adj,category_code,cell_value,error_data,time_slot_id,time_slot_date',
        'category_code': '44X72',
        'data_type_code': data_type_code,
        'seasonally_adj': 'yes',
        'error_data': 'no',
        'time': 'from 1992',
        'key': api_key,
    }
    try:
        response = requests.get(
            'https://api.census.gov/data/timeseries/eits/marts',
            params=params, timeout=30,
        )
    except Exception as exc:
        # Do not include the request exception: some HTTP libraries print the
        # full request URL, which contains the API key.
        raise ValueError(
            f'Census MARTS {data_type_code} request failed ({type(exc).__name__})'
        ) from None
    if response.status_code >= 400:
        detail = census_error_detail(response, api_key)
        suffix = f': {detail}' if detail else ''
        raise ValueError(
            f'Census MARTS {data_type_code} returned HTTP {response.status_code}{suffix}'
        )
    try:
        payload = response.json()
    except Exception:
        # Census can return an HTML activation/error page even with HTTP 200.
        detail = census_error_detail(response, api_key)
        suffix = f': {detail}' if detail else ''
        raise ValueError(
            f'Census MARTS {data_type_code} returned invalid JSON{suffix}'
        ) from None
    return parse_census_marts(payload, data_type_code)


def fetch_series(sid):
    if sid in YAHOO:
        import yfinance as yf
        frame = yf.download(sid, period='max', interval='1d', auto_adjust=False,
                            back_adjust=False, repair=False, keepna=True,
                            progress=False, threads=False, timeout=30,
                            multi_level_index=False)
        if frame is None or frame.empty:
            raise ValueError('Yahoo returned no prices')
        # Keep only completed local-market sessions, using each exchange's clock.
        if sid in {'^N225', '1306.T'}:
            local_now = datetime.now(ZoneInfo('Asia/Tokyo'))
            completed_day = (local_now.date()
                             if (local_now.hour, local_now.minute) >= (15, 30)
                             else local_now.date() - timedelta(days=1))
        elif sid in {'^GSPC', '^NDX', 'SPY', 'RSP'}:
            local_now = datetime.now(ZoneInfo('America/New_York'))
            completed_day = (local_now.date() if local_now.hour >= 16
                             else local_now.date() - timedelta(days=1))
        else:
            completed_day = datetime.now(timezone.utc).date() - timedelta(days=1)
        points = clean(((d.strftime('%Y-%m-%d'), v) for d, v in frame['Close'].items()),
                       today=completed_day)
    elif sid in CENSUS_SOURCES:
        points = fetch_census_marts(CENSUS_SOURCES[sid])
    elif sid in CLI_SOURCES:
        points = cli.fetch_points(CLI_SOURCES[sid])
    elif sid in es.SOURCES:
        points = es.fetch_points(sid)
    elif sid in jp.SOURCES:
        points = jp.fetch_points(sid)
    elif sid in sh.SOURCES:
        points = sh.fetch_points(sid)
    elif sid in kr.ECOS or sid in kr.KOSIS:
        points = kr.fetch_points(sid)
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


KOSIS_DOWNLOAD_SLOTS = Semaphore(2)
OECD_DOWNLOAD_SLOTS = Semaphore(1)


def fetch_bounded(sid, timeout=60):
    """A separate process makes the entire provider download deadline enforceable."""
    if sid.startswith(('KOSIS_', 'OECD_CLI_')) and timeout == 60:
        timeout = 100
    logging.info('%s: starting download (maximum %ss)', sid, timeout)
    slot = (KOSIS_DOWNLOAD_SLOTS if sid.startswith('KOSIS_') else
            OECD_DOWNLOAD_SLOTS if sid.startswith('OECD_CLI_') else None)
    if slot:
        slot.acquire()
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
    finally:
        if slot:
            slot.release()


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
    mom = lambda sid: lagged(m(sid), 1, lambda a, b: (a/b-1)*100 if b > 0 else None)
    quarterly_yoy = lambda sid: lagged(s(sid), 4, lambda a, b: (a/b-1)*100 if b > 0 else None)

    def korea_gdp_yoy(sid):
        """Compare only the same calendar quarter one year apart; reject bad API spikes."""
        points = s(sid)
        by_quarter = {}
        for day, value in points:
            year, month = map(int, day[:7].split('-'))
            quarter = (month - 1) // 3
            by_quarter[(year, quarter)] = value
        output = []
        for day, value in points:
            year, month = map(int, day[:7].split('-'))
            quarter = (month - 1) // 3
            prior = by_quarter.get((year - 1, quarter))
            growth = ((value / prior - 1) * 100
                      if finite(value) and finite(prior) and prior > 0 else None)
            if growth is not None and abs(growth) > 35:
                growth = None
            output.append([day, growth])
        return output
    ratio = aligned([s('RSP'), s('SPY')], lambda a, b: a/b if b > 0 else None)

    # Keep only common trading dates, retaining explicit nulls on common dates.
    spy_dates = dict(s('SPY'))
    ratio = [p for p in ratio if p[0] in spy_dates]

    # Buffett-indicator proxy: both inputs are quarterly observations.
    # The market-cap series is in millions of dollars and GDP is in billions.
    gdp_points = s('GDP')
    return {
        'jobs': transform(lagged(m('PAYEMS'), 3, lambda a, b: a-b), lambda v: v/3),
        'jobs_private': transform(lagged(m('USPRIV'), 3, lambda a, b: a-b), lambda v: v/3),
        'jobs_government': transform(lagged(m('USGOVT'), 3, lambda a, b: a-b), lambda v: v/3),
        'unemployment': m('UNRATE'), 'pce': yoy('PCEPILFE'), 'cpi': yoy('CPILFESL'),
        'pce_headline': yoy('PCEPI'), 'cpi_headline': yoy('CPIAUCSL'),
        'ppi_core': yoy('PPIFES'), 'retail': yoy('CENSUS_MARTS_SM'),
        'retail_mom': m('CENSUS_MARTS_MPCSM'), 'durable': yoy('DGORDER'),
        'claims_weekly': transform(calendar(s('ICSA'), weekly=True), lambda v: v/1000),
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
        'eurusd': s('EURUSD=X'), 'gbpusd': s('GBPUSD=X'), 'eurgbp': s('EURGBP=X'),
        'usdcny': s('CNY=X'), 'gbpjpy': s('GBPJPY=X'), 'eurjpy': s('EURJPY=X'),
        'gbpkrw': s('GBPKRW=X'), 'eurkrw': s('EURKRW=X'),
        'jpykrw_100': transform(s('JPYKRW=X'), lambda value: value*100),
        'cnykrw': aligned([s('KRW=X'), s('CNY=X')], lambda krw_per_usd, cny_per_usd: krw_per_usd/cny_per_usd if cny_per_usd > 0 else None),
        'gold': s('GC=F'), 'silver': s('SI=F'), 'copper': s('HG=F'),
        'wti': s('CL=F'), 'brent': s('BZ=F'),
        'pce_secondary': lagged(m('PCEPILFE'), 3, lambda a, b: ((a/b)**4-1)*100 if b > 0 else None),
        'buffett': aligned(
            [s('BOGZ1FL883164113Q'), gdp_points],
            lambda market_cap, gdp: market_cap/(gdp*1000)*100 if gdp > 0 else None,
        ),
        'household_debt_service': s('TDSP'),
        'bank_lending': s('DRTSCILM'),
        'us_real_gdp': quarterly_yoy('GDPC1'),
        'philly_fed': s('GACDFSA066MSFRBPHI'),
        'umich_sentiment': s('UMCSENT'),
        'real_pce': quarterly_yoy('PCECC96'),
        'real_pce_nondurable': quarterly_yoy('PCNDGC96'),
        'real_pce_durable': quarterly_yoy('PCDGCC96'),
        'real_pce_services': quarterly_yoy('PCESVC96'),
        'sticky_cpi': s('CORESTICKM159SFRBATL'),
        'trimmed_pce': s('PCETRIM12M159SFRBDAL'),
        'jp_real_gdp': quarterly_yoy('JPNRGDPEXP'),
        'kr_exports': yoy('ECOS_KR_EXPORT_VALUE'),
        'kr_industrial_production': yoy('KOSIS_KR_INDUSTRIAL_PRODUCTION'),
        'kr_retail': yoy('KOSIS_KR_RETAIL'),
        'kr_unemployment': s('KOSIS_KR_UNEMPLOYMENT'),
        'kr_real_gdp': korea_gdp_yoy('ECOS_KR_REAL_GDP'),
        'kr_semiconductor_exports': yoy('ECOS_KR_SEMICONDUCTOR_EXPORT_VALUE'),
        'kr_cpi_ecos': yoy('ECOS_KR_CPI'),
        'kr_core_cpi_ecos': yoy('ECOS_KR_CORE_CPI'),
        'kr_house_prices_ecos': s('ECOS_KR_HOUSE_PRICES'),
        'kr_base_rate': s('ECOS_KR_BASE_RATE'),
        'kr_reserves_bok': s('ECOS_KR_RESERVES'),
        'kr_household_credit_bok': transform(s('ECOS_KR_HOUSEHOLD_CREDIT'), lambda v: v/1000),
        'kr_bsi_bok': s('ECOS_KR_BSI'),
        'kr_ccsi': s('ECOS_KR_CCSI'),
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
    origin, url = (es.ORIGINS.get(sid) or jp.ORIGINS.get(sid) or
                   sh.ORIGINS.get(sid) or (
        ('Yahoo Finance', f'https://finance.yahoo.com/quote/{quote(sid, safe="")}/history/')
        if sid in YAHOO else ('FRED', f'https://fred.stlouisfed.org/series/{sid}')))
    origin = SOURCE_ORIGINS.get(sid, origin)
    url = SOURCE_URLS.get(sid, url)
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
                     if mid == 'fedtarget' else
                     None)
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
        dcfInputs={
            key: {
                'label': label,
                'indexPoints': copy.deepcopy(raw.get(index_sid, {}).get('points', []))[-2600:],
                'riskFreePoints': copy.deepcopy(raw.get(rate_sid, {}).get('points', []))[-2600:],
                'indexSource': source_metadata(index_sid, raw, today),
                'riskFreeSource': source_metadata(rate_sid, raw, today),
            }
            for key, label, index_sid, rate_sid in DCF_INDEXES
        },
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


def active_source_errors(cache_data, frequencies=None):
    """Return failures only for sources used by the current configuration."""
    frequencies = FREQUENCIES if frequencies is None else frequencies
    return [sid for sid in frequencies if cache_data.get(sid, {}).get('error')]


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
                if entry['points']:
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
    if not args.offline:
        failures = active_source_errors(read_json(args.cache, {}))
        if count < len(FREQUENCIES) or failures:
            logging.error('One or more active sources failed: %s',
                          ', '.join(failures) or 'unknown source')
            raise SystemExit(2)

