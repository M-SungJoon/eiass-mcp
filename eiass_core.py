"""EIASS 조회/GIS 핵심 로직 (PyQt 비의존).

`DOHWA EIASS agent.py`(v4.5.5)의 SearchWorker / DetailWorker / MapGeocodeWorker /
ProjectGisLayerLoadWorker(KDPA) 로직을 GUI와 분리한 순수 함수로 이식한 모듈이다.
MCP 서버(`mcp_server.py`)에서 이 모듈의 함수만 호출한다.

검색 필터(협의완료일 범위, 진행상태(완료/진행), 진행구분, 기후변화영향평가, 업종(biz_gubun))와
상세 개요 필드 추출기(_row_value_after_label/_table_value_by_header 등)는 원본
`run_search`/`_extract_*_from_detail_soup` 로직을 그대로 이식했다.

KDPA 인접 조회는 원본에 없던 반경(DWITHIN) 서버측 공간필터를 새로 추가했다(실사용
서버로 검증 완료: WFS 1.0.0 + CQL_FILTER=DWITHIN(geom, POINT(lon lat), radius, meters)).

PyInstaller로 mcp_server.py를 exe화해서 배포할 수 있다(build_mcp.py 참고) — Python
설치 없이도 이 exe 하나만으로 MCP 서버가 동작한다.
"""
import json
import math
import os
import re
import sys
import tempfile
import urllib.parse
from collections import OrderedDict
from datetime import date, datetime

import requests
import urllib3
from bs4 import BeautifulSoup

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REQUEST_TIMEOUT = (8, 30)
MAX_SEARCH_PAGES = 5

EIASS_BASE = 'https://www.eiass.go.kr'
SEARCH_API_URL = EIASS_BASE + '/searchApi/search.do'
FILE_DOWNLOAD_URL = EIASS_BASE + '/common/file/downloadFileByFileSeq.do'

TYPE_NAME_MAP = {
    'S': '전략환경영향평가',
    'M': '소규모환경영향평가',
    'E': '환경영향평가',
    'A': '사후환경영향조사',
    'P': '사전환경성검토',
}

# (라벨, PER용 코드, EIA용 코드). S/M/P(전략환경영향평가/소규모환경영향평가/사전환경성검토)는
# PER코드를, E(환경영향평가)는 EIA코드를 bizGubunCd로 사용한다. A(사후환경영향조사)는 미지원.
BIZ_GUBUN_OPTIONS = [
    ('전체', '', ''),
    ('도시의 개발', 'AAA', 'A'),
    ('산업입지 및 산업단지의 조성', 'AAB', 'B'),
    ('에너지 개발', 'AAC', 'C'),
    ('항만 건설', 'AAD', 'D'),
    ('도로의 건설', 'AAE', 'E'),
    ('수자원의 개발', 'AAF', 'F'),
    ('철도(도시철도 포함)의 건설', 'AAG', 'G'),
    ('공항 또는 비행장의 건설', 'AAH', 'H'),
    ('하천의 이용 및 개발', 'AAI', 'I'),
    ('개간 및 공유수면의 매립', 'AAJ', 'J'),
    ('관광단지의 개발', 'AAK', 'K'),
    ('지역개발/특정지역의 개발', 'AAL', 'M'),
    ('체육시설의 설치', 'AAM', 'N'),
    ('폐기물처리시설 및 분뇨처리시설의 설치', 'AAN', 'O'),
    ('국방군사시설의 설치', 'AAO', 'P'),
    ('토석·모래·자갈·광물 등의 채취', 'AAP', 'Q'),
    ('산지의 개발', 'AAQ', 'L'),
    ('기타', '', 'Z'),
]

KDPA_BASE_URL = 'https://www.kdpa.kr'
KDPA_WFS_URL = KDPA_BASE_URL + '/geoserver/wfs'
KDPA_DEFAULT_VERSION = '2025_ver'
KDPA_DEFAULT_LAYER_DEFS = [
    {'name': '최신 보호지역 전체', 'desig': '', 'layer_url': ''},
    {'name': '국립공원', 'desig': '국립공원', 'layer_url': ''},
    {'name': '천연기념물', 'desig': '천연기념물', 'layer_url': ''},
    {'name': '습지보호지역', 'desig': '습지보호지역', 'layer_url': ''},
    {'name': '야생생물보호구역', 'desig': '야생생물보호구역', 'layer_url': ''},
    {'name': 'OECM', 'desig': '', 'layer_url': 'oecm'},
]

STAGE_MAP = {
    '조안': '초안', '초안': '초안',
    '본안': '본안',
    '협의의견': '협의의견',
    '협의후': '협의후조치',
    '사후': '사후조사',
    '보완': '보완',
    '변경': '변경',
}


# ── .env 설정 (원본 dotenv_paths()/read_dotenv_values()와 동일 위치를 조회한다) ──

def app_runtime_dir():
    # PyInstaller로 exe화된 경우 __file__은 임시 추출 경로(_MEIPASS)를 가리키므로,
    # exe 옆의 .env를 찾으려면 sys.executable 기준 디렉터리를 써야 한다(원본 앱과 동일).
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def app_local_data_dir():
    base = os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()
    path = os.path.join(base, 'DOHWA EIASS Agent')
    os.makedirs(path, exist_ok=True)
    return path


def dotenv_paths():
    primary = os.path.join(app_runtime_dir(), '.env')
    fallback = os.path.join(app_local_data_dir(), '.env')
    paths = []
    for path in (primary, fallback):
        if path not in paths:
            paths.append(path)
    return paths


def _unquote_dotenv_value(value):
    value = (value or '').strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    return value.strip()


def read_dotenv_values(path):
    values = {}
    if not path or not os.path.exists(path):
        return values
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.lstrip('﻿')
                match = re.match(r'\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$', line)
                if not match:
                    continue
                key, value = match.group(1), match.group(2)
                if '#' in value and not value.lstrip().startswith(('"', "'")):
                    value = value.split('#', 1)[0]
                values[key] = _unquote_dotenv_value(value)
    except OSError:
        pass
    return values


def get_vworld_api_key():
    for path in dotenv_paths():
        values = read_dotenv_values(path)
        key = (values.get('VWORLD_API_KEY') or values.get('VWORLD_KEY') or '').strip()
        if key:
            return key
    return (os.environ.get('VWORLD_API_KEY') or '').strip()


def get_vworld_domain():
    text = ''
    for path in dotenv_paths():
        values = read_dotenv_values(path)
        text = (values.get('VWORLD_DOMAIN') or values.get('VWORLD_API_DOMAIN') or '').strip()
        if text:
            break
    text = re.sub(r'^https?://', '', text, flags=re.IGNORECASE)
    text = text.split('/')[0].strip()
    return text or 'localhost'


