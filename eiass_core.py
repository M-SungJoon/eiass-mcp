"""EIASS 조회/GIS 핵심 로직 (PyQt 비의존).

`DOHWA EIASS agent.py`(v4.5.5)의 SearchWorker / DetailWorker / MapGeocodeWorker /
ProjectGisLayerLoadWorker(KDPA) 로직을 GUI와 분리한 순수 함수로 이식한 모듈이다.
MCP 서버(`mcp_server.py`)에서 이 모듈의 함수만 호출한다.

검색 필터(협의완료일 범위, 진행상태(완료/진행), 진행구분, 기후변화영향평가, 사업유형(biz_gubun))와
상세 개요 필드 추출기(_row_value_after_label/_table_value_by_header 등)는 원본
`run_search`/`_extract_*_from_detail_soup` 로직을 그대로 이식했다.

KDPA 인접 조회는 원본에 없던 반경(DWITHIN) 서버측 공간필터를 새로 추가했다(실사용
서버로 검증 완료: WFS 1.0.0 + CQL_FILTER=DWITHIN(geom, POINT(lon lat), radius, meters)).

PyInstaller로 mcp_server.py를 exe화해서 배포할 수 있다(build_mcp.py 참고) — Python
설치 없이도 이 exe 하나만으로 MCP 서버가 동작한다.
"""
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
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

# 저장소 루트 VERSION 파일과 항상 같은 값으로 맞춰서 커밋할 것(버전 두 곳 중복 관리).
__version__ = '1.3.0'

REQUEST_TIMEOUT = (8, 30)

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

ALL_KNOWN_STAGES = ('초안', '본안', '협의의견', '협의후조치', '사후조사', '보완', '변경')


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


# ── 첨부문서 텍스트 캐시 (file_seq 기준) ──
# 같은 사업/키워드를 다시 조회하거나 CALPUFF→CMAQ처럼 후보군이 겹치는 여러 키워드를
# 연속 조회할 때, 같은 PDF를 매번 재다운로드·재추출하지 않도록 로컬 SQLite에 캐시한다.

_CACHE_LOCK = threading.Lock()


def _cache_db_path():
    return os.path.join(app_local_data_dir(), 'doc_text_cache.sqlite3')


def _cache_connect():
    conn = sqlite3.connect(_cache_db_path(), timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS doc_text_cache (
            file_seq TEXT PRIMARY KEY,
            text TEXT NOT NULL,
            page_offsets TEXT NOT NULL,
            pages INTEGER NOT NULL,
            char_count INTEGER NOT NULL,
            fetched_at TEXT NOT NULL
        )
    ''')
    return conn


def _cache_get(file_seq):
    with _CACHE_LOCK:
        conn = _cache_connect()
        try:
            row = conn.execute(
                'SELECT text, page_offsets, pages FROM doc_text_cache WHERE file_seq = ?', (file_seq,)
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    text, page_offsets_json, pages = row
    return {'text': text, 'page_offsets': json.loads(page_offsets_json), 'pages': pages}


def _cache_put(file_seq, text, page_offsets, pages):
    with _CACHE_LOCK:
        conn = _cache_connect()
        try:
            conn.execute(
                'INSERT OR REPLACE INTO doc_text_cache '
                '(file_seq, text, page_offsets, pages, char_count, fetched_at) VALUES (?, ?, ?, ?, ?, ?)',
                (file_seq, text, json.dumps(page_offsets), pages, len(text), datetime.utcnow().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()


def _page_for_offset(page_offsets, char_index):
    """누적 문자 오프셋 목록에서 char_index가 속한 페이지 번호(1-base)를 추정한다."""
    for i, cum in enumerate(page_offsets or []):
        if char_index < cum:
            return i + 1
    return len(page_offsets) if page_offsets else None


# ── 패턴 캐시 (요청 조건별 "어느 단계에서 매칭이 잘 나왔는지" 기록) ──
# 중요: 이 캐시는 "우선 확인 순서" 힌트로만 쓴다. 검색 범위를 자동으로 줄이는 근거로 쓰면
# 안 된다 — 신뢰도(표본 수)가 낮으면 반드시 low로 표시하고, 호출부(AI)가 이를 이유로
# 임의로 단계를 생략하지 않도록 preview 단계에서 항상 "우선순위일 뿐" 문구를 같이 반환한다.

def _pattern_signature(type_codes, biz_gubun):
    key = json.dumps({'types': sorted(type_codes or []), 'biz_gubun': (biz_gubun or '').strip()},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]


def _pattern_cache_connect():
    conn = sqlite3.connect(_cache_db_path(), timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS pattern_cache (
            signature TEXT NOT NULL,
            stage TEXT NOT NULL,
            checked_count INTEGER NOT NULL DEFAULT 0,
            matched_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (signature, stage)
        )
    ''')
    return conn


def pattern_cache_record(type_codes, biz_gubun, stage_stats):
    """stage_stats: {stage: {'checked': N, 'matched': M}}. 실제로 실행된(확인 완료된)
    검색이 끝날 때마다 호출해 다음 유사 요청의 우선순위 힌트를 누적한다."""
    if not stage_stats:
        return
    sig = _pattern_signature(type_codes, biz_gubun)
    now = datetime.utcnow().isoformat()
    with _CACHE_LOCK:
        conn = _pattern_cache_connect()
        try:
            for stage, stats in stage_stats.items():
                checked = int(stats.get('checked', 0))
                matched = int(stats.get('matched', 0))
                if checked <= 0:
                    continue
                conn.execute('''
                    INSERT INTO pattern_cache (signature, stage, checked_count, matched_count, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(signature, stage) DO UPDATE SET
                        checked_count = checked_count + excluded.checked_count,
                        matched_count = matched_count + excluded.matched_count,
                        updated_at = excluded.updated_at
                ''', (sig, stage, checked, matched, now))
            conn.commit()
        finally:
            conn.close()


def pattern_cache_lookup(type_codes, biz_gubun, min_samples_for_medium=5, min_samples_for_high=20):
    """(평가종류, 사업유형) 조합 기준 과거 기록을 매칭률 순으로 반환한다.
    confidence는 표본 수 기준: checked_count < min_samples_for_medium -> 'low'
    (반드시 저신뢰로 취급, 자동 범위축소 근거 금지), 그 이상 -> 'medium'/'high'."""
    sig = _pattern_signature(type_codes, biz_gubun)
    with _CACHE_LOCK:
        conn = _pattern_cache_connect()
        try:
            rows = conn.execute(
                'SELECT stage, checked_count, matched_count FROM pattern_cache WHERE signature = ?', (sig,)
            ).fetchall()
        finally:
            conn.close()
    result = []
    for stage, checked, matched in rows:
        if checked < min_samples_for_medium:
            confidence = 'low'
        elif checked < min_samples_for_high:
            confidence = 'medium'
        else:
            confidence = 'high'
        result.append({
            'stage': stage, 'checked_count': checked, 'matched_count': matched,
            'match_rate': round(matched / checked, 3) if checked else 0.0,
            'confidence': confidence,
        })
    result.sort(key=lambda r: r['match_rate'], reverse=True)
    return result


