"""Optional official ECOS and KOSIS adapters for Korean macro indicators.

API keys are read only from environment variables. The adapters discover table
and item identifiers from the providers' metadata APIs instead of embedding
personal credentials or relying on browser-only session state.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


ECOS = {
    'ECOS_KR_BASE_RATE': {
        'title': ('한국은행 기준금리',), 'item': ('한국은행 기준금리',),
        'cycle': 'D', 'stat': '722Y001', 'item_codes': ['0101000'],
        'start': '19990101',
    },
    'ECOS_KR_REAL_GDP': {
        'title': ('국내총생산',), 'item': ('국내총생산', '실질'),
        'cycle': 'Q', 'stat': '200Y102', 'item_codes': ['10111'],
        'start': '1960Q1',
    },
    'ECOS_KR_HOUSEHOLD_CREDIT': {
        'title': ('가계신용',), 'item': ('가계신용',),
        'cycle': 'Q', 'start': '2002Q1',
    },
    'ECOS_KR_RESERVES': {
        'title': ('외환보유액',), 'item': ('외환보유액',),
        'cycle': 'M', 'start': '199001',
    },
    'ECOS_KR_BSI': {
        'title': ('기업경기조사', '기업경기실사지수'),
        'item': ('제조업 업황', '업황실적'), 'cycle': 'M', 'start': '200301',
    },
    'ECOS_KR_CCSI': {
        'title': ('소비자동향조사', '소비자심리지수'),
        'item': ('소비자심리지수',), 'cycle': 'M', 'start': '200801',
        'stat': '511Y002', 'item_codes': ['FME', '99988'],
    },
    'ECOS_KR_CPI': {
        'title': ('소비자물가지수',), 'item': ('총지수',),
        'cycle': 'M', 'stat': '901Y009', 'item_codes': ['0'], 'start': '196501',
    },
    'ECOS_KR_CORE_CPI': {
        'title': ('소비자물가지수(특수분류)',), 'item': ('농산물및석유류제외지수',),
        'cycle': 'M', 'stat': '901Y010', 'item_codes': ['QB'], 'start': '196501',
    },
}

KOSIS = {
    'KOSIS_KR_EXPORTS': {
        'search': '품목별 수출입실적', 'table_terms': ('품목별 수출입실적', '수출입실적'),
        'item_terms': ('항목',),
        'output_terms': ('총계', '계', '전체', '전국'),
        'cycle': 'M', 'start': '200001',
    },
    'KOSIS_KR_SEMICONDUCTOR_EXPORTS': {
        'search': '품목별 수출입실적', 'table_terms': ('품목별 수출입실적', '수출입실적'),
        'item_terms': ('항목',), 'output_target': '반도체',
        'output_terms': ('총계', '계', '전체', '전국'),
        'cycle': 'M', 'start': '200001',
    },
    'KOSIS_KR_INDUSTRIAL_PRODUCTION': {
        'search': '광공업생산지수', 'table_terms': ('광공업생산지수',),
        'item_terms': ('광공업생산지수', '생산지수'),
        'output_terms': ('광공업', '전산업', '전국', '총지수', '계', '전체'),
        'cycle': 'M', 'start': '200001',
    },
    'KOSIS_KR_RETAIL': {
        'search': '재별 및 상품군별 소매판매액지수',
        'table_terms': ('재별및상품군별소매판매액지수', '소매판매액지수'),
        'item_terms': ('소매판매액지수', '불변지수'),
        'output_terms': ('전국', '총지수', '계', '전체'),
        'cycle': 'M', 'start': '200001',
    },
    'KOSIS_KR_UNEMPLOYMENT': {
        'search': '경제활동인구총괄 공식 실업률',
        'table_terms': ('경제활동인구총괄', '실업률'),
        'item_terms': ('실업률',), 'output_terms': ('전국', '계', '전체', '15세 이상 전체'),
        'cycle': 'M', 'start': '199906',
    },
    'KOSIS_KR_HOUSE_PRICES': {
        'search': '전국주택가격동향조사 매매가격지수',
        'table_terms': ('주택가격',), 'item_terms': ('주택가격지수',),
        'output_terms': ('전국', '매매', '전체', '계'),
        'cycle': 'M', 'start': '200301',
    },
}


def _json(url: str, timeout: int = 20):
    request = Request(url, headers={'User-Agent': 'macro-observer/1.0'})
    try:
        with urlopen(request, timeout=timeout) as response:
            data = response.read().decode('utf-8-sig')
    except Exception as exc:
        # urllib exception messages can include the full request URL. Never
        # propagate ECOS/KOSIS credentials into GitHub Actions logs.
        raise RuntimeError(f'Korean statistics API request failed ({type(exc).__name__})') from None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        if 'kosis.kr' in url:
            # KOSIS metadata may use JavaScript-style unquoted property names.
            # Quote only property names in object positions, never evaluate code.
            normalized = re.sub(r'([\{,]\s*)([A-Za-z_][A-Za-z_0-9]*)(\s*:)',
                                r'\1"\2"\3', data)
            try:
                return json.loads(normalized)
            except json.JSONDecodeError:
                pass
        # KOSIS sometimes formats API errors as JavaScript object literals
        # (for example {err:"11",errMsg:"..."}), which are not JSON.
        code = re.search(r'\berr\s*:\s*["\']?(\d+)', data)
        if 'kosis.kr' in url and code:
            explanations = {'10': '인증키 누락', '11': '인증키가 유효하지 않음'}
            detail = explanations.get(code.group(1), '공급자 오류')
            raise ValueError(f'KOSIS API 오류 {code.group(1)}: {detail}') from None
        prefix = data[:64]
        for secret in (os.environ.get('ECOS_API_KEY', ''), os.environ.get('KOSIS_API_KEY', '')):
            if secret:
                prefix = prefix.replace(secret, '[redacted]')
        prefix = re.sub(r'[A-Za-z0-9+/=]{24,}', '[redacted]', prefix)
        raise ValueError(f'한국 통계 API가 유효한 JSON을 반환하지 않았습니다 (응답 시작: {prefix!r})') from None


def _rows(payload, key):
    block = payload.get(key, {}) if isinstance(payload, dict) else {}
    rows = block.get('row', []) if isinstance(block, dict) else []
    if not rows and isinstance(payload, list):
        rows = payload
    if not rows and isinstance(payload, dict) and payload.get('RESULT'):
        result = payload['RESULT']
        raise ValueError(f"Provider API {result.get('CODE')}: {result.get('MESSAGE')}")
    return rows if isinstance(rows, list) else []


def _ecos_rows(service, api_key, *parts, end=1000):
    suffix = '/'.join(quote(str(p), safe='') for p in parts)
    all_rows = []
    start = 1
    while start <= end:
        stop = min(start + 999, end)
        url = f'https://ecos.bok.or.kr/api/{service}/{quote(api_key, safe="")}/json/kr/{start}/{stop}'
        if suffix:
            url += '/' + suffix
        payload = _json(url)
        rows = _rows(payload, service)
        all_rows.extend(rows)
        if len(rows) < stop - start + 1:
            break
        start = stop + 1
    return all_rows


def _score_text(text, terms):
    normalized = re.sub(r'\s+', '', str(text)).lower()
    return sum(1 for term in terms if re.sub(r'\s+', '', term).lower() in normalized)


def _select_ecos_codes(rows, cfg):
    """Choose one leaf item per ECOS group, preferring total/national rows."""
    matching = [r for r in rows if r.get('CYCLE') == cfg['cycle']]
    groups = {}
    for row in matching:
        group = row.get('GRP_CODE') or 'Group1'
        groups.setdefault(group, []).append(row)
    codes = []
    for group in sorted(groups):
        choices = groups[group]
        best = max(choices, key=lambda r: (
            _score_text(r.get('ITEM_NAME', ''), cfg['item']),
            bool(r.get('P_ITEM_CODE')),
            sum(term in str(r.get('ITEM_NAME', '')) for term in ('전국', '전체', '총계', '총액')),
            -sum(term in str(r.get('ITEM_NAME', '')) for term in ('지역별', '업종별', '전망', '예상')),
            bool(r.get('ITEM_CODE')),
        ))
        codes.append(best.get('ITEM_CODE', ''))
    return codes[:4]


def _discover_ecos_table(rows, cfg):
    candidates = [row for row in rows if row.get('SRCH_YN') == 'Y'
                  and row.get('CYCLE') == cfg['cycle']
                  and _score_text(row.get('STAT_NAME', ''), cfg['title'])]
    if not candidates:
        raise ValueError('ECOS 통계표를 찾지 못했습니다: ' + '/'.join(cfg['title']))
    return max(candidates, key=lambda row: (
        _score_text(row.get('STAT_NAME', ''), cfg['title']),
        row.get('STAT_NAME', '').count('.'),
    ))


def fetch_ecos(sid):
    cfg = ECOS[sid]
    api_key = os.environ.get('ECOS_API_KEY', '').strip()
    if not api_key:
        raise ValueError('GitHub Actions secret ECOS_API_KEY is not configured')
    tables = _ecos_rows('StatisticTableList', api_key)
    table = next((r for r in tables if r.get('STAT_CODE') == cfg.get('stat')), None)
    if table is None:
        table = _discover_ecos_table(tables, cfg)
    stat = table['STAT_CODE']
    cycle = cfg['cycle']
    now = date.today()
    end = (f'{now.year}Q{(now.month - 1) // 3 + 1}' if cycle == 'Q'
           else now.strftime('%Y%m%d' if cycle == 'D' else '%Y%m'))
    codes = cfg.get('item_codes') or _select_ecos_codes(
        _ecos_rows('StatisticItemList', api_key, stat), cfg,
    )
    parts = [stat, cycle, cfg['start'], end, *codes]
    rows = []
    start_row = 1
    while True:
        stop_row = start_row + 999
        try:
            payload = _json(
                f"https://ecos.bok.or.kr/api/StatisticSearch/{quote(api_key, safe='')}"
                f"/json/kr/{start_row}/{stop_row}/" +
                '/'.join(quote(str(p), safe='') for p in parts),
            )
        except ValueError as exc:
            raise ValueError(f'{exc} (ECOS 통계표 {stat}, 항목 {codes})') from None
        try:
            page = _rows(payload, 'StatisticSearch')
        except ValueError as exc:
            raise ValueError(f'{exc} (ECOS 통계표 {stat}, 항목 {codes})') from None
        rows.extend(page)
        if len(page) < 1000:
            break
        start_row = stop_row + 1
    return _points(rows, 'TIME', 'DATA_VALUE', cycle)


def _kosis_search(api_key, search):
    query = urlencode({'method': 'getList', 'apiKey': api_key, 'format': 'json',
                       'jsonVD': 'Y', 'searchNm': search,
                       'resultCount': '100', 'startCount': '1'})
    payload = _json('https://kosis.kr/openapi/statisticsSearch.do?' + query)
    if isinstance(payload, dict) and payload.get('err'):
        raise ValueError('KOSIS: ' + str(payload.get('errMsg', payload['err'])))
    return payload if isinstance(payload, list) else _rows(payload, '')


def _kosis_meta(api_key, org_id, table_id, kind):
    query = urlencode({'method': 'getMeta', 'type': kind, 'apiKey': api_key,
                       'format': 'json', 'orgId': org_id, 'tblId': table_id})
    payload = _json('https://kosis.kr/openapi/statisticsData.do?' + query)
    return payload if isinstance(payload, list) else _rows(payload, '')


def _kosis_data(api_key, org_id, table_id, item_id, dimensions, cycle, start, end):
    params = {'method': 'getList', 'apiKey': api_key, 'format': 'json',
              'orgId': org_id, 'tblId': table_id, 'itmId': item_id,
              'prdSe': cycle, 'startPrdDe': start, 'endPrdDe': end}
    params.update({f'objL{i}': dim for i, dim in enumerate(dimensions, 1)})
    url = 'https://kosis.kr/openapi/Param/statisticsParameterData.do?' + urlencode(params)
    payload = _json(url)
    if isinstance(payload, dict) and (payload.get('err') or payload.get('ERROR')):
        raise ValueError('KOSIS: ' + str(payload.get('errMsg') or payload.get('ERROR')))
    return payload if isinstance(payload, list) else _rows(payload, '')


def _select_kosis_table(rows, cfg):
    candidates = []
    for row in rows:
        title = row.get('TBL_NM') or row.get('tblNm') or row.get('STAT_NM') or ''
        score = _score_text(title, cfg['table_terms'])
        if score and (not row.get('TBL_ID') and not row.get('tblId')):
            continue
        if score:
            recency = str(row.get('END_PRD_DE') or row.get('endPrdDe') or '')
            candidates.append((score, recency, row))
    if not candidates:
        raise ValueError('KOSIS 통계표를 찾지 못했습니다: ' + cfg['search'])
    if cfg['search'] == '품목별 수출입실적':
        preview = [(r.get('ORG_ID'), r.get('TBL_ID'), r.get('TBL_NM'), r.get('STAT_NM'))
                   for _, _, r in sorted(candidates, key=lambda pair: (pair[0], pair[1]), reverse=True)[:12]]
        raise ValueError(f'KOSIS 수출 통계표 후보 확인 필요: {preview}')
    return max(candidates, key=lambda pair: (pair[0], pair[1]))[2]


def _points(rows, time_key, value_key, cycle):
    output = []
    for row in rows:
        raw_date = str(row.get(time_key, ''))
        raw_value = str(row.get(value_key, '')).replace(',', '').strip()
        if cycle == 'Q' and re.fullmatch(r'\d{4}Q[1-4]', raw_date):
            day = f'{raw_date[:4]}-{(int(raw_date[-1]) - 1) * 3 + 1:02d}-01'
        elif cycle == 'M' and re.fullmatch(r'\d{6}', raw_date):
            day = f'{raw_date[:4]}-{raw_date[4:6]}-01'
        elif cycle == 'D' and re.fullmatch(r'\d{8}', raw_date):
            day = f'{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}'
        else:
            day = raw_date
        try:
            value = float(raw_value)
            date.fromisoformat(day)
        except (ValueError, TypeError):
            continue
        output.append([day, value])
    by_date = {}
    for day, value in output:
        if day in by_date and by_date[day] != value:
            raise ValueError(f'한국 통계 API가 같은 기간에 서로 다른 값을 반환했습니다: {day}')
        by_date[day] = value
    return [[day, value] for day, value in sorted(by_date.items())]


def fetch_kosis(sid):
    cfg = KOSIS[sid]
    api_key = os.environ.get('KOSIS_API_KEY', '').strip()
    if not api_key:
        raise ValueError('GitHub Actions secret KOSIS_API_KEY is not configured')
    tables = _kosis_search(api_key, cfg['search'])
    table = _select_kosis_table(tables, cfg)
    org_id = str(table.get('ORG_ID') or table.get('orgId') or '101')
    table_id = table.get('TBL_ID') or table.get('tblId')
    items = _kosis_meta(api_key, org_id, table_id, 'ITM')
    item_choices = [row for row in items
                    if (row.get('ITM_ID') or row.get('itmId'))
                    and str(row.get('OBJ_ID', 'ITEM')).upper() == 'ITEM']
    if not item_choices:
        raise ValueError(f'KOSIS 항목 메타데이터가 없습니다: {table_id}; '
                         f'분류={[(r.get("OBJ_ID"), r.get("ITM_NM")) for r in items[:8]]}')
    item = max(item_choices, key=lambda r: _score_text(
        r.get('ITM_NM') or r.get('itmNm') or '', cfg['item_terms'],
    ))
    if not _score_text(item.get('ITM_NM') or item.get('itmNm') or '', cfg['item_terms']):
        names = [str(r.get('ITM_NM') or r.get('itmNm') or '') for r in item_choices[:12]]
        raise ValueError(f'KOSIS 항목 불일치: {table_id}; 항목={names}')
    item_id = item.get('ITM_ID') or item.get('itmId')
    # KOSIS needs a concrete code for every classifier. ALL is not a valid
    # substitute for the table's own national/total classification code.
    groups = {}
    for row in items:
        obj = str(row.get('OBJ_ID', ''))
        if obj and obj.upper() != 'ITEM' and (row.get('ITM_ID') or row.get('itmId')):
            groups.setdefault(obj, []).append(row)
    dimensions = []
    for obj, choices in sorted(groups.items(), key=lambda entry: entry[0]):
        target = cfg.get('output_target', '')
        preferred = [r for r in choices
                     if str(r.get('ITM_NM') or r.get('itmNm') or '').strip() == target] if target else []
        if not preferred:
            preferred = [r for r in choices if str(r.get('ITM_NM') or r.get('itmNm') or '').strip()
                         in ('전국', '전체', '총계', '계', '총지수', '광공업', '매매',
                             '수출', '15세 이상 전체', '주택매매가격지수')]
        if not preferred:
            names = [str(r.get('ITM_NM') or r.get('itmNm') or '') for r in choices[:12]]
            raise ValueError(f'KOSIS 분류값 불일치: {table_id} {obj}; 값={names}')
        dimensions.append(str(preferred[0].get('ITM_ID') or preferred[0].get('itmId')))
    if not dimensions:
        raise ValueError(f'KOSIS 분류 메타데이터가 없습니다: {table_id}')
    cycle = cfg['cycle']
    now = date.today()
    end = now.strftime('%Y%m' if cycle == 'M' else '%Y')
    try:
        rows = _kosis_data(api_key, org_id, table_id, item_id, dimensions,
                           cycle, cfg['start'], end)
    except ValueError as exc:
        raise ValueError(f'{exc} (표 {table_id}, 항목 {item_id}, 분류 {dimensions})') from None
    points = []
    for row in rows:
        labels = [str(row.get(f'C{i}_NM') or row.get(f'OBJ_NM{i}') or '').strip()
                  for i in range(1, 9)]
        labels = [label for label in labels if label]
        normalized = [re.sub(r'\s+', '', label).lower() for label in labels]
        target = re.sub(r'\s+', '', cfg.get('output_target', '')).lower()
        codes_match = all(str(row.get(f'C{i}', '')) == code
                          for i, code in enumerate(dimensions, 1))
        if codes_match and (not target or target in normalized):
            dt = row.get('PRD_DE') or row.get('prdDe')
            value = row.get('DT') or row.get('dt')
            points.extend(_points([{'TIME': dt, 'DATA_VALUE': value}], 'TIME', 'DATA_VALUE', cycle))
    if not points:
        raise ValueError('KOSIS 응답에서 전국/총계 관측값을 찾지 못했습니다')
    by_date = {}
    for day, value in points:
        if day in by_date and by_date[day] != value:
            raise ValueError(f'KOSIS 분류값이 기간별로 하나로 좁혀지지 않았습니다: {sid} {day}')
        by_date[day] = value
    return [[day, value] for day, value in sorted(by_date.items())]


def fetch_points(sid):
    if sid in ECOS:
        return fetch_ecos(sid)
    if sid in KOSIS:
        return fetch_kosis(sid)
    raise KeyError(sid)