class EiassError(RuntimeError):
    pass


def _session():
    s = requests.Session()
    return s


# ── 검색 ──

def _strip_search_highlight_markers(html_text):
    return (html_text or '').replace('<!HS>', '').replace('<!HE>', '')


def _clean_text(node):
    if not node:
        return '-'
    text = re.sub(r'\s+', ' ', node.get_text(separator=' ')).strip()
    return text or '-'


def _find_result_cell(tr, *keywords):
    for kw in keywords:
        cell = tr.find('td', attrs={'data-cell-header': lambda x, kw=kw: x and kw in x})
        if cell:
            return cell
    return None


def _parse_result_progress_cell(cell):
    """진행현황 셀에서 (협의기관, 진행값, 진행상태 '완료'|'진행') 을 뽑는다."""
    text = _clean_text(cell)
    if text == '-':
        return '-', '-', '-'
    status = '-'
    status_match = re.search(r'\((완료|진행)\)\s*$', text)
    if status_match:
        status = status_match.group(1)
    return text, text, status


DEFAULT_PROGRESS_STAGE_KEYS = ('draft', 'report', 'reconsult', 'simple', 'change')


def _normalize_biz_gubun_label(value):
    text = re.sub(r'\s+', ' ', (value or '')).strip()
    if not text or text == '-':
        return '-'
    compact = re.sub(r'\s+', '', text)
    for label, _per_code, _eia_code in BIZ_GUBUN_OPTIONS:
        if label == '전체':
            continue
        if re.sub(r'\s+', '', label) == compact:
            return label
    return text


def _biz_gubun_label_from_eia_code(eia_cd):
    match = re.match(r'^[A-Z]{2}\d{4}([A-Z])\d+', eia_cd or '')
    if not match:
        return '-'
    code = match.group(1)
    for label, _per_code, eia_code in BIZ_GUBUN_OPTIONS:
        if eia_code == code:
            return label
    return '-'


def _biz_gubun_code_for_type(biz_gubun, eval_type_code):
    """biz_gubun(라벨 텍스트, 예: '산업입지 및 산업단지의 조성')을 평가종류별 bizGubunCd로 변환.
    A(사후환경영향조사)는 지원하지 않으므로 항상 빈 문자열."""
    if not biz_gubun or eval_type_code == 'A':
        return ''
    target = re.sub(r'\s+', '', biz_gubun)
    for label, per_code, eia_code in BIZ_GUBUN_OPTIONS:
        if label == '전체' or re.sub(r'\s+', '', label) != target:
            continue
        if eval_type_code == 'E':
            return eia_code
        if eval_type_code in ('S', 'M', 'P'):
            return per_code
    return ''


def _progress_business_exquery(eval_type_code, stage_keys=None):
    """원본 _progress_business_exquery 이식. 사후환경영향조사(A)는 대상 외."""
    selected = set(stage_keys) if stage_keys is not None else set(DEFAULT_PROGRESS_STAGE_KEYS)
    if eval_type_code == 'A':
        return ''
    clauses = []
    if 'draft' in selected:
        clauses.append('<CHOAN:contains:Y>')
    if eval_type_code == 'E':
        bonan_codes = []
        if 'report' in selected:
            bonan_codes.append('A')
        if 'reconsult' in selected:
            bonan_codes.append('B')
        if 'simple' in selected:
            bonan_codes.append('C')
        if bonan_codes:
            clauses.append(f"(<BONAN:contains:Y> <BIZ_TYPE_CD:contains:{'|'.join(bonan_codes)}>)")
        if 'change' in selected:
            clauses.append('<BYUN:contains:Y>')
    elif eval_type_code in ('S', 'M', 'P'):
        bonan_codes = []
        if 'report' in selected:
            bonan_codes.append('0')
        if 'reconsult' in selected:
            bonan_codes.append('A')
        if 'change' in selected:
            bonan_codes.append('B')
        if 'simple' in selected:
            bonan_codes.append('C')
        if bonan_codes:
            clauses.append(f"(<BONAN:contains:Y> <BIZ_TYPE_CD:contains:{'|'.join(bonan_codes)}>)")
    return ' | '.join(clauses)


def _parse_iso_date(value):
    """'YYYY-MM-DD' 문자열을 datetime.date로. 실패 시 None."""
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value).strip(), '%Y-%m-%d').date()
    except ValueError:
        return None