# ── 키워드 오탐(참고문헌/부록 등) 휴리스틱 ──

_REFERENCE_MARKERS_RE = re.compile(
    r'참고\s*문헌|참고자료|Reference|References|인용\s*문헌|서지사항|부록\s*[0-9IVXA-Z]*\s*[:\.]|Appendix'
)
_CITATION_LIST_RE = re.compile(r'\(\d{4}\)|\[\d+\]|et al\.')


def _is_reference_like_context(text, idx, window=400):
    """매칭 지점 주변이 참고문헌/부록/인용 목록처럼 보이면 True.
    완벽하지 않은 휴리스틱이며, needs_refinement 판단의 입력값 중 하나일 뿐이다."""
    start = max(0, idx - window)
    end = min(len(text), idx + window)
    ctx = text[start:end]
    if _REFERENCE_MARKERS_RE.search(ctx):
        return True
    if len(_CITATION_LIST_RE.findall(ctx)) >= 2:
        return True
    return False


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

# (내부 키, 한글 라벨). 원본 앱 PROGRESS_STAGE_OPTIONS과 동일 — 사업 자체의 진행구분이며
# 첨부문서 stage(초안/본안/협의의견 등)와는 다른 축이다.
PROGRESS_STAGE_OPTIONS = [
    ('draft', '초안'),
    ('report', '평가서'),
    ('reconsult', '재협의'),
    ('simple', '약식평가'),
    ('change', '변경협의'),
]
PROGRESS_STAGE_LABEL_TO_KEY = {label: key for key, label in PROGRESS_STAGE_OPTIONS}
PROGRESS_STAGE_KEY_TO_LABEL = {key: label for key, label in PROGRESS_STAGE_OPTIONS}


def progress_stage_keys_from_labels(labels):
    """한글 라벨 목록(초안/평가서/재협의/약식평가/변경협의)을 search_projects의
    progress_stage_keys(내부 키)로 변환한다. labels가 비어있으면 None(=전체 선택, 원본
    UI 기본값과 동일)을 반환한다."""
    labels = [l for l in (labels or []) if l]
    if not labels:
        return None
    keys, unknown = [], []
    for label in labels:
        key = PROGRESS_STAGE_LABEL_TO_KEY.get(label.strip())
        (keys if key else unknown).append(key or label)
    if unknown:
        raise EiassError(
            f"알 수 없는 진행구분 라벨: {', '.join(unknown)}. "
            f"사용 가능: {', '.join(label for _, label in PROGRESS_STAGE_OPTIONS)}"
        )
    return keys


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