def _date_from_text(text):
    """'2026.05.29' 류 EIASS 표시 날짜에서 date를 뽑는다."""
    if not text or text == '-':
        return None
    m = re.search(r'(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})', text)
    if not m:
        return None
    year, month, day = map(int, m.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _normalize_eiass_date(value):
    text = re.sub(r'\s+', '', value or '').strip()
    if not text or text == '-':
        return '-'
    m = re.fullmatch(r'(\d{4})[.\-/]?(\d{2})(\d{2})', text)
    if m:
        return f'{m.group(1)}.{m.group(2)}.{m.group(3)}'
    m = re.search(r'(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})', text)
    if m:
        return f'{m.group(1)}.{int(m.group(2)):02d}.{int(m.group(3)):02d}'
    return value.strip() if value else '-'


def _completion_date_or_dash(value):
    normalized = _normalize_eiass_date(value)
    date_value = _date_from_text(normalized)
    return date_value.strftime('%Y.%m.%d') if date_value else '-'


def _passes_extra_filters(item, consult_date_from=None, consult_date_to=None, progress_status=None):
    """원본 _passes_extra_filters 이식. 서버측 년도 필터는 성기므로 클라이언트에서 정확히 재검증한다."""
    if progress_status and item.get('progress_status') != progress_status:
        return False
    start, end = consult_date_from, consult_date_to
    if start or end:
        if item.get('type') == '사후환경영향조사':
            year_match = re.search(r'\d{4}', item.get('date', '') or item.get('comp_date', '') or '')
            if not year_match:
                return False
            survey_year = int(year_match.group(0))
            if start and survey_year < start.year:
                return False
            if end and survey_year > end.year:
                return False
            return True
        date_value = _date_from_text(item.get('comp_date', '')) or _date_from_text(item.get('date', ''))
        if not date_value:
            return True
        if start and date_value < start:
            return False
        if end and date_value > end:
            return False
    return True


def search_projects(keyword='', type_codes=None, agency_code='', max_pages=MAX_SEARCH_PAGES, session=None,
                     consult_date_from=None, consult_date_to=None, progress_status='', climate_filter='',
                     progress_stage_keys=None, biz_gubun=''):
    """EIASS 사업 검색. 원본 앱(run_search)의 협의완료일 범위/진행상태/기후변화영향평가/
    진행구분/업종(biz_gubun) 필터를 그대로 지원한다.

    Args:
        keyword: 사업명 등 포함검색 키워드. 비워도 다른 필터(협의일자/진행상태/기관)만으로 검색 가능.
        type_codes: None이면 5개 평가종류(S/M/E/A/P) 전체.
        agency_code: 협의기관 코드.
        consult_date_from/consult_date_to: 'YYYY-MM-DD' 문자열. 협의완료일(사후조사는 조사년도)
            기준 범위 필터. 서버에는 연도 단위로만 먼저 필터링을 걸고, 정확한 날짜 비교는
            결과를 받은 뒤 클라이언트에서 다시 검증한다(원본과 동일한 2단계 필터링).
        progress_status: '완료' | '진행' | ''. 진행현황 셀의 '(완료)'/'(진행)' 표기와 매칭.
        climate_filter: 'Y' | 'N' | ''. 기후변화영향평가 대상 여부(사후조사 제외).
        progress_stage_keys: 진행구분 부분집합 {'draft','report','reconsult','simple','change'}.
            None이면 원본 UI 기본값과 동일하게 전체 선택.
        biz_gubun: BIZ_GUBUN_OPTIONS의 라벨 텍스트(예: '산업입지 및 산업단지의 조성'). 사후환경영향조사(A)는
            지원하지 않으므로 target_types에 A만 있으면 결과가 비게 된다.
    반환: [{'type','name','agency','date','comp_date','progress_status','biz_gubun',
            'view_type','eia_cd','revirpt_seq'}, ...]
    """
    keyword = (keyword or '').strip()
    biz_gubun = (biz_gubun or '').strip()
    consult_start = _parse_iso_date(consult_date_from)
    consult_end = _parse_iso_date(consult_date_to)
    has_consult_date_filter = bool(consult_start or consult_end)
    if (not keyword and not agency_code and not has_consult_date_filter
            and not progress_status and not climate_filter and not biz_gubun):
        raise EiassError('keyword, agency_code, consult_date_from/to, progress_status, climate_filter, '
                          'biz_gubun 중 하나 이상은 지정해야 합니다.')

    target_types = list(type_codes) if type_codes else ['S', 'E', 'A', 'M', 'P']
    if climate_filter:
        target_types = [t for t in target_types if t != 'A']
    if biz_gubun:
        known_labels = {label for label, _per, _eia in BIZ_GUBUN_OPTIONS if label != '전체'}
        if biz_gubun not in known_labels:
            raise EiassError(f"biz_gubun 값 '{biz_gubun}'을(를) 알 수 없습니다. "
                              f"BIZ_GUBUN_OPTIONS 라벨과 정확히 일치해야 합니다: {', '.join(sorted(known_labels))}")
        if not any(_biz_gubun_code_for_type(biz_gubun, t) for t in target_types):
            raise EiassError(f"'{biz_gubun}'은(는) 선택한 평가종류(target_types={target_types})에서 사용할 수 "
                              f"없습니다 (사후환경영향조사는 업종 필터를 지원하지 않습니다).")
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'X-Requested-With': 'XMLHttpRequest',
        'Origin': EIASS_BASE,
        'Referer': EIASS_BASE + '/biz/base/info/perList.do',
    }

    # eiass.go.kr은 인증서 체인 문제로 원본 앱도 verify=False로 접속한다.
    s = session or _session()
    s.get(EIASS_BASE + '/', headers={'User-Agent': headers['User-Agent']}, verify=False, timeout=REQUEST_TIMEOUT)

    results = []
    seen_keys = set()
    for t in target_types:
        payload = {
            'query': keyword,
            'collection': 'business',
            'currentPage': '1',
            'sort': 'DATE/DESC',
            'listCount': '100',
        }
        url_dict = {}
        if t == 'S':
            url_dict = {'alias': '2', 'perssGubn': 'S', 'openFl': 'Y'}
            payload['viewName'] = '/eiass/user/biz/base/info/searchListPer_searchApi'
        elif t == 'M':
            url_dict = {'alias': '2', 'perssGubn': 'M', 'openFl': 'Y'}
            payload['viewName'] = '/eiass/user/biz/base/info/searchListPer_searchApi'
        elif t == 'E':
            url_dict = {'alias': '1', 'openFl': 'Y'}
            payload['viewName'] = '/eiass/user/biz/base/info/searchListEia_searchApi'
            payload['sort'] = 'DATE/DESC,APPLY_DT/DESC'
        elif t == 'A':
            url_dict = {'alias': '3', 'approvFls': '3', 'openFl': 'Y'}
            payload['viewName'] = '/eiass/user/biz/base/info/searchListAfter_searchApi'
        elif t == 'P':
            url_dict = {'alias': '2', 'perssGubn': 'P', 'openFl': 'Y'}
            payload['viewName'] = '/eiass/user/biz/base/info/searchListPer_searchApi'
        else:
            continue

        biz_gubun_code = _biz_gubun_code_for_type(biz_gubun, t)
        if biz_gubun and not biz_gubun_code:
            continue  # 이 평가종류는 업종 필터를 지원하지 않음(A) → 건너뜀

        if progress_status:
            url_dict['completeFl'] = progress_status
        progress_exquery = _progress_business_exquery(t, progress_stage_keys)
        if progress_exquery:
            url_dict['businessExquery'] = progress_exquery
        if agency_code:
            url_dict['orgnCd'] = agency_code
        if biz_gubun_code:
            url_dict['bizGubunCd'] = biz_gubun_code
        if climate_filter and t != 'A':
            url_dict['whrChFl'] = climate_filter
        if t == 'A':
            if consult_start:
                url_dict['ivgtSYear'] = str(consult_start.year)
            if consult_end:
                url_dict['ivgtEYear'] = str(consult_end.year)
        else:
            if consult_start:
                url_dict['rSYear'] = str(consult_start.year)
            if consult_end:
                url_dict['rEYear'] = str(consult_end.year)

        payload['urlString'] = '&' + urllib.parse.urlencode(url_dict)

        for page_no in range(1, max_pages + 1):
            payload['currentPage'] = str(page_no)
            res = s.post(SEARCH_API_URL, data=payload, headers=headers, timeout=REQUEST_TIMEOUT, verify=False)
            if res.status_code != 200:
                break
            soup = BeautifulSoup(_strip_search_highlight_markers(res.text), 'html.parser')
            rows = soup.select('table.disTm tbody tr')
            if not rows:
                break

            for tr in rows:
                title_td = tr.find('td', class_='title')
                if not title_td or not title_td.find('a'):
                    continue
                link = title_td.find('a')
                m = re.search(r"view\('([^']+)',\s*'([^']+)',\s*'([^']+)'", link.get('href', ''))
                if not m:
                    continue
                result_key = (m.group(1), m.group(2), m.group(3), t)
                if result_key in seen_keys:
                    continue
                seen_keys.add(result_key)

                progress_td = tr.find('td', class_='td_prog') or _find_result_cell(tr, '진행현황')
                _parsed_agency, _progress_value, progress_status_val = _parse_result_progress_cell(progress_td)
                agency_td = _find_result_cell(tr, '협의기관')
                date_td = _find_result_cell(tr, '접수일', '조사년도')
                comp_td = _find_result_cell(tr, '완료일', '조사기간')
                biz_td = _find_result_cell(tr, '사업유형', '사업구분', '사업분야')
                if agency_td:
                    direct = agency_td.find(string=True, recursive=False)
                    agency_val = direct.strip() if direct and direct.strip() else agency_td.get_text(strip=True)
                else:
                    agency_val = _parsed_agency

                biz_val = _clean_text(biz_td)
                if biz_val == '-' and biz_gubun and biz_gubun != '전체':
                    biz_val = biz_gubun
                if biz_val == '-' and t == 'E':
                    biz_val = _biz_gubun_label_from_eia_code(m.group(2))
                biz_val = _normalize_biz_gubun_label(biz_val)

                item_data = {
                    'type': TYPE_NAME_MAP[t],
                    'name': link.text.strip(),
                    'agency': agency_val or '-',
                    'date': _clean_text(date_td),
                    'comp_date': _clean_text(comp_td),
                    'progress_status': progress_status_val,
                    'biz_gubun': biz_val or '-',
                    'view_type': m.group(1),
                    'eia_cd': m.group(2),
                    'revirpt_seq': m.group(3),
                }
                if not _passes_extra_filters(item_data, consult_start, consult_end, progress_status):
                    continue
                results.append(item_data)
            if len(rows) < 100:
                break
    return results


# ── 상세조회 (개요 필드 + 단계별 첨부문서) ──

def _get_after_detail_text(soup, *keywords):
    for kw in keywords:
        el = soup.find(lambda t: t.name == 'td' and t.get('class') == ['head']
                        and t.get_text(strip=True) == kw)
        if el:
            sib = el.find_next_sibling('td')
            val = _clean_text(sib)
            if val != '-':
                return val
    return '-'


STRUCTURAL_LABEL_VALUES = {
    '소재지', '위치', '면적', '규모', 'B', 'L', '구분',
    '사업시행자명', '승인기관명', '평가대행자', '접수일', '통보일',
}


def _detail_rows(table):
    rows = []
    for tr in table.find_all('tr'):
        cells = [
            re.sub(r'\s+', ' ', cell.get_text(' ', strip=True)).strip()
            for cell in tr.find_all(['th', 'td'])
        ]
        if cells:
            rows.append(cells)
    return rows


def _row_value_after_label(rows, *labels):
    normalized_labels = {re.sub(r'\s+', '', label) for label in labels}
    for row in rows:
        for idx, cell in enumerate(row[:-1]):
            label = re.sub(r'\s+', '', cell.replace(':', ''))
            if label in normalized_labels:
                value = row[idx + 1].strip()
                if value and value != '-' and re.sub(r'\s+', '', value) not in normalized_labels:
                    return value
    return '-'


def _table_value_by_header(soup, header_label):
    """헤더행-값행이 별도 행으로 나뉜(가로 헤더) 표 구조에서 header_label 열의 값을 찾는다."""
    target = re.sub(r'\s+', '', header_label)
    for table in soup.find_all('table'):
        rows = _detail_rows(table)
        for idx, row in enumerate(rows[:-1]):
            headers = [re.sub(r'\s+', '', cell) for cell in row]
            if target not in headers:
                continue
            col = headers.index(target)
            value_row = rows[idx + 1]
            if col < len(value_row):
                value = value_row[col].strip()
                compact = re.sub(r'\s+', '', value)
                if value and value != '-' and compact not in STRUCTURAL_LABEL_VALUES and compact != target:
                    return value
            if header_label == '소재지':
                for value in value_row:
                    normalized = _normalize_detail_value(value, 'location')
                    if normalized != '-':
                        return value
    return '-'


def _extract_address_fragment(text):
    text = re.sub(r'\s+', ' ', text or '').strip()
    if not text or text == '-':
        return '-'
    compact = re.sub(r'\s+', '', text)
    if compact in {'소재지', '위치', '면적', '규모', 'B', 'L', '구분'}:
        return '-'
    text = re.sub(r'^(사업위치|소재지|위치|주소)\s*(표)?\s*', '', text).strip()
    province_pattern = (
        r'(서울특별시|서울시|부산광역시|부산시|대구광역시|대구시|인천광역시|인천시|'
        r'광주광역시|광주시|대전광역시|대전시|울산광역시|울산시|세종특별자치시|세종시|'
        r'경기도|강원특별자치도|강원도|충청북도|충북|충청남도|충남|전북특별자치도|전라북도|전북|'
        r'전라남도|전남|경상북도|경북|경상남도|경남|제주특별자치도|제주도)'
    )
    match = re.search(province_pattern, text)
    if match:
        text = text[match.start():].strip()
    elif re.fullmatch(r'(면적|규모|사업규모|사업시행자|시행자|협의기관|승인기관).*', text):
        return '-'
    text = re.split(
        r'\s+(면적|규모|사업규모|사업시행자|시행자|협의기관|승인기관|평가대행|접수일|협의완료일)\s*',
        text, maxsplit=1,
    )[0].strip()
    text = re.sub(r'\s*(일원|지내|주변|인근)\s*$', '', text).strip()
    return text or '-'


def _normalize_detail_value(value, value_type=''):
    value = re.sub(r'\s+', ' ', value or '').strip()
    if not value or value == '-':
        return '-'
    if value_type == 'location':
        value = re.sub(r'^사업위치\s*표\s*(소재지|위치)?\s*', '', value).strip()
        value = re.sub(r'^(소재지|위치)\s+', '', value).strip()
        value = _extract_address_fragment(value)
    elif value_type == 'scale':
        value = re.sub(r'^사업규모\s*표\s*규모\s*', '', value).strip()
        value = re.sub(r'^규모\s+', '', value).strip()
    return value or '-'


def _extract_location_from_detail_soup(soup):
    value = _table_value_by_header(soup, '소재지')
    if value != '-':
        return _normalize_detail_value(value, 'location')
    for table in soup.find_all('table'):
        rows = _detail_rows(table)
        value = _row_value_after_label(rows, '사업위치', '소재지', '위치')
        value = _normalize_detail_value(value, 'location')
        if value != '-':
            return value
    return '-'