def search_projects(keyword='', type_codes=None, agency_code='', max_pages=0, session=None,
                     consult_date_from=None, consult_date_to=None, progress_status='', climate_filter='',
                     progress_stage_keys=None, biz_gubun=''):
    """EIASS 사업 검색. 원본 앱(run_search)의 협의완료일 범위/진행상태/기후변화영향평가/
    진행구분/사업유형(biz_gubun) 필터를 그대로 지원한다.

    Args:
        keyword: 사업명 등 포함검색 키워드. 비워도 다른 필터(협의일자/진행상태/기관)만으로 검색 가능.
        type_codes: None이면 5개 평가종류(S/M/E/A/P) 전체.
        agency_code: 협의기관 코드.
        max_pages: 평가종류별 최대 조회 페이지 수(1페이지=100건). 0(기본값) 또는 None이면
            무제한 — 검색조건으로 이미 좁혀졌다고 보고, 결과가 100건 미만인 페이지가 나올
            때까지(=더 이상 다음 페이지가 없을 때까지) 끝까지 조회한다. 후보가 아주 많은
            조건이면 호출 시간이 오래 걸릴 수 있다.
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
                              f"없습니다 (사후환경영향조사는 사업유형 필터를 지원하지 않습니다).")
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
            continue  # 이 평가종류는 사업유형 필터를 지원하지 않음(A) → 건너뜀

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

        page_no = 0
        while True:
            page_no += 1
            if max_pages and max_pages > 0 and page_no > max_pages:
                break
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


_DETAIL_CACHE_LOCK = threading.Lock()
_DETAIL_CACHE = {}  # (view_type, eia_cd, revirpt_seq) -> detail dict; 프로세스 수명 동안만 유지(메모리)


def get_project_detail(view_type, eia_cd, revirpt_seq, session=None, use_cache=False):
    """사업 개요 필드 + 단계별 첨부문서(stage_docs, '협의의견' 포함) 조회.

    use_cache=True면 같은 (view_type, eia_cd, revirpt_seq) 재조회 시 재요청하지 않고
    메모리 캐시를 반환한다(CALPUFF→CMAQ처럼 같은 후보군을 다른 키워드로 다시 훑을 때
    search_projects_by_document_keyword가 이 옵션을 켠다). 최신 상태를 원하면 기본값(False)대로
    둔다 — 진행 중인 사업은 문서가 추가될 수 있으므로 eiass_get_project_documents는 기본이 최신조회다.

    반환: {'fields': {...}, 'stage_docs': OrderedDict{stage: {category: [{'seq','name','is_pdf'}]}}}
    """
    cache_key = (view_type, eia_cd, revirpt_seq)
    if use_cache:
        with _DETAIL_CACHE_LOCK:
            cached = _DETAIL_CACHE.get(cache_key)
        if cached is not None:
            return cached

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
        result = {'fields': fields, 'stage_docs': _parse_after_docs(soup)}
        if use_cache:
            with _DETAIL_CACHE_LOCK:
                _DETAIL_CACHE[cache_key] = result
        return result

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
    result = {'fields': fields, 'stage_docs': _parse_docs_by_stage(soup)}
    if use_cache:
        with _DETAIL_CACHE_LOCK:
            _DETAIL_CACHE[cache_key] = result
    return result


def _get_full_document_text(file_seq, session=None):
    """캐시 우선 조회 후 없으면 다운로드+추출한다. 검색용이므로 절대 잘리지 않은 전체
    텍스트를 반환한다(요약/표시용 자르기는 download_document_text에서만 한다)."""
    cached = _cache_get(file_seq)
    if cached:
        return {'text': cached['text'], 'page_offsets': cached['page_offsets'],
                'pages': cached['pages'], 'from_cache': True}
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
    full_text = '\n'.join(parts)
    page_offsets = []
    cursor = 0
    for part in parts:
        cursor += len(part) + 1
        page_offsets.append(cursor)
    try:
        _cache_put(file_seq, full_text, page_offsets, len(parts))
    except Exception:
        pass  # 캐시 저장 실패는 무시(검색 자체는 계속 진행)
    return {'text': full_text, 'page_offsets': page_offsets, 'pages': len(parts), 'from_cache': False}


def _filter_files_by_title(files, title_terms):
    """title_terms(문자열 목록)가 있으면 파일명에 그 중 하나라도 포함된 문서만 남긴다
    (대소문자 무시). 비어 있으면 그대로 반환. 본안/초안 등은 챕터별로 파일이 쪼개져
    있고 파일명에 챕터명이 그대로 들어있으므로(예: '0922 대기질(...).pdf'), 이 필터로
    "대기질/기상 항목만" 같은 요청을 단계 전체 대신 관련 파일 몇 개로 좁힐 수 있다."""
    if not title_terms:
        return files
    terms = [t.lower() for t in title_terms if t]
    if not terms:
        return files
    return [f for f in files if any(t in (f.get('name') or '').lower() for t in terms)]


def download_document_text(file_seq, session=None, max_chars=20000):
    """첨부 PDF(FILE_SEQ)를 다운로드해 텍스트를 추출한다(로컬 캐시 우선). fitz(PyMuPDF) 필요."""
    result = _get_full_document_text(file_seq, session=session)
    text = result['text'].strip()
    truncated = len(text) > max_chars
    return {
        'text': text[:max_chars] if truncated else text,
        'truncated': truncated,
        'pages': result['pages'],
        'from_cache': result['from_cache'],
    }


def preview_document_keyword_search(
    text_queries,
    match_mode='any',
    keyword='',
    type_codes=None,
    agency_code='',
    consult_date_from=None,
    consult_date_to=None,
    progress_status='완료',
    biz_gubun='',
    progress_stage_keys=None,
    stages=('초안', '본안', '보완', '협의의견'),
    doc_title_contains=None,
    max_pages=0,
    inference_notes='',
    sample_detail_count=15,
    session=None,
):
    """실제 문서를 다운로드하지 않고 후보 수/예상 문서 수/과거 패턴 우선순위를 미리 계산해
    확인 문구(confirmation_message)를 만든다. eiass_find_projects_by_document_keyword /
    eiass_start_document_keyword_scan은 confirmed=True 없이 호출되면 이 함수와 동일한
    내용을 반환하고 실제 조회(다운로드)는 하지 않는다 — "실행 전 확인" 게이트.

    inference_notes: 사용자가 직접 말하지 않았는데 AI가 추론/제안해서 좁힌 조건이 있다면
    그 내용을 자연어로 채운다(예: "평가종류를 환경영향평가로만 좁혔습니다 — 사용자는 '산업단지
    사례'라고만 했음"). 비워두면 "AI 추론 조건 없음"으로 취급된다 — 즉 사용자가 말하지 않은
    필터는 기본적으로 전체(None/'')로 두고, 좁힐 때만 이 필드로 그 사실을 명시해야 한다.

    doc_title_contains: 문자열 목록(또는 None). 지정하면 stages 범위 안에서도 파일명에
    그 중 하나라도 포함된 문서만 확인 대상으로 잡는다. 초안/본안/보완 등은 챕터별로 PDF가
    쪼개져 있고 파일명에 챕터명이 그대로 들어있으므로(예: '0922 대기질(...).pdf'), "대기질/
    기상 항목만" 같은 요청은 이 파라미터로 처리한다(단계 전체가 아니라 항목명 문자열로
    문서 자체를 필터링). MCP가 "대기질 항목"을 의미 단위로 이해해서 자르는 게 아니라
    파일명 문자열 매칭이므로, 실제 챕터 파일명 표기와 다른 용어를 쓰면 못 찾을 수 있다 —
    미리보기의 estimated_documents가 기대보다 너무 적/많으면 용어를 조정해야 한다.

    progress_stage_keys: 진행구분(사업 자체의 진행 단계 — 첨부문서 stage와는 다른 축) 부분집합
    {'draft','report','reconsult','simple','change'}. progress_stage_keys_from_labels()로
    한글 라벨(초안/평가서/재협의/약식평가/변경협의)을 변환해서 넘긴다. None이면 원본 UI
    기본값과 동일하게 5개 전부 선택된 것으로 취급한다(=전체, 필터 없음).
    """
    if isinstance(text_queries, str):
        text_queries = [text_queries]
    text_queries = [q for q in (text_queries or []) if q]
    if not text_queries:
        raise EiassError('text_queries에 검색어를 하나 이상 지정해야 합니다.')
    doc_title_contains = [t for t in (doc_title_contains or []) if t]

    session = session or _session()
    candidates = search_projects(
        keyword, type_codes=type_codes, agency_code=agency_code, max_pages=max_pages, session=session,
        consult_date_from=consult_date_from, consult_date_to=consult_date_to, progress_status=progress_status,
        biz_gubun=biz_gubun, progress_stage_keys=progress_stage_keys,
    )
    total = len(candidates)

    # 문서 수는 표본만 상세조회(다운로드 없음, 캐시 활용)해서 추정한다.
    sample = candidates[:max(0, sample_detail_count)]
    sample_doc_counts = []
    for item in sample:
        try:
            detail = get_project_detail(item['view_type'], item['eia_cd'], item['revirpt_seq'],
                                         session=session, use_cache=True)
        except Exception:
            continue
        n = 0
        for st in stages:
            for files in (detail['stage_docs'].get(st) or {}).values():
                pdfs = [f for f in files if f.get('is_pdf')]
                n += len(_filter_files_by_title(pdfs, doc_title_contains))
        sample_doc_counts.append(n)
    avg_docs = (sum(sample_doc_counts) / len(sample_doc_counts)) if sample_doc_counts else None
    estimated_documents = round(avg_docs * total) if avg_docs is not None else None

    pattern = pattern_cache_lookup(type_codes, biz_gubun)
    pattern_note = (
        '과거 유사 조건(같은 평가종류+사업유형 조합) 기록이 있습니다. 아래 우선순위는 참고용 힌트일 뿐이며, '
        '검색 범위를 줄이는 근거로 쓰지 않습니다 — 요청한 stages는 전부 그대로 확인합니다.'
        if pattern else '이 조건 조합(평가종류+사업유형)에 대한 과거 기록이 아직 없습니다.'
    )

    type_label = ', '.join(TYPE_NAME_MAP.get(t, t) for t in type_codes) if type_codes else '전체'
    biz_label = biz_gubun or '전체'
    agency_label = agency_code or '전체'
    date_label = f"{consult_date_from or '제한없음'} ~ {consult_date_to or '제한없음'}"

    doc_scope_value = '/'.join(stages)
    if doc_title_contains:
        doc_scope_value += f" (제목에 {'/'.join(doc_title_contains)} 포함된 문서만)"

    if len(text_queries) == 1:
        keyword_phrase = f"`{text_queries[0]}` 포함"
    else:
        joined = ' 와 '.join(f'`{q}`' for q in text_queries)
        keyword_phrase = f"{joined} " + ('모두 포함' if match_mode == 'all' else '중 하나 포함')

    # 예상 규모에 따라 소규모 즉시조회/백그라운드 스캔 중 어느 도구가 맞는지 힌트를 준다.
    scale = estimated_documents if estimated_documents is not None else total
    recommended_tool = 'eiass_start_document_keyword_scan(백그라운드 스캔)' if scale > 50 \
        else 'eiass_find_projects_by_document_keyword(즉시 조회)'

    progress_stage_label = (
        ', '.join(PROGRESS_STAGE_KEY_TO_LABEL.get(k, k) for k in progress_stage_keys)
        if progress_stage_keys else '전체'
    )
    progress_status_label = {'완료': '완료', '진행': '진행중'}.get(progress_status, '전체')
    # 사용자에게 보여줄 확인 문구는 반드시 이 10개 항목을 이 순서/라벨 그대로 담아야 한다
    # (검색조건 항목 하나도 빠뜨리지 말 것) — 순서/라벨을 바꾸지 마라.
    bullets = [
        f"- 평가종류: `{type_label}`",
        f"- 사업유형: `{biz_label}`",
        f"- 협의기관: `{agency_label}`",
        f"- 협의완료일: `{date_label}`",
        f"- 진행현황: `{progress_status_label}`",
        f"- 진행구분: `{progress_stage_label}`",
        f"- 확인 문서 범위: `{doc_scope_value}`",
        f"- 키워드 매칭: {keyword_phrase}",
        f"- 예상 후보 사업 수: `{total}`건",
    ]
    bullets.append(
        f"- 예상 확인 문서 수: 약 `{estimated_documents}`건 (표본 {len(sample_doc_counts)}건 기준 추정)"
        if estimated_documents is not None else "- 예상 확인 문서 수: 표본 추정 실패(상세조회 오류)"
    )

    notes = []
    if inference_notes:
        notes.append(f"※ 사용자가 직접 말하지 않고 AI가 추론/제안한 조건: {inference_notes}")
    else:
        notes.append("※ AI가 임의로 좁힌 조건 없음 — 언급되지 않은 필터는 전체로 적용했습니다.")
    if doc_title_contains:
        notes.append(
            "※ 문서 제목 필터는 파일명 문자열 매칭입니다(의미 단위 이해 아님) — 실제 챕터 파일명 "
            "표기와 다른 용어를 쓰면 관련 문서를 놓칠 수 있으니, 예상 문서 수가 기대와 다르면 "
            "용어를 조정해서 다시 미리보기 해보세요."
        )
    if pattern:
        top = pattern[0]
        notes.append(
            f"※ 과거 유사 조건에서는 `{top['stage']}` 단계 매칭률이 {top['match_rate']:.0%}"
            f"(신뢰도: {top['confidence']}, 표본 {top['checked_count']}건)로 가장 높았습니다 — "
            f"우선 확인 순서 참고용이며, 다른 단계를 생략하지는 않습니다."
        )

    message_parts = [
        "아래 조건으로 실제 원문 스캔을 진행해도 될까요?",
        "",
        "적용할 검색 조건:",
        "",
        '\n'.join(bullets),
    ]
    if notes:
        message_parts += ["", '\n'.join(notes)]
    message_parts += [
        "",
        f"예상 규모상 `{recommended_tool}`으로 진행하는 게 맞습니다. 승인해주시면 같은 조건에 "
        f"`confirmed=true`를 붙여 실행하고, 결과를 정리해 알려드리겠습니다.",
    ]

    return {
        'candidates_total': total,
        'estimated_documents': estimated_documents,
        'document_count_sample_size': len(sample_doc_counts),
        'pattern_cache': pattern,
        'pattern_cache_note': pattern_note,
        'recommended_execution_tool': recommended_tool,
        'applied_filters': {
            'keyword': keyword or None, 'types': type_codes or 'ALL', 'agency_code': agency_code or 'ALL',
            'consult_date_from': consult_date_from, 'consult_date_to': consult_date_to,
            'progress_status': progress_status or 'ALL', 'biz_gubun': biz_gubun or 'ALL',
            'progress_stage_keys': list(progress_stage_keys) if progress_stage_keys else 'ALL',
        },
        'inference_notes': inference_notes or None,
        'stages_to_check': list(stages),
        'doc_title_contains': doc_title_contains or None,
        'text_queries': text_queries,
        'match_mode': match_mode,
        'confirm_required': True,
        'confirmation_message': '\n'.join(message_parts),
    }


def search_projects_by_document_keyword(
    text_queries,
    match_mode='any',
    keyword='',
    type_codes=None,
    agency_code='',
    consult_date_from=None,
    consult_date_to=None,
    progress_status='완료',
    biz_gubun='',
    progress_stage_keys=None,
    stages=('초안', '본안', '보완', '협의의견'),
    doc_title_contains=None,
    max_pages=0,
    offset=0,
    max_candidates=30,
    session=None,
    snippet_chars=250,
    audit_sample_size=0,
    record_pattern=True,
):
    """검색 필터(협의완료일 범위/진행상태/진행구분 등)로 후보 사업을 뽑은 뒤, 지정한 단계(기본값
    '초안,본안,보완,협의의견' — 협의의견만 우선 확인하면 놓치는 경우가 있어 여러 단계를
    기본으로 함께 확인한다)의 첨부 PDF 원문에서 text_queries 키워드가 있는 사업만 골라낸다.

    이 함수는 실제로 문서를 다운로드하는 "실행" 단계다 — MCP 도구 계층
    (eiass_find_projects_by_document_keyword / eiass_start_document_keyword_scan)은
    confirmed=True가 아니면 이 함수 대신 preview_document_keyword_search를 호출해서
    사용자 확인을 먼저 받도록 게이트를 건다.

    후보 전체(candidates_total)를 한 번에 다 훑지 않고 offset부터 max_candidates개만
    처리한다. 응답의 next_offset을 다음 호출의 offset으로 넘기면 이어서 진행할 수 있다.

    text_queries는 문자열 또는 리스트. 여러 키워드를 한 번의 PDF 다운로드/추출로 함께
    확인한다. match_mode='any'면 하나라도 있으면 매칭, 'all'이면 전부 있어야 매칭.

    같은 file_seq의 PDF/상세조회는 로컬 캐시를 타므로, 같은 후보군을 다른 키워드로
    다시 조회하거나 offset을 이어서 조회할 때 재다운로드하지 않는다.

    doc_title_contains: 문자열 목록(또는 None). stages 범위 안에서도 파일명에 그 중 하나라도
    포함된 문서만 확인한다(대소문자 무시, 단순 문자열 매칭). 초안/본안/보완 등은 챕터별로
    PDF가 쪼개져 있고 파일명에 챕터명이 그대로 들어있으므로(예: '0922 대기질(...).pdf'),
    "모든 단계의 대기질 항목만" 같은 요청은 stages를 넓히고 doc_title_contains=['대기질','기상']
    처럼 지정해서 처리한다 — 단계 전체를 다 열어보지 않고 관련 파일만 연다.

    progress_stage_keys: 진행구분(사업 자체의 진행 단계) 부분집합 {'draft','report','reconsult',
    'simple','change'}. progress_stage_keys_from_labels()로 한글 라벨(초안/평가서/재협의/
    약식평가/변경협의)을 변환해서 넘긴다. None이면 전체(5개 모두)로 취급한다.

    audit_sample_size>0이면, 이번 배치(batch) 중 그만큼을 골라 stages에 포함되지 않은
    다른 단계도 표본 검증한다(결과의 'audit_sample'). "지정 범위 밖은 아예 안 본다"가
    되지 않도록 하는 안전장치이며, 전수조사가 아니므로 없다고 단정하지는 않는다.
    doc_title_contains가 있으면 이 표본 검증에도 동일하게 적용된다.

    record_pattern=True(기본)면 이번 실행 결과(단계별 확인/매칭 건수)를 로컬 패턴
    캐시에 누적한다 — 다음 유사 조건 요청의 preview에서 우선순위 힌트로만 쓰인다.

    주의: 단순 부분문자열(포함) 매칭이다. 동의어나 문맥상 유사 내용까지 잡으려면,
    이 함수로 1차 후보를 좁힌 뒤 eiass_read_document로 원문을 받아 AI가 다시 의미
    단위로 판단해야 한다. 결과의 needs_refinement가 true면 매칭이 과도하거나
    참고문헌/부록 문맥으로 보이는 비율이 높다는 뜻이니, 바로 최종 답을 내지 말고
    사용자에게 문맥 조건 추가 여부를 물어보라.

    반환: {'candidates_total', 'offset', 'checked', 'next_offset', 'has_more',
        'skipped': [{'name','eia_cd','reason'}, ...],
        'matches': [{..search_projects 항목.., 'fields': {...}, 'matched_keywords': [...],
                     'matched_snippets': [{'file','seq','stage','keyword','page_estimate',
                                            'snippet','reference_like'}]}],
        'stage_stats': {stage: {'checked','matched'}},
        'needs_refinement', 'refinement_hint',
        'audit_sample': {...} | None,
        'search_summary': '...'}
    """
    if isinstance(text_queries, str):
        text_queries = [text_queries]
    text_queries = [q for q in (text_queries or []) if q]
    if not text_queries:
        raise EiassError('text_queries에 검색어를 하나 이상 지정해야 합니다.')
    if match_mode not in ('any', 'all'):
        raise EiassError("match_mode는 'any' 또는 'all'이어야 합니다.")
    doc_title_contains = [t for t in (doc_title_contains or []) if t]

    session = session or _session()
    candidates = search_projects(
        keyword, type_codes=type_codes, agency_code=agency_code, max_pages=max_pages, session=session,
        consult_date_from=consult_date_from, consult_date_to=consult_date_to, progress_status=progress_status,
        biz_gubun=biz_gubun, progress_stage_keys=progress_stage_keys,
    )
    total = len(candidates)
    batch = candidates[offset:offset + max_candidates]

    stage_stats = {stage: {'checked': 0, 'matched': 0} for stage in stages}
    matches = []
    skipped = []
    total_snippets = 0
    reference_like_snippets = 0

    for item in batch:
        try:
            detail = get_project_detail(item['view_type'], item['eia_cd'], item['revirpt_seq'],
                                         session=session, use_cache=True)
        except Exception as exc:
            skipped.append({'name': item['name'], 'eia_cd': item['eia_cd'], 'reason': f'상세조회 실패: {exc}'})
            continue
        stage_docs = detail['stage_docs']

        any_stage_has_files = False
        any_stage_has_filtered_files = False
        matched_snippets = []
        matched_keywords = set()
        for stage in stages:
            raw_files = [f for cat_files in (stage_docs.get(stage) or {}).values()
                         for f in cat_files if f.get('is_pdf')]
            if not raw_files:
                continue
            any_stage_has_files = True
            files = _filter_files_by_title(raw_files, doc_title_contains)
            if not files:
                continue
            any_stage_has_filtered_files = True
            stage_stats[stage]['checked'] += 1
            stage_had_match = False
            for f in files:
                try:
                    doc = _get_full_document_text(f['seq'], session=session)
                except Exception as exc:
                    skipped.append({'name': item['name'], 'eia_cd': item['eia_cd'],
                                     'reason': f"{f['name']} 다운로드/추출 실패: {exc}"})
                    continue
                text = doc['text']
                for q in text_queries:
                    idx = text.find(q)
                    if idx == -1:
                        continue
                    matched_keywords.add(q)
                    stage_had_match = True
                    total_snippets += 1
                    ref_like = _is_reference_like_context(text, idx)
                    if ref_like:
                        reference_like_snippets += 1
                    start = max(0, idx - snippet_chars // 2)
                    end = min(len(text), idx + len(q) + snippet_chars // 2)
                    matched_snippets.append({
                        'file': f['name'], 'seq': f['seq'], 'stage': stage, 'keyword': q,
                        'page_estimate': _page_for_offset(doc['page_offsets'], idx),
                        'snippet': text[start:end].strip(),
                        'reference_like': ref_like,
                    })
            if stage_had_match:
                stage_stats[stage]['matched'] += 1

        if not any_stage_has_files:
            skipped.append({'name': item['name'], 'eia_cd': item['eia_cd'],
                             'reason': f"{'/'.join(stages)} 단계에 PDF 첨부문서 없음"})
            continue
        if not any_stage_has_filtered_files:
            skipped.append({'name': item['name'], 'eia_cd': item['eia_cd'],
                             'reason': f"{'/'.join(stages)} 단계에 PDF는 있으나 문서 제목 필터"
                                       f"({'/'.join(doc_title_contains)})에 맞는 문서 없음"})
            continue

        is_match = (matched_keywords == set(text_queries)) if match_mode == 'all' else bool(matched_keywords)
        if is_match:
            matches.append({
                **item, 'fields': detail['fields'],
                'matched_keywords': sorted(matched_keywords), 'matched_snippets': matched_snippets,
            })

    checked = len(batch)
    next_offset = offset + checked if (offset + checked) < total else None

    # 오탐 가능성 휴리스틱: 매칭률이 너무 높거나, 매칭된 문맥 상당수가 참고문헌/부록처럼 보이면
    # 바로 최종 답을 내지 말고 사용자에게 문맥 구체화를 물어보라는 신호를 준다.
    match_rate = (len(matches) / checked) if checked else 0.0
    reference_like_ratio = (reference_like_snippets / total_snippets) if total_snippets else 0.0
    needs_refinement = checked >= 5 and (match_rate > 0.5 or reference_like_ratio > 0.4)
    refinement_hint = None
    if needs_refinement:
        refinement_hint = (
            f"매칭률이 높거나({match_rate:.0%}) 매칭된 문맥 중 참고문헌/부록처럼 보이는 비율이 높습니다"
            f"({reference_like_ratio:.0%}, {reference_like_snippets}/{total_snippets}건). "
            f"최종 답변 전에 실제 의도(예: 특정 단계에서 적용한 모델/기법인지)를 좁히는 문맥 키워드를 "
            f"추가할지 사용자에게 확인하는 것을 권장합니다."
        )

    # scan_scope_audit: 지정 stages 밖의 다른 단계도 이번 배치 중 일부는 표본 검증한다.
    audit_sample = None
    if audit_sample_size > 0:
        other_stages = [s for s in ALL_KNOWN_STAGES if s not in stages]
        if other_stages:
            audit_candidates = batch[:audit_sample_size]
            audit_matches = []
            audit_checked = 0
            for item in audit_candidates:
                try:
                    detail = get_project_detail(item['view_type'], item['eia_cd'], item['revirpt_seq'],
                                                 session=session, use_cache=True)
                except Exception:
                    continue
                files = [f for st in other_stages for cat_files in (detail['stage_docs'].get(st) or {}).values()
                         for f in cat_files if f.get('is_pdf')]
                files = _filter_files_by_title(files, doc_title_contains)
                if not files:
                    continue
                audit_checked += 1
                for f in files[:3]:
                    try:
                        doc = _get_full_document_text(f['seq'], session=session)
                    except Exception:
                        continue
                    text = doc['text']
                    for q in text_queries:
                        idx = text.find(q)
                        if idx != -1:
                            audit_matches.append({'name': item['name'], 'stage': '/'.join(other_stages),
                                                   'file': f['name'], 'keyword': q})
            audit_sample = {
                'other_stages_checked': other_stages,
                'candidates_sampled': len(audit_candidates),
                'documents_checked': audit_checked,
                'matches_found': audit_matches,
                'note': '지정한 stages 밖에서 표본만 검증한 결과입니다. 전수조사가 아니므로 매칭이 '
                        '없어도 완전히 없다고 단정할 수 없습니다.',
            }

    if record_pattern:
        try:
            pattern_cache_record(type_codes, biz_gubun, stage_stats)
        except Exception:
            pass  # 캐시 기록 실패는 검색 결과에 영향 주지 않음

    summary_parts = [
        f"검색조건: 평가종류={type_codes or '전체'}, 사업유형={biz_gubun or '전체'}, "
        f"협의완료일={consult_date_from or '제한없음'}~{consult_date_to or '제한없음'}, "
        f"진행상태={progress_status or '전체'}, "
        f"진행구분={','.join(progress_stage_keys) if progress_stage_keys else '전체'}",
        f"확인범위: {'/'.join(stages)} 단계"
        + (f" 중 제목에 {'/'.join(doc_title_contains)} 포함된 문서만" if doc_title_contains else "")
        + f", 이번 배치 {offset}~{offset + checked - 1} "
        f"(전체 후보 {total}건 중 {checked}건 확인, {'남은 후보 있음' if next_offset is not None else '전체 확인 완료'})",
        f"결과: 매칭 {len(matches)}건 / 미확인·제외 {len(skipped)}건",
    ]
    if audit_sample:
        summary_parts.append(
            f"범위 밖 표본검증: {'/'.join(audit_sample['other_stages_checked'])} 단계 "
            f"{audit_sample['documents_checked']}건 확인, 매칭 {len(audit_sample['matches_found'])}건"
        )

    return {
        'candidates_total': total,
        'offset': offset,
        'checked': checked,
        'next_offset': next_offset,
        'has_more': next_offset is not None,
        'doc_title_contains': doc_title_contains or None,
        'skipped': skipped,
        'matches': matches,
        'stage_stats': stage_stats,
        'needs_refinement': needs_refinement,
        'refinement_hint': refinement_hint,
        'audit_sample': audit_sample,
        'search_summary': ' | '.join(summary_parts),
    }


# ── 조사 결과 CSV 내보내기 ──

# eiass_find_projects_by_document_keyword / eiass_start_document_keyword_scan 결과를
# 사용자에게 보고할 때 반드시 이 5개 컬럼을 이 순서 그대로 담아야 한다(하나도 빠짐없이).
CSV_REPORT_COLUMNS = ['사업명', 'eia_cd', '원문 파일명', '유사내용 페이지번호', '변경 내용 요약']

# eiass_check_project_protected_area_adjacency로 여러 사업을 공간조회한 결과를 보고할 때
# 반드시 이 4개 컬럼을 이 순서 그대로 담아야 한다(하나도 빠짐없이).
CSV_SPATIAL_REPORT_COLUMNS = ['사업명', 'eia_cd', '대상 보호구역', '거리']


def _export_csv_rows(rows, columns, filename, out_dir, default_name_prefix):
    if not rows:
        raise EiassError('rows가 비어 있습니다 — CSV로 내보낼 결과가 없습니다.')
    missing_any = set()
    for row in rows:
        missing_any |= (set(columns) - set(row.keys()))
    if missing_any:
        raise EiassError(
            f"rows 항목에 다음 컬럼이 빠져 있습니다: {', '.join(sorted(missing_any))} "
            f"(필수 컬럼 {len(columns)}개, 이 순서로 전부 포함해야 함: {', '.join(columns)})"
        )

    out_dir = out_dir or os.path.join(os.path.expanduser('~'), 'Downloads')
    os.makedirs(out_dir, exist_ok=True)
    if not filename:
        filename = f"{default_name_prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    if not filename.lower().endswith('.csv'):
        filename += '.csv'
    path = os.path.join(out_dir, filename)

    import csv as _csv
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = _csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, '') for col in columns})
    return path


def export_matches_csv(rows, filename=None, out_dir=None):
    """조사한 사업의 전체 리스트를 CSV 파일로 저장한다.

    rows: dict 리스트. 각 dict는 CSV_REPORT_COLUMNS(사업명/eia_cd/원문 파일명/
    유사내용 페이지번호/변경 내용 요약)를 전부 키로 가지고 있어야 한다. '변경 내용 요약'은
    기계적으로 생성할 수 없으므로 AI가 matched_snippets(원문 발췌)를 근거로 직접 요약해서
    채워 넣어야 한다 — 빈 문자열로 두지 말 것.

    filename: 저장할 파일명(확장자 없으면 .csv 자동 부여). 비우면 타임스탬프로 자동 생성.
    out_dir: 저장 폴더. 비우면 사용자 Downloads 폴더.
    """
    return _export_csv_rows(rows, CSV_REPORT_COLUMNS, filename, out_dir, 'eiass_조사결과')


def export_spatial_matches_csv(rows, filename=None, out_dir=None):
    """공간조회(보호구역 인접) 결과의 전체 사업 리스트를 CSV 파일로 저장한다.

    rows: dict 리스트. 각 dict는 CSV_SPATIAL_REPORT_COLUMNS(사업명/eia_cd/대상 보호구역/거리)를
    전부 키로 가지고 있어야 한다. 한 사업에 인접 보호구역이 여러 개면 행을 나눠서 각각 적는다.

    filename: 저장할 파일명(확장자 없으면 .csv 자동 부여). 비우면 타임스탬프로 자동 생성.
    out_dir: 저장 폴더. 비우면 사용자 Downloads 폴더.
    """
    return _export_csv_rows(rows, CSV_SPATIAL_REPORT_COLUMNS, filename, out_dir, 'eiass_공간조회결과')


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


def _geocode_address_attempts(query, api_key=None, domain=None, session=None):
    """VWorld 여러 API를 순서대로 시도하고, (좌표 또는 None, 시도별 진단 목록)을 함께 반환한다.
    이전에는 모든 HTTP/파싱 예외를 조용히 삼켜서 "결과 없음"과 "API 오류/타임아웃"을
    구분할 수 없었다 — 각 시도의 HTTP 상태·에러·매칭 여부를 attempts에 남겨 진단 가능하게 한다.
    """
    query = (query or '').strip()
    attempts = []
    if not query:
        return None, attempts
    api_key = api_key or get_vworld_api_key()
    if not api_key:
        raise EiassError('VWORLD_API_KEY가 설정되어 있지 않습니다 (.env).')
    domain = domain or get_vworld_domain()
    s = session or _session()

    def record(label, status_code=None, error=None, matched=False):
        entry = {'api': label, 'matched': matched}
        if status_code is not None:
            entry['status_code'] = status_code
        if error is not None:
            entry['error'] = str(error)
        attempts.append(entry)

    def try_get(url, params, label):
        try:
            res = s.get(url, params=params, timeout=8, verify=True)
        except Exception as exc:
            record(label, error=exc)
            return None
        if res.status_code != 200:
            record(label, status_code=res.status_code)
            return None
        try:
            coord = _extract_lon_lat_from_json(res.json())
        except ValueError as exc:
            record(label, status_code=res.status_code, error=exc)
            return None
        if coord:
            record(label, status_code=res.status_code, matched=True)
            return coord
        record(label, status_code=res.status_code)
        return None

    for category, label in [('parcel', 'VWorld 지번'), ('road', 'VWorld 도로명')]:
        params = {
            'service': 'search', 'request': 'search', 'version': '2.0', 'crs': 'EPSG:4326',
            'size': '1', 'page': '1', 'query': query, 'type': 'address', 'category': category,
            'format': 'json', 'errorformat': 'json', 'key': api_key,
        }
        coord = try_get('https://api.vworld.kr/req/search', params, label)
        if coord:
            return (coord[0], coord[1], label), attempts

    common = {'q': query, 'output': 'json', 'epsg': 'epsg:4326', 'apiKey': api_key, 'domain': domain}
    for endpoint, label in [('https://apis.vworld.kr/new2coord.do', '도로명'),
                             ('https://apis.vworld.kr/jibun2coord.do', '지번')]:
        coord = try_get(endpoint, common, label)
        if coord:
            return (coord[0], coord[1], label), attempts

    for category, label in [('Juso', '주소검색'), ('Jibun', '지번검색')]:
        params = {'query': query, 'category': category, 'pageUnit': '1', 'pageIndex': '1',
                   'output': 'json', 'apiKey': api_key, 'domain': domain}
        coord = try_get('https://map.vworld.kr/search.do', params, label)
        if coord:
            return (coord[0], coord[1], label), attempts

    return None, attempts


def geocode_address(query, api_key=None, domain=None, session=None):
    """주소 문자열을 (lon, lat, source) 로 변환한다. 실패 시 None."""
    coord, _attempts = _geocode_address_attempts(query, api_key=api_key, domain=domain, session=session)
    return coord


_PAREN_RE = re.compile(r'\(([^()]*)\)')
_EUPMYEONDONG_RE = re.compile(r'^(.*?[가-힣]+(?:읍|면|동|가))(?=\s|\(|$)')


def _location_candidates(location):
    """사업지 주소 문자열(EIASS 원문, 도로명·지번이 괄호로 섞인 복합 표기일 수 있음)에서
    지오코딩을 시도할 후보 쿼리를 우선순위 순서로 만든다.

    예: '경기도 김포시 대곶면 (천호로 210) 대벽리 662-1번지' →
        1. 원문 그대로
        2. 개행/중복 공백만 정리한 원문
        3. 괄호 안 도로명 + 앞쪽 행정구역 접두어 → '경기도 김포시 대곶면 천호로 210'
        4. 괄호를 제거한 지번 표기 → '경기도 김포시 대곶면 대벽리 662-1번지'
        5(최후 대체): 읍/면/동 단위까지만 → '경기도 김포시 대곶면'

    반환: [(query, precision), ...]. precision ∈
        {'raw','normalized','road_address','parcel_address','admin_fallback'}.
    admin_fallback은 다른 후보가 모두 실패했을 때만 시도해야 한다(호출부 책임).
    """
    location = (location or '').strip()
    if not location or location == '-':
        return []
    candidates = []
    seen = set()

    def add(q, precision):
        q = re.sub(r'\s+', ' ', (q or '')).strip()
        if q and q not in seen:
            seen.add(q)
            candidates.append((q, precision))

    add(location, 'raw')

    cleaned = re.sub(r'[\r\n\t]+', ' ', location)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    add(cleaned, 'normalized')

    paren_matches = list(_PAREN_RE.finditer(cleaned))
    if paren_matches:
        prefix = cleaned[:paren_matches[0].start()].strip()
        for m in paren_matches:
            inner = m.group(1).strip()
            if inner:
                road_query = f'{prefix} {inner}'.strip() if prefix else inner
                add(road_query, 'road_address')
        parcel_text = _PAREN_RE.sub(' ', cleaned)
        parcel_text = re.sub(r'\s+', ' ', parcel_text).strip()
        add(parcel_text, 'parcel_address')

    admin_match = _EUPMYEONDONG_RE.search(cleaned)
    if admin_match:
        add(admin_match.group(1), 'admin_fallback')

    return candidates


def geocode_project_location(location, api_key=None, domain=None, session=None):
    """EIASS 사업지 주소(사업 개요의 'location' 필드)를 지오코딩한다.

    사업지 주소 자체가 정확히 지오코딩되면 그 결과를 쓰고, 모든 사업지 주소 후보가
    실패했을 때만 읍/면/동 단위 행정구역으로 최후 대체한다(fallback_used=True로 표시돼서
    "사업지 주소 기준 거리"와 "읍면동 대체 거리"를 결과에서 구분할 수 있다).

    반환: {
        'lon', 'lat', 'geocode_source', 'geocode_query_used', 'location_precision',
        'fallback_used', 'attempts': [{'precision','query','api','matched',...}, ...],
    } — location이 비어 있으면 None. 모든 후보(행정구역 대체 포함)가 실패하면 lon/lat 등이
    None인 채로 attempts만 채워서 반환한다(진단용).
    """
    candidates = _location_candidates(location)
    if not candidates:
        return None
    session = session or _session()
    api_key = api_key or get_vworld_api_key()
    domain = domain or get_vworld_domain()
    attempts = []

    def try_candidates(cands):
        for query, precision in cands:
            coord, sub_attempts = _geocode_address_attempts(
                query, api_key=api_key, domain=domain, session=session)
            for a in sub_attempts:
                attempts.append({'precision': precision, 'query': query, **a})
            if coord:
                return query, precision, coord
        return None

    primary = [c for c in candidates if c[1] != 'admin_fallback']
    hit = try_candidates(primary)
    fallback_used = False
    if not hit:
        admin = [c for c in candidates if c[1] == 'admin_fallback']
        hit = try_candidates(admin)
        fallback_used = bool(hit)

    if not hit:
        return {
            'lon': None, 'lat': None, 'geocode_source': None,
            'geocode_query_used': None, 'location_precision': None,
            'fallback_used': False, 'attempts': attempts,
        }
    query, precision, (lon, lat, source) = hit
    return {
        'lon': lon, 'lat': lat, 'geocode_source': source,
        'geocode_query_used': query, 'location_precision': precision,
        'fallback_used': fallback_used, 'attempts': attempts,
    }


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


_KDPA_VERSION_CACHE = {'version': None, 'fetched_at': 0.0}
_KDPA_VERSION_TTL_SECONDS = 300  # 사업지 여러 건을 연달아 조회할 때 매번 /getNewVer를 부르지 않도록 짧게 캐시


def _kdpa_latest_version(session):
    now = time.time()
    if _KDPA_VERSION_CACHE['version'] and (now - _KDPA_VERSION_CACHE['fetched_at']) < _KDPA_VERSION_TTL_SECONDS:
        return _KDPA_VERSION_CACHE['version']
    res = session.post(
        KDPA_BASE_URL + '/getNewVer',
        data=json.dumps({'version_nm': ''}, ensure_ascii=False).encode('utf-8'),
        headers=_kdpa_headers(json_request=True), timeout=15,
    )
    version = KDPA_DEFAULT_VERSION
    if res.status_code == 200:
        version = str(res.json().get('body') or KDPA_DEFAULT_VERSION)
    _KDPA_VERSION_CACHE['version'] = version
    _KDPA_VERSION_CACHE['fetched_at'] = now
    return version


def find_nearby_protected_areas(lon, lat, radius_m=1000, session=None, version=None, designations=None):
    """지점 기준 radius_m(미터) 이내 KDPA 보호지역을 조회한다.

    KDPA GeoServer는 WFS 1.1.0 + EPSG:4326에서 위경도 축순서(lat,lon)를 강제하므로,
    축순서가 명확한 WFS 1.0.0(lon,lat)을 사용해 DWITHIN 공간필터로 서버 측 필터링한다.

    designations: None(기본)이면 KDPA_DEFAULT_LAYER_DEFS 전체 레이어를 조회한다(기존 동작과
        동일, 하위 호환). 문자열(콤마 구분) 또는 리스트로 지정하면 KDPA_DEFAULT_LAYER_DEFS의
        'name'과 정확히 일치하는 레이어만 조회한다(예: '천연기념물'만 필요하면 다른 5개 레이어를
        조회하지 않아 왕복 요청 수와 시간이 그만큼 줄어든다).

    반환: [{'name','desig','agency','status_year','distance_m'}, ...] (거리 오름차순).
    서버측 DWITHIN 필터는 근사치라 경계 케이스에서 radius_m을 살짝 넘는 결과가 섞일 수 있어,
    폴리곤 경계까지 재계산한 distance_m 기준으로 radius_m 초과 결과는 후처리로 제외한다.
    """
    if designations:
        if isinstance(designations, str):
            designations = [d.strip() for d in designations.split(',') if d.strip()]
        known_names = {layer['name'] for layer in KDPA_DEFAULT_LAYER_DEFS}
        unknown = [d for d in designations if d not in known_names]
        if unknown:
            raise EiassError(
                f"알 수 없는 designations 값: {', '.join(unknown)}. "
                f"사용 가능: {', '.join(sorted(known_names))}"
            )
        layers = [layer for layer in KDPA_DEFAULT_LAYER_DEFS if layer['name'] in designations]
    else:
        layers = KDPA_DEFAULT_LAYER_DEFS

    s = session or _session()
    version = version or _kdpa_latest_version(s)

    results = []
    seen = set()
    for layer in layers:
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
            if distance_m is not None and distance_m > radius_m:
                continue
            results.append({
                'name': props.get('ORIG_NAME') or props.get('NAME') or '(이름 없음)',
                'desig': props.get('DESIG') or desig or '보호지역',
                'agency': props.get('MANG_AUTH') or props.get('GOV_TYPE') or 'KDPA',
                'status_year': props.get('STATUS_YR'),
                'distance_m': round(distance_m, 1) if distance_m is not None else None,
            })
    results.sort(key=lambda r: (r['distance_m'] is None, r['distance_m'] if r['distance_m'] is not None else 0))
    return results