def _extract_executor_from_detail_soup(soup):
    for table in soup.find_all('table'):
        rows = _detail_rows(table)
        value = _row_value_after_label(rows, '사업시행자명', '사업시행자', '시행자', '사업자')
        if value != '-':
            value = re.sub(r'^사업시행자\s*표\s*', '', value).strip()
            nested = re.search(r'사업시행자명\s+(.+)$', value)
            if nested:
                value = nested.group(1).strip()
            return value
    return '-'


def _extract_approval_from_detail_soup(soup):
    for table in soup.find_all('table'):
        rows = _detail_rows(table)
        value = _row_value_after_label(rows, '승인기관명')
        if value != '-':
            return value
    for table in soup.find_all('table'):
        rows = _detail_rows(table)
        value = _row_value_after_label(rows, '승인기관')
        if value == '-':
            continue
        nested = re.search(r'승인기관명\s+(.+)$', value)
        if nested:
            return nested.group(1).strip()
        return value
    return '-'


def _writer_context_text(table):
    parts = []
    caption = table.find('caption')
    if caption:
        parts.append(caption.get_text(' ', strip=True))
    heading = table.find_previous(['h2', 'h3', 'h4', 'h5', 'strong', 'span', 'p', 'td', 'th'])
    if heading:
        parts.append(heading.get_text(' ', strip=True))
    parts.append(table.get_text(' ', strip=True)[:160])
    return re.sub(r'\s+', ' ', ' '.join(parts)).strip()


def _extract_report_writer_affiliation_from_detail_soup(soup):
    """평가서작성자정보의 본안 작성자 소속을 우선 사용하고, 본안이 없으면 초안 작성자 소속을 사용한다."""
    draft_fallback = '-'
    for table in soup.find_all('table'):
        rows = _detail_rows(table)
        value = _row_value_after_label(rows, '소속')
        if value == '-':
            continue
        context = _writer_context_text(table)
        compact = re.sub(r'\s+', '', context)
        if '작성자정보' not in compact or '보완' in compact:
            continue
        if '본안' in compact:
            return value
        if draft_fallback == '-' and '초안' in compact:
            draft_fallback = value
    return draft_fallback


def _extract_after_writer_company_from_detail_soup(soup):
    preferred = []
    fallback = []
    for table in soup.find_all('table'):
        rows = _detail_rows(table)
        value = _row_value_after_label(rows, '업체명')
        if value == '-':
            continue
        context = re.sub(r'\s+', '', _writer_context_text(table))
        if '사후환경영향조사작성자정보' in context:
            preferred.append(value)
        elif '작성자정보' in context or '업무담당자정보' in context:
            fallback.append(value)
    if preferred:
        return preferred[0]
    if fallback:
        return fallback[0]
    return '-'


def _format_after_survey_period(value):
    text = re.sub(r'\s+', ' ', value or '').strip()
    if not text or text == '-':
        return '-'
    text = re.sub(r'^(환경영향\s*)?조사기간(\(금회\))?\s*[:：]?\s*', '', text).strip()
    text = re.sub(
        r'(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일?',
        lambda m: f'{m.group(1)}.{int(m.group(2)):02d}.{int(m.group(3)):02d}',
        text,
    )
    dates = []
    for m in re.finditer(r'(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})', text):
        dates.append(f'{m.group(1)}.{int(m.group(2)):02d}.{int(m.group(3)):02d}')
    if len(dates) >= 2:
        return f'조사기간 {dates[0]} ~ {dates[1]}'
    if len(dates) == 1:
        return f'조사기간 {dates[0]}'
    return f'조사기간 {text}' if text else '-'


def _extract_evaluator_from_detail_soup(soup):
    main_writer = _extract_report_writer_affiliation_from_detail_soup(soup)
    if main_writer != '-':
        return main_writer
    for table in soup.find_all('table'):
        rows = _detail_rows(table)
        direct = _row_value_after_label(rows, '평가대행자정보', '평가대행자', '평가대행자명', '평가서작성자정보')
        if direct != '-':
            return direct
    for table in soup.find_all('table'):
        rows = _detail_rows(table)
        value = _row_value_after_label(rows, '소속')
        if value != '-' and re.search(r'\(주\)|㈜|기술|엔지니어링|환경|종합', value):
            return value
    return '-'


def _extract_stage_dates_from_detail_soup(soup):
    """단계별(초안/본안) 접수일/통보일 표에서 접수일과 잠정 통보일을 뽑는다.
    최종 협의완료일은 _extract_consult_result_from_detail_soup가 더 정확하게 판단한다."""
    for table in soup.find_all('table'):
        rows = _detail_rows(table)
        if not rows:
            continue
        headers = [re.sub(r'\s+', '', cell) for cell in rows[0]]
        if '단계명' not in headers or '접수일' not in headers:
            continue
        stage_idx = headers.index('단계명')
        recv_idx = headers.index('접수일')
        notice_idx = None
        for idx, header in enumerate(headers):
            if '통보일' in header or '협의완료일' in header:
                notice_idx = idx
                break
        selected = None
        for preferred in ('본안', '초안'):
            for row in rows[1:]:
                if stage_idx < len(row) and preferred in row[stage_idx]:
                    selected = row
                    break
            if selected:
                break
        if selected:
            recv = selected[recv_idx] if recv_idx < len(selected) else '-'
            comp = selected[notice_idx] if notice_idx is not None and notice_idx < len(selected) else '-'
            return _normalize_eiass_date(recv), _normalize_eiass_date(comp)
    return '-', '-'


def _extract_consult_result_from_detail_soup(soup):
    """'협의완료일/협의결과' 표는 실제로 협의가 완료된 경우에만 사이트가 렌더링하므로,
    이 표의 존재 여부로 협의결과 유무를 판단하고 같은 행에서 날짜/결과를 함께 읽는다."""
    for table in soup.find_all('table'):
        header_idx = None
        for tr in table.find_all('tr'):
            cells = tr.find_all(['th', 'td'])
            if not cells:
                continue
            texts = [c.get_text(strip=True) for c in cells]
            if header_idx is None:
                compact = [re.sub(r'\s+', '', t) for t in texts]
                if '협의완료일' in compact and '협의결과' in compact:
                    header_idx = {'date': compact.index('협의완료일'), 'result': compact.index('협의결과')}
                continue
            date_val = texts[header_idx['date']] if header_idx['date'] < len(texts) else '-'
            result_val = texts[header_idx['result']] if header_idx['result'] < len(texts) else '-'
            return _completion_date_or_dash(date_val), (result_val or '-')
    return '-', '-'


def _format_comp_date_with_result(comp_date, result_text):
    if comp_date == '-' or not comp_date:
        return '-'
    if re.sub(r'\s+', '', result_text or '') == '기타(취하)':
        return f'{comp_date}(취하)'
    return comp_date


def _first_detail_value(*values):
    for value in values:
        if value and value != '-':
            return value
    return '-'


def _parse_after_docs(soup):
    result = OrderedDict()
    files = []
    added_seqs = set()
    html = soup.decode_contents() if hasattr(soup, 'decode_contents') else str(soup)
    for m in re.finditer(r"viewFile\('(\d+)',\s*'([^']+)'\)", html):
        seq, name = m.group(1), m.group(2)
        if seq not in added_seqs:
            added_seqs.add(seq)
            files.append({'seq': seq, 'name': name, 'is_pdf': name.lower().endswith('.pdf')})
    if files:
        result['사후조사'] = OrderedDict([('원문', files)])
    return result


def _normalize_doc_stage(text):
    text = re.sub(r'\s+', ' ', text or '').strip()
    if not text:
        return ''
    text = re.sub(r'\s*작성자\s*정보.*$', '', text).strip()
    text = re.sub(r'\s*표\s*-.*$', '', text).strip()
    text = re.sub(r'\s*표$', '', text).strip()
    text = re.sub(r'\s+(평가서|검토서|보고서|문서|자료)$', '', text).strip()
    if text in ('사업정보', '사업명 및 기관명', '결정내용 공개', '주민의견수렴결과'):
        return ''
    if not re.search(r'초안|본안|보완|변경|협의의견|협의후|사후|원문', text):
        return ''
    return text


def _find_doc_stage_from_dom(link):
    table = link.find_parent('table')
    if table:
        caption = table.find('caption')
        stage = _normalize_doc_stage(caption.get_text(' ', strip=True) if caption else '')
        if stage:
            return stage
        heading = table.find_previous(['h4', 'h3', 'h5'])
        while heading:
            stage = _normalize_doc_stage(heading.get_text(' ', strip=True))
            if stage:
                return stage
            heading = heading.find_previous(['h4', 'h3', 'h5'])
    for heading in link.find_all_previous(['h4', 'h3', 'h5'], limit=20):
        stage = _normalize_doc_stage(heading.get_text(' ', strip=True))
        if stage:
            return stage
    return ''


def _parse_docs_by_stage(soup):
    added_seqs = set()
    result = OrderedDict()
    for link in soup.find_all('a', href=True):
        href = link.get('href', '')
        file_seq = None
        if 'FILE_SEQ=' in href:
            m = re.search(r'FILE_SEQ=(\d+)', href)
            if m:
                file_seq = m.group(1)
        elif re.search(r'(?i)file|down|view', href) or 'javascript:' in href:
            m = re.search(r"\('?(\d{5,})'?", href)
            if m:
                file_seq = m.group(1)
        if not file_seq or file_seq in added_seqs:
            continue

        title = link.get_text(strip=True)
        if not title:
            img = link.find('img')
            title = img.get('alt', '첨부문서') if img else ''
        if not title:
            continue

        category = '기타'
        tr = link.find_parent('tr')
        if tr:
            for cell in tr.find_all(['th', 'td']):
                t = re.sub(r'\s+', ' ', cell.get_text()).strip()
                if t and t != title:
                    category = t
                    break

        stage = _find_doc_stage_from_dom(link)
        pm = re.match(r'\(([^)]+)\)', title.strip())
        if not stage and pm:
            prefix = pm.group(1).strip()
            stage = STAGE_MAP.get(prefix, prefix)
        if not stage:
            stage = '원문'

        added_seqs.add(file_seq)
        result.setdefault(stage, OrderedDict()).setdefault(category, []).append(
            {'seq': file_seq, 'name': title, 'is_pdf': title.lower().endswith('.pdf')}
        )
    return result


def get_project_detail(view_type, eia_cd, revirpt_seq, session=None):
    """사업 개요 필드 + 단계별 첨부문서(stage_docs, '협의의견' 포함) 조회.

    반환: {'fields': {...}, 'stage_docs': OrderedDict{stage: {category: [{'seq','name','is_pdf'}]}}}
    """
    s = session or _session()

    if view_type == 'after':
        res = s.post(
            EIASS_BASE + '/biz/base/info/afterInfo.do',
            data={'EIA_CD': eia_cd, 'AES_SEQ': revirpt_seq},
            headers={'User-Agent': 'Mozilla/5.0', 'Referer': EIASS_BASE + '/'},
            timeout=REQUEST_TIMEOUT, verify=False,
        )
        soup = BeautifulSoup(res.text, 'html.parser')

        def get_td_text(*keywords):
            return _get_after_detail_text(soup, *keywords)

        fields = {
            'name': get_td_text('사업명'),
            'agency': get_td_text('협의기관'),
            'approval': get_td_text('승인기관명', '승인기관'),
            'location': _normalize_detail_value(
                _first_detail_value(get_td_text('위치'), get_td_text('소재지'), get_td_text('사업위치')),
                'location',
            ),
            'scale': _normalize_detail_value(
                _first_detail_value(get_td_text('규모'), get_td_text('사업규모')), 'scale',
            ),
            'executor': _first_detail_value(get_td_text('사업시행자'), get_td_text('시행자'), get_td_text('사업자')),
            'evaluator': _first_detail_value(
                _extract_after_writer_company_from_detail_soup(soup),
                get_td_text('평가대행자정보'), get_td_text('평가대행자'),
                get_td_text('평가대행자명'), get_td_text('조사기관명'),
            ),
            'recv_date': get_td_text('조사년도'),
            'comp_date': _format_after_survey_period(get_td_text('환경영향 조사기간(금회)')),
        }
        return {'fields': fields, 'stage_docs': _parse_after_docs(soup)}

    if view_type == 'eia':
        detail_url = f'{EIASS_BASE}/biz/base/info/eiaInfo.do?EIA_CD={eia_cd}&REVIRPT_SEQ={revirpt_seq}'
    else:
        detail_url = f'{EIASS_BASE}/biz/base/info/perInfo.do?PER_CD={eia_cd}'

    res = s.get(detail_url, headers={'User-Agent': 'Mozilla/5.0', 'Origin': EIASS_BASE}, timeout=REQUEST_TIMEOUT, verify=False)
    soup = BeautifulSoup(res.text, 'html.parser')

    def get_detail_label(*keywords):
        for table in soup.find_all('table'):
            value = _row_value_after_label(_detail_rows(table), *keywords)
            if value != '-':
                return value
        return '-'

    stage_recv, _stage_notice = _extract_stage_dates_from_detail_soup(soup)
    consult_complete, consult_result = _extract_consult_result_from_detail_soup(soup)
    fields = {
        'name': get_detail_label('사업명'),
        'agency': get_detail_label('협의기관'),
        'approval': _extract_approval_from_detail_soup(soup),
        'location': _extract_location_from_detail_soup(soup),
        'scale': _normalize_detail_value(get_detail_label('규모', '사업규모'), 'scale'),
        'executor': _extract_executor_from_detail_soup(soup),
        'evaluator': _extract_evaluator_from_detail_soup(soup),
        'recv_date': stage_recv if stage_recv != '-' else get_detail_label('접수일자', '접수일'),
        'comp_date': _format_comp_date_with_result(consult_complete, consult_result),
    }
    return {'fields': fields, 'stage_docs': _parse_docs_by_stage(soup)}


def download_document_text(file_seq, session=None, max_chars=20000):
    """첨부 PDF(FILE_SEQ)를 다운로드해 텍스트를 추출한다. fitz(PyMuPDF) 필요."""
    if fitz is None:
        raise EiassError('PyMuPDF(fitz)가 설치되어 있지 않아 PDF 텍스트를 추출할 수 없습니다.')
    s = session or _session()
    url = f'{FILE_DOWNLOAD_URL}?FILE_SEQ={file_seq}'
    res = s.get(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': EIASS_BASE + '/'},
                timeout=(8, 60), verify=False)
    if res.status_code != 200 or not res.content:
        raise EiassError(f'문서 다운로드 실패: HTTP {res.status_code}')
    doc = fitz.open(stream=res.content, filetype='pdf')
    try:
        parts = [page.get_text() for page in doc]
    finally:
        doc.close()
    text = '\n'.join(parts).strip()
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return {'text': text, 'truncated': truncated, 'pages': len(parts)}


def search_projects_by_document_keyword(
    text_query,
    keyword='',
    type_codes=None,
    agency_code='',
    consult_date_from=None,
    consult_date_to=None,
    progress_status='완료',
    biz_gubun='',
    stages=('협의의견',),
    max_pages=2,
    max_candidates=30,
    session=None,
    snippet_chars=250,
):
    """검색 필터(협의완료일 범위/진행상태 등)로 후보 사업을 뽑은 뒤, 지정한 단계(기본값
    '협의의견')의 첨부 PDF 원문에서 text_query가 포함된 사업만 골라낸다.

    "최근 1년 내 협의완료된 사업 중 협의의견에 원형보전지 관련 내용이 있는 사업"
    같은 요청은 이 함수 하나로 처리한다: search_projects()로 날짜/상태 필터링 후,
    각 후보의 stage_docs에서 대상 단계 문서를 다운로드해 원문에서 text_query를 찾는다.

    주의: 단순 부분문자열(포함) 매칭이다. 동의어("원형보전지" vs "존치녹지" 등)나 문맥상
    유사 내용까지 잡으려면, 이 함수로 1차 후보를 좁힌 뒤 eiass_read_document로 원문을
    받아 AI가 다시 의미 단위로 판단해야 한다. stages를 ('초안','본안') 등으로 넓히면
    검토의견/본문에서도 검색할 수 있지만 다운로드량이 늘어 느려진다.

    반환: {'candidates_total', 'checked', 'skipped_no_target_doc', 'matches': [
        {..search_projects 항목.., 'fields': {...}, 'matched_snippets': [{'file','seq','snippet'}]}
    ]}
    """
    session = session or _session()
    candidates = search_projects(
        keyword, type_codes=type_codes, agency_code=agency_code, max_pages=max_pages, session=session,
        consult_date_from=consult_date_from, consult_date_to=consult_date_to, progress_status=progress_status,
        biz_gubun=biz_gubun,
    )
    matches = []
    checked = 0
    skipped_no_doc = 0
    for item in candidates:
        if checked >= max_candidates:
            break
        checked += 1
        try:
            detail = get_project_detail(item['view_type'], item['eia_cd'], item['revirpt_seq'], session=session)
        except Exception:
            continue
        stage_docs = detail['stage_docs']
        target_files = []
        for stage in stages:
            for files in (stage_docs.get(stage) or {}).values():
                target_files.extend(files)
        if not target_files:
            skipped_no_doc += 1
            continue

        matched_snippets = []
        for f in target_files:
            if not f.get('is_pdf'):
                continue
            try:
                doc = download_document_text(f['seq'], session=session, max_chars=300000)
            except Exception:
                continue
            text = doc['text']
            idx = text.find(text_query)
            if idx == -1:
                continue
            start = max(0, idx - snippet_chars // 2)
            end = min(len(text), idx + len(text_query) + snippet_chars // 2)
            matched_snippets.append({'file': f['name'], 'seq': f['seq'], 'snippet': text[start:end].strip()})
        if matched_snippets:
            matches.append({**item, 'fields': detail['fields'], 'matched_snippets': matched_snippets})
    return {
        'candidates_total': len(candidates),
        'checked': checked,
        'skipped_no_target_doc': skipped_no_doc,
        'matches': matches,
    }


# ── VWorld 지오코딩 ──

def _extract_lon_lat_from_json(value):
    if isinstance(value, dict):
        candidates = [('x', 'y'), ('lon', 'lat'), ('lng', 'lat'), ('longitude', 'latitude')]
        for xk, yk in candidates:
            if xk in value and yk in value:
                try:
                    return float(value[xk]), float(value[yk])
                except (TypeError, ValueError):
                    pass
        for key in ('point', 'result', 'response'):
            if key in value:
                found = _extract_lon_lat_from_json(value[key])
                if found:
                    return found
        if 'items' in value and isinstance(value['items'], list) and value['items']:
            return _extract_lon_lat_from_json(value['items'][0])
        # VWorld search API 응답: response.result.point.{x,y}
        if 'response' in value:
            resp = value.get('response') or {}
            result = resp.get('result') or {}
            point = result.get('point') or {}
            if 'x' in point and 'y' in point:
                try:
                    return float(point['x']), float(point['y'])
                except (TypeError, ValueError):
                    pass
    elif isinstance(value, list) and value:
        return _extract_lon_lat_from_json(value[0])
    return None


def geocode_address(query, api_key=None, domain=None, session=None):
    """주소 문자열을 (lon, lat, source) 로 변환한다. 실패 시 None."""
    query = (query or '').strip()
    if not query:
        return None
    api_key = api_key or get_vworld_api_key()
    if not api_key:
        raise EiassError('VWORLD_API_KEY가 설정되어 있지 않습니다 (.env).')
    domain = domain or get_vworld_domain()
    s = session or _session()

    for category, label in [('parcel', 'VWorld 지번'), ('road', 'VWorld 도로명')]:
        params = {
            'service': 'search', 'request': 'search', 'version': '2.0', 'crs': 'EPSG:4326',
            'size': '1', 'page': '1', 'query': query, 'type': 'address', 'category': category,
            'format': 'json', 'errorformat': 'json', 'key': api_key,
        }
        try:
            res = s.get('https://api.vworld.kr/req/search', params=params, timeout=8, verify=True)
            if res.status_code == 200:
                coord = _extract_lon_lat_from_json(res.json())
                if coord:
                    return coord[0], coord[1], label
        except Exception:
            pass

    common = {'q': query, 'output': 'json', 'epsg': 'epsg:4326', 'apiKey': api_key, 'domain': domain}
    for endpoint, label in [('https://apis.vworld.kr/new2coord.do', '도로명'),
                             ('https://apis.vworld.kr/jibun2coord.do', '지번')]:
        try:
            res = s.get(endpoint, params=common, timeout=8, verify=True)
            if res.status_code == 200:
                coord = _extract_lon_lat_from_json(res.json())
                if coord:
                    return coord[0], coord[1], label
        except Exception:
            pass

    for category, label in [('Juso', '주소검색'), ('Jibun', '지번검색')]:
        params = {'query': query, 'category': category, 'pageUnit': '1', 'pageIndex': '1',
                   'output': 'json', 'apiKey': api_key, 'domain': domain}
        try:
            res = s.get('https://map.vworld.kr/search.do', params=params, timeout=8, verify=True)
            if res.status_code == 200:
                coord = _extract_lon_lat_from_json(res.json())
                if coord:
                    return coord[0], coord[1], label
        except Exception:
            pass
    return None


# ── KDPA 보호지역 인접 조회 ──

def _haversine_meters(lon1, lat1, lon2, lat2):
    radius = 6371008.8
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _point_to_ring_min_distance_m(lon, lat, ring):
    """점-폴리곤 경계 최소거리 근사. 위경도를 지점 기준 등장방형(equirectangular) 투영 후
    선분 최근접점을 미터 단위로 계산한다(수 km 이내 오차는 무시할 만한 수준)."""
    lat_rad = math.radians(lat)
    mx = 111320.0 * math.cos(lat_rad)
    my = 110540.0

    def to_xy(pt):
        return ((pt[0] - lon) * mx, (pt[1] - lat) * my)

    px, py = 0.0, 0.0
    best = None
    pts = [to_xy(p) for p in ring]
    for i in range(len(pts)):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % len(pts)]
        dx, dy = bx - ax, by - ay
        seg_len_sq = dx * dx + dy * dy
        if seg_len_sq == 0:
            cx, cy = ax, ay
        else:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len_sq))
            cx, cy = ax + t * dx, ay + t * dy
        d = math.hypot(px - cx, py - cy)
        if best is None or d < best:
            best = d
    return best if best is not None else float('inf')


def _feature_min_distance_m(lon, lat, geometry):
    geom_type = geometry.get('type')
    coords = geometry.get('coordinates')
    if geom_type == 'Polygon':
        rings = coords
    elif geom_type == 'MultiPolygon':
        rings = [ring for poly in coords for ring in poly]
    else:
        return None
    best = None
    for ring in rings:
        d = _point_to_ring_min_distance_m(lon, lat, ring)
        if best is None or d < best:
            best = d
    return best


def _kdpa_headers(json_request=False):
    headers = {'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0', 'Referer': KDPA_BASE_URL + '/'}
    if json_request:
        headers['Content-Type'] = 'application/json; charset=UTF-8'
    return headers


def _kdpa_latest_version(session):
    res = session.post(
        KDPA_BASE_URL + '/getNewVer',
        data=json.dumps({'version_nm': ''}, ensure_ascii=False).encode('utf-8'),
        headers=_kdpa_headers(json_request=True), timeout=15,
    )
    if res.status_code != 200:
        return KDPA_DEFAULT_VERSION
    return str(res.json().get('body') or KDPA_DEFAULT_VERSION)


def find_nearby_protected_areas(lon, lat, radius_m=1000, session=None, version=None):
    """지점 기준 radius_m(미터) 이내 KDPA 보호지역을 조회한다.

    KDPA GeoServer는 WFS 1.1.0 + EPSG:4326에서 위경도 축순서(lat,lon)를 강제하므로,
    축순서가 명확한 WFS 1.0.0(lon,lat)을 사용해 DWITHIN 공간필터로 서버 측 필터링한다.
    반환: [{'name','desig','agency','status_year','distance_m'}, ...] (거리 오름차순)
    """
    s = session or _session()
    version = version or _kdpa_latest_version(s)

    results = []
    seen = set()
    for layer in KDPA_DEFAULT_LAYER_DEFS:
        layer_url = (layer.get('layer_url') or '').strip().lower()
        type_name = f'oecm_{version}' if layer_url == 'oecm' else version
        desig = (layer.get('desig') or '').strip()
        cql = f"DWITHIN(geom, POINT({lon} {lat}), {radius_m}, meters)"
        if desig:
            cql = f"DESIG = '{desig}' AND " + cql
        params = {
            'service': 'WFS', 'version': '1.0.0', 'request': 'GetFeature',
            'typeName': 'korea:' + type_name, 'outputFormat': 'application/json',
            'CQL_FILTER': cql, 'maxFeatures': '200',
        }
        try:
            res = s.get(KDPA_WFS_URL, params=params, headers=_kdpa_headers(), timeout=20, verify=True)
        except Exception:
            continue
        if res.status_code != 200:
            continue
        try:
            data = res.json()
        except ValueError:
            continue
        for feature in data.get('features') or []:
            props = feature.get('properties') or {}
            wdpaid = props.get('WDPAID')
            key = (type_name, wdpaid, props.get('ORIG_NAME') or props.get('NAME'))
            if key in seen:
                continue
            seen.add(key)
            geometry = feature.get('geometry') or {}
            distance_m = _feature_min_distance_m(lon, lat, geometry)
            results.append({
                'name': props.get('ORIG_NAME') or props.get('NAME') or '(이름 없음)',
                'desig': props.get('DESIG') or desig or '보호지역',
                'agency': props.get('MANG_AUTH') or props.get('GOV_TYPE') or 'KDPA',
                'status_year': props.get('STATUS_YR'),
                'distance_m': round(distance_m, 1) if distance_m is not None else None,
            })
    results.sort(key=lambda r: (r['distance_m'] is None, r['distance_m'] if r['distance_m'] is not None else 0))
    return results
