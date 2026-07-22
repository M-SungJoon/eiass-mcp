"""EIASS MCP 서버 (프로토타입).

`eiass_core.py`의 순수 함수를 AI(Claude 등)가 호출 가능한 MCP 도구로 노출한다.

실행: python mcp_server.py   (stdio transport)
Claude Code에 연결하려면 프로젝트 `.mcp.json`에 다음과 같이 등록한다:
    {
      "mcpServers": {
        "eiass": {
          "command": "python",
          "args": ["mcp_server.py"],
          "cwd": "D:/Personal/GoogleDrive/ClaudeCowork/eiass_new_ver2_cdx"
        }
      }
    }
"""
import threading
import time
import uuid
import multiprocessing
import os
import re
import shutil
import sys
import tempfile

from mcp.server.fastmcp import FastMCP

import eiass_core as core
import requests
from config import JOB_RESULT_PAGE_LIMIT, MAX_DOCUMENT_SCAN_BATCH_SIZE, MAX_SCAN_BATCH_SIZE
from job_store import JobStore
from scan_engine import ScanRunner, document_status, spatial_status

mcp = FastMCP('eiass')

# 작업 상태·후보·결과는 SQLite에 저장한다. 이전 프로세스가 남긴 running 작업은 ScanRunner가
# queued로 복구해 같은 후보 스냅샷의 미처리분만 다시 실행한다.
_job_store = None
_job_runner = None
_backend_lock = threading.Lock()


def _jobs_backend():
    """실제 MCP 서버 프로세스에서만 영속 작업 러너를 지연 초기화한다.

    Windows multiprocessing spawn 자식이 모듈을 다시 import할 때 작업 복구 러너가
    생기는 것을 막는다.
    """
    global _job_store, _job_runner
    with _backend_lock:
        if _job_store is None:
            _job_store = JobStore()
            _job_runner = ScanRunner(_job_store)
        return _job_store, _job_runner


# ── 실패 원인 안내 ──
# 도구마다 의존하는 외부 서비스가 다르다. 지오코딩이 실패했는데 "EIASS 사이트 장애"라고
# 알리면 오진이므로, 그 도구가 실제로 쓰는 서비스만 점검해 범인을 지목한다.
SVC_SEARCH = ('eiass_site', 'eiass_search_api')      # 사업 검색
SVC_DOCS = ('eiass_site',)                            # 상세/첨부문서 다운로드
SVC_GEO = ('vworld',)                                 # 주소 → 좌표
SVC_SPATIAL = ('vworld', 'kdpa')                      # 좌표 → 보호지역
SVC_SEARCH_DOCS = SVC_SEARCH                          # 검색 후 문서까지 여는 도구
SVC_PROJECT_SPATIAL = ('eiass_site', 'vworld', 'kdpa')  # 사업 상세 → 주소 → 보호지역
SVC_SEARCH_SPATIAL = ('eiass_site', 'eiass_search_api', 'vworld', 'kdpa')


def _fail(exc, services):
    """도구 실패를 사용자에게 돌려줄 dict로 만든다.

    외부 서비스 때문에 생긴 실패일 때만 상태를 점검해 어느 서버가 문제인지 붙인다. 입력값
    오류까지 점검하면 오타 한 번에 헬스체크가 날아간다. 점검은 설명을 붙일 뿐 재시도나 중단을
    결정하지 않으므로, 이 경로 때문에 작업이 버려지지는 않는다.
    """
    if core.is_network_error(exc):
        return core.explain_failure(exc, services)
    return {'error': str(exc)}


def _confirmation_only(result):
    """AI가 조건을 재구성하지 않고 고정 문구만 전달하도록 미리보기 응답을 최소화한다."""
    if result.get('error'):
        return result
    return {
        'confirm_required': True,
        'confirmation_required': True,
        'display_mode': 'verbatim',
        'confirmation_message': result['confirmation_message'],
    }

# ── 백그라운드 스캔 job 레지스트리 ──
# 대량 후보(수백 건)를 협의의견/본안 등에서 키워드로 훑을 때, 한 번의 MCP tool 호출 안에서
# 다 끝내려 하면 타임아웃이 난다. eiass_start_document_keyword_scan은 즉시 반환하고,
# 실제 스캔은 백그라운드 스레드가 offset을 이어가며 계속 진행한다.
_jobs_lock = threading.Lock()
_jobs = {}
_JOB_RETENTION_SECONDS = 24 * 60 * 60
_MAX_RETAINED_JOBS = 100


def _cleanup_jobs_locked(jobs):
    """완료 job 결과가 서버 수명 내내 메모리에 누적되지 않게 제한한다."""
    now = time.time()
    terminal = {'done', 'cancelled', 'error'}
    expired = [job_id for job_id, job in jobs.items()
               if job.get('status') in terminal and now - job.get('updated_at', now) > _JOB_RETENTION_SECONDS]
    for job_id in expired:
        jobs.pop(job_id, None)
    retained = [(job.get('updated_at', 0), job_id) for job_id, job in jobs.items()
                if job.get('status') in terminal]
    retained.sort()
    for _, job_id in retained[:-_MAX_RETAINED_JOBS]:
        jobs.pop(job_id, None)


def _snapshot_candidates(kwargs, session):
    return core.search_projects(
        kwargs.get('keyword', ''), type_codes=kwargs.get('type_codes'), agency_code=kwargs.get('agency_code', ''),
        max_pages=kwargs.get('max_pages', 0), session=session,
        consult_date_from=kwargs.get('consult_date_from'), consult_date_to=kwargs.get('consult_date_to'),
        progress_status=kwargs.get('progress_status', ''), climate_filter=kwargs.get('climate_filter', ''),
        biz_gubun=kwargs.get('biz_gubun', ''),
        progress_stage_keys=kwargs.get('progress_stage_keys'),
    )


def _run_scan_job(job_id, kwargs):
    job = _jobs[job_id]
    session = core._session()
    offset = 0
    try:
        with _jobs_lock:
            job['status'] = 'discovering'
            job['current_phase'] = 'candidate_snapshot'
            job['updated_at'] = time.time()
        candidates = _snapshot_candidates(kwargs, session)
        with _jobs_lock:
            job['candidates_total'] = len(candidates)
            job['status'] = 'running'
            job['current_phase'] = 'document_scan'
            job['updated_at'] = time.time()
        while True:
            with _jobs_lock:
                if job['cancel']:
                    job['status'] = 'cancelled'
                    job['updated_at'] = time.time()
                    return
            result = core.search_projects_by_document_keyword(
                session=session, offset=offset, candidates=candidates,
                should_cancel=lambda: job['cancel'], **kwargs)
            with _jobs_lock:
                job['checked'] += result['checked']
                job['candidates_total'] = result['candidates_total']
                job['matches'].extend(result['matches'])
                job['skipped'].extend(result['skipped'])
                job['checked_no_match'].extend(result['checked_no_match'])
                job['audit_samples'].append(result['audit_sample']) if result['audit_sample'] else None
                if result['needs_refinement']:
                    job['needs_refinement'] = True
                    job['refinement_hints'].append(result['refinement_hint'])
                for stage, stats in result['stage_stats'].items():
                    acc = job['stage_stats'].setdefault(stage, {'checked': 0, 'matched': 0})
                    acc['checked'] += stats['checked']
                    acc['matched'] += stats['matched']
                job['updated_at'] = time.time()
            if not result['has_more']:
                break
            offset = result['next_offset']
        with _jobs_lock:
            job['status'] = 'done'
            job['current_phase'] = 'completed'
            job['updated_at'] = time.time()
    except core.ScanCancelled:
        with _jobs_lock:
            job['status'] = 'cancelled'
            job['updated_at'] = time.time()
    except Exception as exc:
        with _jobs_lock:
            job['status'] = 'error'
            job['error'] = str(exc)
            job['updated_at'] = time.time()
    finally:
        session.close()


# ── 백그라운드 공간조회(검색+보호구역 인접) job 레지스트리 ──
# 문서 키워드 스캔과 동일한 이유(후보가 많으면 한 번의 MCP 호출로 다 끝내면 타임아웃)로,
# eiass_start_spatial_scan도 즉시 job_id를 반환하고 백그라운드 스레드가 이어서 진행한다.
_spatial_jobs_lock = threading.Lock()
_spatial_jobs = {}


def _run_spatial_scan_job(job_id, kwargs):
    job = _spatial_jobs[job_id]
    session = core._session()
    offset = 0
    try:
        with _spatial_jobs_lock:
            job['status'] = 'discovering'
            job['current_phase'] = 'candidate_snapshot'
            job['updated_at'] = time.time()
        candidates = _snapshot_candidates(kwargs, session)
        with _spatial_jobs_lock:
            job['candidates_total'] = len(candidates)
            job['status'] = 'running'
            job['current_phase'] = 'spatial_scan'
            job['updated_at'] = time.time()
        while True:
            with _spatial_jobs_lock:
                if job['cancel']:
                    job['status'] = 'cancelled'
                    job['updated_at'] = time.time()
                    return
            result = core.scan_projects_protected_area_adjacency(
                session=session, offset=offset, candidates=candidates,
                should_cancel=lambda: job['cancel'], **kwargs)
            with _spatial_jobs_lock:
                job['checked'] += result['checked']
                job['candidates_total'] = result['candidates_total']
                job['scanned'].extend(result['scanned'])
                job['matches'].extend(result['matches'])
                job['geocode_failures'].extend(result['geocode_failures'])
                job['spatial_failures'].extend(result['spatial_failures'])
                job['updated_at'] = time.time()
            if not result['has_more']:
                break
            offset = result['next_offset']
        with _spatial_jobs_lock:
            job['status'] = 'done'
            job['current_phase'] = 'completed'
            job['updated_at'] = time.time()
    except core.ScanCancelled:
        with _spatial_jobs_lock:
            job['status'] = 'cancelled'
            job['updated_at'] = time.time()
    except Exception as exc:
        with _spatial_jobs_lock:
            job['status'] = 'error'
            job['error'] = str(exc)
            job['updated_at'] = time.time()
    finally:
        session.close()


@mcp.tool()
def eiass_search_projects(keyword: str = '', types: str = '', agency_code: str = '', max_pages: int = 0,
                           consult_date_from: str = '', consult_date_to: str = '',
                           progress_status: str = '', climate_filter: str = '', biz_gubun: str = '',
                           progress_stage: str = '') -> dict:
    """EIASS(환경영향평가정보지원시스템)에서 원본 앱과 동일한 필터로 사업을 검색한다.

    유사 사업을 찾을 때는 이 도구로 후보 목록을 뽑은 뒤, 각 후보에 대해
    eiass_get_project_documents로 '초안/본안/보완/협의의견 등' 첨부문서를 확인하고
    eiass_read_document로 원문을 읽어 내용을 비교하라(협의의견만 우선 확인하면 놓치는
    경우가 있으니 여러 단계를 함께 확인하는 편이 안전하다). 협의완료일 범위 +
    문서 원문 키워드를 한 번에 조회하려면 eiass_find_projects_by_document_keyword를 써라.

    Args:
        keyword: 사업명 등 검색 키워드. 비워도 다른 필터만으로 검색 가능(전부 비우면 에러).
        types: 평가종류 코드 콤마 구분 (S=전략환경영향평가, M=소규모환경영향평가,
               E=환경영향평가, A=사후환경영향조사, P=사전환경성검토). 비우면 전체.
        agency_code: 협의기관 코드 (선택, 예: 'HG'=한강유역환경청).
        max_pages: 평가종류별 최대 조회 페이지 수(1페이지=100건). 0(기본값)이면 무제한 —
            검색조건으로 이미 필터링됐다고 보고 결과가 끝날 때까지(다음 페이지가 없을 때까지)
            전부 조회한다. 후보가 아주 많으면 시간이 오래 걸릴 수 있다.
        consult_date_from: 'YYYY-MM-DD'. 협의완료일(사후조사는 조사년도) 하한.
        consult_date_to: 'YYYY-MM-DD'. 협의완료일(사후조사는 조사년도) 상한.
            예: "최근 1년" → consult_date_from=오늘로부터 1년 전, consult_date_to=오늘.
        progress_status: '완료' | '진행' | ''. 진행현황 필터.
        climate_filter: 'Y' | 'N' | ''. 기후변화영향평가 대상 여부(사후조사 제외).
        biz_gubun: 사업유형(사업구분) 필터. 다음 라벨 중 정확히 일치해야 한다(사후환경영향조사는 미지원):
            도시의 개발, 산업입지 및 산업단지의 조성, 에너지 개발, 항만 건설, 도로의 건설,
            수자원의 개발, 철도(도시철도 포함)의 건설, 공항 또는 비행장의 건설, 하천의 이용 및 개발,
            개간 및 공유수면의 매립, 관광단지의 개발, 지역개발/특정지역의 개발, 체육시설의 설치,
            폐기물처리시설 및 분뇨처리시설의 설치, 국방군사시설의 설치, 토석·모래·자갈·광물 등의 채취,
            산지의 개발, 기타
        progress_stage: 진행구분 다중선택, 콤마 구분. 사용 가능 라벨: 초안, 평가서, 재협의,
            약식평가, 변경협의. 비우면 5개 전부(=전체, 필터 없음)로 취급한다.
    """
    type_codes = [c.strip().upper() for c in types.split(',') if c.strip()] or None
    stage_labels = [s.strip() for s in progress_stage.split(',') if s.strip()]
    date_filter_exclusions = []
    try:
        stage_keys = core.progress_stage_keys_from_labels(stage_labels)
        results = core.search_projects(
            keyword, type_codes=type_codes, agency_code=agency_code,
            max_pages=max_pages,
            consult_date_from=consult_date_from or None, consult_date_to=consult_date_to or None,
            progress_status=progress_status, climate_filter=climate_filter, biz_gubun=biz_gubun,
            progress_stage_keys=stage_keys, date_filter_exclusions=date_filter_exclusions,
        )
    except (core.EiassError, requests.exceptions.RequestException) as exc:
        return _fail(exc, SVC_SEARCH)
    return {'count': len(results), 'projects': results, 'date_filter_exclusions': date_filter_exclusions}


@mcp.tool()
def eiass_preview_search(
    text_queries: str, match_mode: str = 'any', keyword: str = '', types: str = '', agency_code: str = '',
    consult_date_from: str = '', consult_date_to: str = '', progress_status: str = '완료',
    climate_filter: str = '', biz_gubun: str = '', progress_stage: str = '',
    stages: str = '초안,본안,보완,협의의견', doc_title_contains: str = '',
    max_pages: int = 0, inference_notes: str = '', audit_sample_size: int = 0,
) -> dict:
    """실제로 문서를 다운로드하지 않고, 이 조건으로 검색하면 무엇을 하게 될지 미리 보여준다.
    eiass_find_projects_by_document_keyword / eiass_start_document_keyword_scan을
    confirmed=True 없이 호출하면 내부적으로 이 함수와 같은 내용을 반환하므로, 보통은 이
    도구를 따로 부르지 않고 그 두 도구를 confirmed=False(기본값)로 먼저 불러도 된다 —
    이 도구는 "확인 문구만 다시 보고 싶을 때"나 조건을 조정해보며 비교할 때 쓴다.

    반환된 confirmation_message만 사용자에게 **한 글자도 바꾸지 말고 그대로** 보여라.
    요약·생략·순서 변경·설명 추가를 금지한다. 승인을 받은 뒤에만
    같은 조건 + confirmed=true로 실제 실행 도구를 호출하라. 사용자가 명시적으로 말하지
    않은 필터는 여기 그대로 두면 자동으로 '전체'로 취급된다 — types/biz_gubun 등을
    AI가 임의로 좁혔다면 반드시 inference_notes에 그 사실과 이유를 적어서 사용자가
    구분할 수 있게 하라.

    Args:
        text_queries: 문서 원문에서 찾을 문자열, 콤마 구분(예: 'CALPUFF,CMAQ').
        match_mode: 'any' | 'all'.
        keyword/types/agency_code/consult_date_from/consult_date_to/progress_status/biz_gubun/stages/max_pages:
            eiass_find_projects_by_document_keyword와 동일. 사용자가 말하지 않은 조건은 비워두면
            '전체'로 표시된다(임의로 좁혀서 넘기지 말 것).
        doc_title_contains: stages 범위 안에서 파일명에 포함되어야 할 문자열, 콤마 구분(예:
            '대기질,기상'). 초안~보완 등 여러 단계를 다 열어보지 않고, 그 안에서도 제목에
            해당 단어가 들어간 문서(챕터별 PDF 파일명에 항목명이 그대로 들어있음, 예:
            '0922 대기질(...).pdf')만 확인 대상으로 좁힐 때 쓴다. 파일명 문자열 매칭이라
            실제 표기와 다른 용어를 쓰면 놓칠 수 있다 — 예상 문서 수가 이상하면 용어를 바꿔보라.
        progress_stage: 진행구분 다중선택, 콤마 구분. 사용 가능 라벨: 초안, 평가서, 재협의,
            약식평가, 변경협의. 비우면 5개 전부(=전체, 필터 없음)로 취급한다.
        inference_notes: 사용자가 직접 말하지 않았는데 AI가 추론/제안해서 좁힌 조건이 있다면
            그 내용과 이유를 여기 적는다(예: "평가종류를 환경영향평가로 좁혔습니다 — 사용자는
            '산업단지 사례'라고만 했음"). 없으면 빈 문자열로 둔다.
    """
    type_codes = [c.strip().upper() for c in types.split(',') if c.strip()] or None
    stage_list = tuple(s.strip() for s in stages.split(',') if s.strip()) or ('초안', '본안', '보완', '협의의견')
    query_list = [q.strip() for q in text_queries.split(',') if q.strip()]
    title_terms = [t.strip() for t in doc_title_contains.split(',') if t.strip()] or None
    progress_stage_labels = [s.strip() for s in progress_stage.split(',') if s.strip()]
    try:
        stage_keys = core.progress_stage_keys_from_labels(progress_stage_labels)
        return _confirmation_only(core.preview_document_keyword_search(
            query_list, match_mode=match_mode, keyword=keyword, type_codes=type_codes, agency_code=agency_code,
            consult_date_from=consult_date_from or None, consult_date_to=consult_date_to or None,
            progress_status=progress_status, climate_filter=climate_filter,
            biz_gubun=biz_gubun, progress_stage_keys=stage_keys,
            stages=stage_list, doc_title_contains=title_terms,
            max_pages=max_pages, inference_notes=inference_notes,
            audit_sample_size=audit_sample_size,
        ))
    except (core.EiassError, requests.exceptions.RequestException) as exc:
        return _fail(exc, SVC_SEARCH)


@mcp.tool()
def eiass_find_projects_by_document_keyword(
    text_queries: str, match_mode: str = 'any', keyword: str = '', types: str = '', agency_code: str = '',
    consult_date_from: str = '', consult_date_to: str = '', progress_status: str = '완료',
    climate_filter: str = '', biz_gubun: str = '', progress_stage: str = '',
    stages: str = '초안,본안,보완,협의의견', doc_title_contains: str = '',
    max_pages: int = 0, offset: int = 0, max_candidates: int = 30,
    inference_notes: str = '', confirmed: bool = False, audit_sample_size: int = 0,
) -> dict:
    """필터(협의완료일 범위/진행상태 등)로 사업을 좁힌 뒤, 지정한 단계(기본 초안/본안/보완/
    협의의견 — 협의의견만 우선 확인하면 놓치는 경우가 있어 여러 단계를 기본으로 함께 확인)의
    첨부 PDF 원문에서 키워드가 있는 사업만 골라 리스트로 반환한다. 후보가 적을 때(대략
    50건 이하) 한 번에 끝낼 수 있는 소규모 조회용이다 — 그보다 큰 후보군을 타임아웃 없이
    끝까지 훑으려면 eiass_start_document_keyword_scan(백그라운드 job)을 써라.

    **실행 전 확인 필수**: confirmed=False(기본값)로 호출하면 실제 조회는 하지 않고,
    적용될 검색조건/문서범위/예상 후보·문서 수/과거 패턴 힌트가 담긴 확인 문구
    (confirmation_message)만 반환한다. confirmation_message만 사용자에게 한 글자도 바꾸지
    말고 그대로 보여라. 요약·생략·순서 변경·설명 추가를 금지한다. 승인을 받은 뒤,
    **같은 조건 그대로에 confirmed=true만 추가**해서 다시 호출해야 실제로 실행된다.
    사용자가 이미 조건을 구체적으로 다 말해서 확인이 필요 없다고 판단되더라도, 이 게이트를
    건너뛰지 마라 — 특히 stages를 '본안'/'초안'처럼 넓히거나 types/biz_gubun을 AI가
    추론해서 좁힌 경우는 반드시 확인받아야 한다.

    "최근 1년 내 협의완료된 사업 중 협의의견에 원형보전지 관련 내용이 있는 사업을 찾아줘"
    같은 요청은 이 도구로 처리한다(먼저 confirmed=False로 확인 문구를 받고, 승인 후
    confirmed=true로 재호출):
      eiass_find_projects_by_document_keyword(text_queries="원형보전지",
          consult_date_from="2025-07-09", consult_date_to="2026-07-09", progress_status="완료")

    여러 키워드를 한 번에 확인하려면 콤마로 구분한다(문서를 키워드 수만큼 반복 다운로드하지
    않고 한 번만 열어서 전부 확인한다): text_queries="CALPUFF,CMAQ", match_mode="any".
    같은 PDF(file_seq)는 로컬에 캐시되므로, 같은 조건을 offset을 바꿔가며 다시 호출하거나
    다른 키워드로 재조회해도 이미 받은 문서는 재다운로드하지 않는다.

    응답의 next_offset을 다음 호출의 offset으로 넘기면 이어서 검색한다(has_more=false가
    될 때까지 반복 — 매 이어하기 호출도 confirmed=true를 유지해야 한다). 후보군이 커서
    여러 번 나눠 호출해야 한다면 처음부터 eiass_start_document_keyword_scan을 쓰는 편이 낫다.

    응답의 needs_refinement가 true면 매칭이 과도하거나 참고문헌/부록 문맥으로 보이는
    비율이 높다는 뜻이다 — refinement_hint를 참고해 바로 최종 답을 내지 말고, 사용자에게
    문맥 조건을 추가할지 물어봐라.

    **최종 보고 형식(필수)**: 조사가 끝나면 (1) `사업명 | eia_cd | 원문 파일명 |
    유사내용 페이지번호 | 변경 내용 요약` 컬럼의 마크다운 표로 결과를 보여주고,
    (2) 같은 행 데이터를 eiass_export_matches_csv로 CSV 파일로도 만들어 경로를 안내하라.
    '변경 내용 요약'은 matched_snippets 원문 발췌를 근거로 AI가 직접 작성한다.
    표/CSV는 매칭된 사업만이 아니라 이번에 "스캔한 전체"(matches + checked_no_match +
    skipped)를 대상으로 한다 — 매칭 없는 사업도 행에 포함하고 원문 파일명/유사내용
    페이지번호/변경 내용 요약은 `매칭 없음`으로 채운다.

    주의: 부분문자열 매칭이다(예: '원형보전지'가 원문에 그대로 등장해야 함). 동의어나
    문맥상 유사 표현까지 잡으려면, 이 도구로 1차 후보를 좁힌 뒤 필요시 eiass_read_document로
    각 사업의 원문 전체를 받아 직접 의미 단위로 재검토하라. stages를 "초안,본안"처럼 넓히면
    검토의견/본문에서도 찾을 수 있지만 다운로드량이 늘어 느려진다.

    Args:
        text_queries: 문서 원문에서 찾을 문자열, 콤마 구분(예: '원형보전지' 또는 'CALPUFF,CMAQ').
        match_mode: 'any'(하나라도 포함되면 매칭) | 'all'(전부 포함돼야 매칭).
        keyword: 사업명 검색 키워드(선택). 비워도 날짜/상태 필터만으로 후보를 좁힐 수 있다.
        types: 평가종류 코드 콤마 구분. 비우면 전체 — 사용자가 특정 종류를 말하지 않았다면
            비워두고, AI 판단으로 좁힐 경우 반드시 inference_notes에 남겨라.
        agency_code: 협의기관 코드(선택).
        consult_date_from/consult_date_to: 'YYYY-MM-DD'. 협의완료일 범위.
        progress_status: '완료' | '진행' | ''. 기본 '완료'(협의의견은 완료 건에만 존재).
        climate_filter: 'Y' | 'N' | ''. 기후변화영향평가 대상 여부. 비우면 전체.
        biz_gubun: 사업유형(사업구분) 필터 라벨(예: '산업입지 및 산업단지의 조성'). 정확한 목록은
            eiass_search_projects 설명 참고. 사후환경영향조사는 미지원.
        progress_stage: 진행구분 다중선택, 콤마 구분. 사용 가능 라벨: 초안, 평가서, 재협의,
            약식평가, 변경협의. 비우면 5개 전부(=전체, 필터 없음)로 취급한다.
        stages: 검색 대상 단계, 콤마 구분(기본 '초안,본안,보완,협의의견'). 특정 단계만 보려면
            좁혀서 넘긴다(예: '협의의견'만).
        doc_title_contains: stages 범위 안에서 파일명에 포함되어야 할 문자열, 콤마 구분(예:
            '대기질,기상'). 챕터별로 쪼개진 초안/본안/보완 등에서 제목에 특정 단어가 들어간
            문서만 확인하고 싶을 때 쓴다(예: '0922 대기질(...).pdf'). 비우면 단계 안의 모든
            PDF를 확인한다.
        max_pages: 평가종류별 최대 검색 페이지 수(1페이지=100건). 0(기본값)이면 무제한(끝까지 조회).
        offset: 후보 목록에서 시작할 위치(이어서 조회할 때 이전 응답의 next_offset을 넘긴다).
        max_candidates: 이번 호출에서 원문까지 내려받아 확인할 최대 후보 수(기본 30).
        inference_notes: AI가 추론/제안해서 좁힌 조건이 있으면 그 내용과 이유(없으면 빈 문자열).
        confirmed: 사용자 승인을 받았으면 true. false(기본값)면 미리보기만 반환하고 실행하지 않는다.
        audit_sample_size: stages 밖의 다른 단계도 이번 배치 중 이 수만큼 표본 검증한다(기본 0=끔).
    """
    type_codes = [c.strip().upper() for c in types.split(',') if c.strip()] or None
    stage_list = tuple(s.strip() for s in stages.split(',') if s.strip()) or ('초안', '본안', '보완', '협의의견')
    query_list = [q.strip() for q in text_queries.split(',') if q.strip()]
    title_terms = [t.strip() for t in doc_title_contains.split(',') if t.strip()] or None
    progress_stage_labels = [s.strip() for s in progress_stage.split(',') if s.strip()]
    try:
        stage_keys = core.progress_stage_keys_from_labels(progress_stage_labels)
        common_kwargs = dict(
            match_mode=match_mode, keyword=keyword, type_codes=type_codes, agency_code=agency_code,
            consult_date_from=consult_date_from or None, consult_date_to=consult_date_to or None,
            progress_status=progress_status, climate_filter=climate_filter,
            biz_gubun=biz_gubun, progress_stage_keys=stage_keys,
            stages=stage_list, doc_title_contains=title_terms,
            max_pages=max_pages,
        )
        if not confirmed:
            return _confirmation_only(core.preview_document_keyword_search(
                query_list, inference_notes=inference_notes,
                audit_sample_size=audit_sample_size, **common_kwargs))
        return core.search_projects_by_document_keyword(
            query_list, offset=offset, max_candidates=max_candidates,
            audit_sample_size=audit_sample_size, **common_kwargs,
        )
    except (core.EiassError, requests.exceptions.RequestException) as exc:
        return _fail(exc, SVC_SEARCH_DOCS)


@mcp.tool()
def eiass_start_document_keyword_scan(
    text_queries: str, match_mode: str = 'any', keyword: str = '', types: str = '', agency_code: str = '',
    consult_date_from: str = '', consult_date_to: str = '', progress_status: str = '완료',
    climate_filter: str = '', biz_gubun: str = '', progress_stage: str = '',
    stages: str = '초안,본안,보완,협의의견', doc_title_contains: str = '',
    max_pages: int = 0, batch_size: int = 10, inference_notes: str = '', confirmed: bool = False,
    audit_sample_size: int = 0,
) -> dict:
    """대량 후보(수십~수백 건)를 타임아웃 걱정 없이 끝까지 훑는 백그라운드 스캔을 시작한다.
    즉시 job_id를 반환하고, 실제 조회(사업 상세조회 + PDF 다운로드/추출 + 키워드 매칭)는
    서버 안에서 백그라운드로 계속 진행된다. eiass_get_scan_status(job_id)로 주기적으로
    진행 상황과 지금까지의 매칭 결과를 확인하라 — done이 될 때까지 몇 번이고 다시 불러도 된다.
    상태조회/취소는 스캔이 실행 중이어도 즉시 응답한다(백그라운드 스레드와 별도로 처리됨).

    **실행 전 확인 필수**: confirmed=False(기본값)면 스캔을 시작하지 않고 eiass_preview_search와
    동일한 확인 문구만 반환한다. confirmation_message만 한 글자도 바꾸지 말고 그대로 보여라.
    요약·생략·순서 변경·설명 추가를 금지한다. 사용자 승인 후 같은 조건 + confirmed=true로 다시 호출해야
    실제 백그라운드 스캔이 시작된다.

    파라미터는 eiass_find_projects_by_document_keyword와 동일하되 offset/max_candidates
    대신 batch_size(체크포인트 묶음 크기, 기본·최대 10)를 쓴다. 실제 동시 처리량은 이 값과
    분리되어 PDF 다운로드 3건, 텍스트 추출 2건으로 제한되며 후보 하나가 끝날 때마다 결과를
    저장한다. 따라서 느린 PDF 한 건이 같은 묶음의 완료 결과 저장이나 재시작 복구를 막지 않는다.

    **최종 보고 형식(필수)**: 스캔이 done이 되면 (1) `사업명 | eia_cd | 원문 파일명 |
    유사내용 페이지번호 | 변경 내용 요약` 컬럼의 마크다운 표로 결과를 보여주고,
    (2) 같은 행 데이터를 eiass_export_matches_csv로 CSV 파일로도 만들어 경로를 안내하라.
    '변경 내용 요약'은 matches의 matched_snippets 원문 발췌를 근거로 AI가 직접 작성한다.
    표/CSV는 매칭된 사업만이 아니라 이번에 "스캔한 전체"(matches + checked_no_match +
    skipped)를 대상으로 한다 — eiass_get_scan_status의 checked_no_match도 포함해서
    매칭 없는 사업은 원문 파일명/유사내용 페이지번호/변경 내용 요약을 `매칭 없음`으로 채운다.
    """
    type_codes = [c.strip().upper() for c in types.split(',') if c.strip()] or None
    stage_list = tuple(s.strip() for s in stages.split(',') if s.strip()) or ('초안', '본안', '보완', '협의의견')
    query_list = [q.strip() for q in text_queries.split(',') if q.strip()]
    title_terms = [t.strip() for t in doc_title_contains.split(',') if t.strip()] or None
    progress_stage_labels = [s.strip() for s in progress_stage.split(',') if s.strip()]
    try:
        stage_keys = core.progress_stage_keys_from_labels(progress_stage_labels)
    except (core.EiassError, requests.exceptions.RequestException) as exc:
        return _fail(exc, SVC_SEARCH)
    common_kwargs = dict(
        match_mode=match_mode, keyword=keyword, type_codes=type_codes, agency_code=agency_code,
        consult_date_from=consult_date_from or None, consult_date_to=consult_date_to or None,
        progress_status=progress_status, climate_filter=climate_filter,
        biz_gubun=biz_gubun, progress_stage_keys=stage_keys,
        stages=stage_list, doc_title_contains=title_terms,
        max_pages=max_pages,
    )
    if not confirmed:
        try:
            return _confirmation_only(core.preview_document_keyword_search(
                query_list, inference_notes=inference_notes,
                audit_sample_size=audit_sample_size, **common_kwargs))
        except (core.EiassError, requests.exceptions.RequestException) as exc:
            return _fail(exc, SVC_SEARCH)

    if not isinstance(batch_size, int) or not 1 <= batch_size <= MAX_DOCUMENT_SCAN_BATCH_SIZE:
        return {'error': f'batch_size는 1~{MAX_DOCUMENT_SCAN_BATCH_SIZE} 범위의 정수여야 합니다.'}
    job_id = uuid.uuid4().hex[:12]
    kwargs = dict(text_queries=query_list, batch_size=batch_size,
                  audit_sample_size=audit_sample_size, **common_kwargs)
    job_store, job_runner = _jobs_backend()
    job_store.cleanup()
    job_store.create(job_id, 'document', kwargs)
    if not job_runner.submit(job_id):
        job_store.update(job_id, status='error', phase='queue_full',
                         error='스캔 대기열이 가득 찼습니다. 잠시 후 다시 시도하세요.')
        return {'error': '스캔 대기열이 가득 찼습니다. 잠시 후 다시 시도하세요.'}
    return {'job_id': job_id, 'status': 'queued'}


@mcp.tool()
def eiass_get_scan_status(job_id: str, include_matches: bool = False,
                          result_offset: int = 0, result_limit: int = 100) -> dict:
    """eiass_start_document_keyword_scan이 반환한 job_id로 진행 상황과 지금까지의 매칭
    결과를 조회한다(스캔이 running이어도 즉시 응답한다). status는
    'queued' | 'discovering' | 'running' | 'done' | 'cancelled' | 'error'. activity_state는
    running | active_slow | server_waiting | server_slow | local_resource_pressure | timed_out |
    stalled 등을 구분한다. work_progress는 현재 단계·사업·파일, 완료 문서/후보 수,
    현재/누적 수신 바이트, 전송률, 마지막 활동 시각을 보여준다. heartbeat_diagnostics와
    seconds_since_heartbeat/activity로 DB 기록 장애와 실제 작업 정지를 구분할 수 있다.

    needs_refinement가 true면 매칭이 과도하거나 참고문헌/부록 문맥으로 보이는 매칭이 많다는
    뜻이다 — refinement_hints를 참고해 스캔 결과를 그대로 최종 답으로 쓰지 말고, 사용자에게
    문맥 조건 추가 여부를 먼저 확인하라. audit_samples는 stages 밖 표본 검증 결과(요청 시)다.

    **최종 보고 형식(필수)**: 스캔이 done이 되면 보고/CSV는 매칭된 사업만이 아니라 이번에
    "스캔한 전체"(matches + checked_no_match + skipped)를 대상으로 한다. 매칭 없는 사업은
    `원문 파일명`/`유사내용 페이지번호`/`변경 내용 요약`을 `매칭 없음`으로 채운다.
    """
    job_store, _ = _jobs_backend()
    return document_status(job_store, job_id, include_matches, result_offset,
                           min(result_limit, JOB_RESULT_PAGE_LIMIT))


@mcp.tool()
def eiass_cancel_scan(job_id: str) -> dict:
    """진행 중인 백그라운드 스캔(eiass_start_document_keyword_scan)을 취소한다."""
    job_store, _ = _jobs_backend()
    job = job_store.get(job_id)
    if not job or job['kind'] != 'document':
        return {'error': f'알 수 없는 job_id: {job_id}'}
    job_store.request_cancel(job_id)
    return {'job_id': job_id, 'status': 'cancelling'}


@mcp.tool()
def eiass_get_project_documents(view_type: str, eia_cd: str, revirpt_seq: str) -> dict:
    """사업 상세 개요와 단계별 첨부문서 목록(초안/본안/협의의견 등)을 조회한다.

    view_type/eia_cd/revirpt_seq는 eiass_search_projects 결과의 동일 필드를 그대로 넘긴다.
    '협의의견' 단계에 있는 파일의 seq를 eiass_read_document에 넘기면 원문 텍스트를 읽을 수 있다.
    """
    try:
        detail = core.get_project_detail(view_type, eia_cd, revirpt_seq)
    except Exception as exc:
        return {'error': str(exc)}
    stage_docs = {
        stage: {cat: files for cat, files in categories.items()}
        for stage, categories in detail['stage_docs'].items()
    }
    return {'fields': detail['fields'], 'stage_docs': stage_docs}


@mcp.tool()
def eiass_read_document(file_seq: str, max_chars: int = 20000) -> dict:
    """첨부 PDF(FILE_SEQ)를 다운로드해 텍스트를 추출한다. 협의의견 원문 등을 읽을 때 사용.
    같은 file_seq를 이전에 조회했다면 로컬 캐시에서 즉시 반환한다(응답의 from_cache 참고).

    캐시가 없으면 PDF를 임시 파일로 스트리밍하며 연결 8초, 첫 데이터 20초, 무응답 30초,
    다운로드 240초, 지속 저속(30초 유예 뒤 60초간 128KiB/s 미만), 추출 시작 15초,
    추출 90초, 문서 전체 360초 제한을 적용한다. 실패한 한 문서는 다른 후보 스캔을 막지 않는다.

    file_seq는 eiass_get_project_documents가 반환한 stage_docs 안의 'seq' 값이다.
    """
    try:
        return core.download_document_text(file_seq, max_chars=max_chars)
    except (core.EiassError, requests.exceptions.RequestException) as exc:
        return _fail(exc, SVC_DOCS)


@mcp.tool()
def eiass_export_matches_csv(rows_json: str, filename: str = '') -> dict:
    """조사 결과(eiass_find_projects_by_document_keyword / eiass_start_document_keyword_scan)를
    CSV 파일로 저장한다.

    문서 키워드 조사 결과를 사용자에게 보고할 때는 다음 두 가지를 항상 함께 한다:
    1) 아래와 같은 컬럼으로 마크다운 표를 작성해 채팅에 보여준다:
       `사업명 | eia_cd | 원문 파일명 | 유사내용 페이지번호 | 변경 내용 요약`
    2) 조사한 사업의 전체 리스트를 이 도구로 CSV 파일로 만들어 제시한다(생성된 경로를
       사용자에게 알려준다).

    **표/CSV 모두 매칭된 사업만이 아니라 이번에 "스캔한 전체"를 대상으로 한다** — 조회
    결과의 matches뿐 아니라 checked_no_match(문서를 열어봤지만 키워드가 없었던 사업)와
    skipped(첨부문서가 없거나 조회에 실패한 사업)도 빠짐없이 행에 포함해야 한다. 매칭이
    없는 사업은 원문 파일명/유사내용 페이지번호/변경 내용 요약을 `매칭 없음`으로 채운다.

    Args:
        rows_json: JSON 배열 문자열. 각 원소는 아래 5개 키를 전부 포함한 객체여야 한다
            (하나도 빠짐없이, 이 키 이름 그대로):
            "사업명", "eia_cd", "원문 파일명", "유사내용 페이지번호", "변경 내용 요약".
            매칭된 사업은 "변경 내용 요약"을 matched_snippets의 원문 발췌를 근거로 AI가
            직접 작성해야 한다(빈 문자열 금지) — 같은 사업/파일이라도 매칭된 위치(페이지)가
            다르면 행을 나눠서 각각 요약한다. 매칭 없는 사업(checked_no_match)/미확인·제외된
            사업(skipped)은 나머지 3개 컬럼을 `매칭 없음`으로 채워 행을 만든다.
            예: '[{"사업명":"OO사업","eia_cd":"E12345","원문 파일명":"협의의견.pdf",
                "유사내용 페이지번호":"12","변경 내용 요약":"CALPUFF 모델 적용 관련 조건 추가"},
                {"사업명":"XX사업","eia_cd":"E67890","원문 파일명":"매칭 없음",
                "유사내용 페이지번호":"매칭 없음","변경 내용 요약":"매칭 없음"}]'
        filename: 저장할 파일명(확장자 생략 가능). 비우면 타임스탬프로 자동 생성.
    """
    try:
        rows = core.json.loads(rows_json)
    except Exception as exc:
        return {'error': f'rows_json 파싱 실패: {exc}'}
    if not isinstance(rows, list):
        return {'error': 'rows_json은 객체(dict)의 JSON 배열이어야 합니다.'}
    try:
        path = core.export_matches_csv(rows, filename=filename or None)
    except core.EiassError as exc:
        return {'error': str(exc)}
    return {'path': path, 'row_count': len(rows), 'columns': core.CSV_REPORT_COLUMNS}


@mcp.tool()
def eiass_check_protected_area_adjacency(address: str, radius_m: int = 1000, designations: str = '') -> dict:
    """주소와 인근 KDPA 보호지역(국립공원/천연기념물/습지보호지역/야생생물보호구역/OECM)의
    인접 여부와 거리를 확인한다. 내부적으로 VWorld로 지오코딩 후 KDPA WFS를 반경 검색한다.

    사업 개요의 사업지 주소(예: '경기도 김포시 대곶면 (천호로 210) 대벽리 662-1번지'처럼
    도로명/지번이 괄호로 섞인 복합 표기)를 넘길 때는 이 도구 대신
    eiass_check_project_protected_area_adjacency를 써라 — 이 도구는 단순 주소 문자열
    하나만 그대로 지오코딩하므로 복합 표기에서 지오코딩이 실패하기 쉽다.

    Args:
        address: 지번 또는 도로명 주소.
        radius_m: 검색 반경(미터). 기본 1000m.
        designations: 확인할 보호구역 종류, 콤마 구분(예: '천연기념물' 또는 '천연기념물,국립공원').
            비우면(기본값) 국립공원/천연기념물/습지보호지역/야생생물보호구역/OECM/최신 보호지역
            전체를 모두 조회한다 — 필요한 종류만 알면 지정해서 조회 시간을 줄여라.
    """
    try:
        coord = core.geocode_address(address)
    except (core.EiassError, requests.exceptions.RequestException) as exc:
        return _fail(exc, SVC_SPATIAL)
    if not coord:
        return {'error': f'주소를 좌표로 변환하지 못했습니다: {address}'}
    lon, lat, source = coord
    try:
        spatial = core.find_nearby_protected_areas(
            lon, lat, radius_m=radius_m, designations=designations or None, return_diagnostics=True)
    except (core.EiassError, requests.exceptions.RequestException) as exc:
        return _fail(exc, SVC_SPATIAL)
    return {
        'address': address,
        'lon': lon, 'lat': lat, 'geocode_source': source,
        'radius_m': radius_m,
        'nearby_count': len(spatial['areas']),
        'nearby_protected_areas': spatial['areas'],
        'spatial_complete': spatial['complete'],
        'spatial_errors': spatial['errors'],
    }


@mcp.tool()
def eiass_check_project_protected_area_adjacency(
    project_location: str, radius_m: int = 1000, designations: str = '',
    allow_admin_fallback: bool = True, confirmed: bool = False,
) -> dict:
    """EIASS 사업 개요의 사업지 주소(eiass_get_project_documents 결과 fields['location'])를
    지오코딩해 인근 KDPA 보호지역과의 거리를 확인한다. 검색 필터(협의완료일 범위/사업유형
    등)로 후보 자체를 새로 뽑아야 하는 요청("최근 6개월 도로사업 중 국립공원 5km 이내" 같은)
    은 이 도구 대신 eiass_find_projects_protected_area_adjacency를 써라 — 그쪽은 검색 필터
    확인 문구까지 포함해서 하나로 처리한다. 이 도구는 사업(들)을 이미 알고 있을 때 쓴다.

    사업지 주소는 '경기도 김포시 대곶면 (천호로 210) 대벽리 662-1번지'처럼 도로명과 지번이
    괄호로 섞인 복합 표기인 경우가 많다 — 이 도구는 원문을 그대로/괄호만 정리/괄호 안
    도로명(+앞쪽 행정구역)/괄호 밖 지번, 이 순서로 여러 후보를 만들어 순서대로 지오코딩을
    시도한다. 사업지 주소 후보가 하나라도 성공하면 그 결과를 쓰고, 전부 실패했을 때만(그리고
    allow_admin_fallback=True일 때만) 읍/면/동 단위 행정구역으로 최후 대체한다. 응답의
    `location_precision`/`fallback_used`로 사업지 주소 기준인지 읍면동 대체인지 구분된다.

    **실행 전 확인 필수**: 원문 키워드 검색 등으로 찾은 여러 후보 사업을 이어서 공간조회할
    때는, 이 도구를 사업별로 반복 호출하기 전에 검토 대상 사업 목록 전체 + radius_m +
    designations를 사용자에게 한 번에 보여주고 승인을 받아라. confirmed=False(기본값)면
    실제 지오코딩·공간조회를 하지 않고 위 조건을 모두 담은 고정 확인 문구를 반환한다.
    confirmation_message만 한 글자도 바꾸지 말고 그대로 보여라. 요약·생략·순서 변경·설명
    추가를 금지한다. 사용자 승인
    후 같은 조건 + confirmed=true로 사업마다 호출한다.

    **최종 보고 형식(필수)**: 여러 사업을 조회했다면 (1) `사업명 | eia_cd | 대상 보호구역 |
    거리` 컬럼의 마크다운 표로 결과를 보여주고, (2) 같은 행 데이터를
    eiass_export_spatial_matches_csv로 CSV 파일로도 만들어 경로를 안내하라(문서 키워드
    조사 결과용 eiass_export_matches_csv와는 컬럼이 다르니 혼동하지 말 것). 표/CSV는 반경
    이내인 사업만이 아니라 이번에 조회한 사업 전체를 포함해야 한다 — 반경 밖이거나
    지오코딩에 실패한 사업도 대상 보호구역/거리를 `해당 없음`으로 채워 행에 넣는다.

    Args:
        project_location: 사업지 주소(사업 개요의 location 필드 원문 그대로 넘기면 된다).
        radius_m: 검색 반경(미터). 기본 1000m.
        designations: 확인할 보호구역 종류, 콤마 구분(예: '천연기념물'). 비우면(기본값) 전체
            레이어를 조회한다 — 필요한 종류만 알면 지정해서 조회 시간을 줄여라(예: 이번 사례처럼
            천연기념물만 확인하면 되면 다른 5개 레이어를 건너뛴다).
        allow_admin_fallback: True(기본값)면 사업지 주소 후보가 모두 실패했을 때 읍/면/동
            단위로 대체 지오코딩한다. False면 대체하지 않고 실패로 반환한다.
        confirmed: 사용자 승인을 받았으면 true. false(기본값)면 미리보기만 반환.
    """
    if not confirmed:
        designation_label = designations or '전체(국립공원/천연기념물/습지보호지역/야생생물보호구역/OECM/최신 보호지역 전체)'
        conditions = [
            ('사업지 주소', project_location),
            ('대상 보호구역', designation_label),
            ('반경', f'{radius_m}m'),
            ('기준 위치', 'EIASS 사업개요의 사업지 주소'),
            ('행정구역 대체 지오코딩', '허용' if allow_admin_fallback else '허용 안 함'),
        ]
        return _confirmation_only({
            'confirmation_message': core.render_scan_confirmation(conditions),
        })

    try:
        geocoded = core.geocode_project_location(project_location)
    except (core.EiassError, requests.exceptions.RequestException) as exc:
        return _fail(exc, SVC_PROJECT_SPATIAL)
    if not geocoded:
        return {'error': f'사업지 주소가 비어 있습니다: {project_location!r}'}
    if geocoded['lon'] is None:
        return {
            'project_location': project_location,
            'error': '사업지 주소를 좌표로 변환하지 못했습니다(읍/면/동 대체 포함 모든 후보 실패).',
            'attempts': geocoded['attempts'],
        }
    if geocoded['fallback_used'] and not allow_admin_fallback:
        return {
            'project_location': project_location,
            'error': '사업지 주소 지오코딩에 실패했고 allow_admin_fallback=False라 읍/면/동 대체를 하지 않았습니다.',
            'attempts': geocoded['attempts'],
        }

    try:
        spatial = core.find_nearby_protected_areas(
            geocoded['lon'], geocoded['lat'], radius_m=radius_m,
            designations=designations or None, return_diagnostics=True)
    except (core.EiassError, requests.exceptions.RequestException) as exc:
        return _fail(exc, SVC_PROJECT_SPATIAL)

    return {
        'project_location': project_location,
        'lon': geocoded['lon'], 'lat': geocoded['lat'],
        'geocode_source': geocoded['geocode_source'],
        'geocode_query_used': geocoded['geocode_query_used'],
        'location_precision': geocoded['location_precision'],
        'fallback_used': geocoded['fallback_used'],
        'radius_m': radius_m,
        'nearby_count': len(spatial['areas']),
        'nearby_protected_areas': spatial['areas'],
        'spatial_complete': spatial['complete'],
        'spatial_errors': spatial['errors'],
    }


@mcp.tool()
def eiass_find_projects_protected_area_adjacency(
    keyword: str = '', types: str = '', agency_code: str = '', consult_date_from: str = '',
    consult_date_to: str = '', progress_status: str = '완료', climate_filter: str = '',
    biz_gubun: str = '', progress_stage: str = '',
    max_pages: int = 0, radius_m: int = 1000, designations: str = '', allow_admin_fallback: bool = True,
    offset: int = 0, max_candidates: int = 15, inference_notes: str = '', confirmed: bool = False,
) -> dict:
    """검색 필터(협의완료일 범위/진행상태/사업유형 등)로 사업 후보를 뽑은 뒤, 각 후보의
    사업지 주소를 지오코딩해 인근 KDPA 보호지역과의 거리를 확인한다. "최근 6개월 내
    협의완료된 도로사업 중 국립공원 5km 이내에 사업지가 위치한 사례를 찾아줘" 같은,
    원문 검색과 마찬가지로 검색 필터 + 공간조회를 함께 쓰는 요청은 이 도구로 처리한다
    (eiass_check_project_protected_area_adjacency는 사업 하나를 이미 알고 있을 때 쓰는
    도구다 — 검색 필터로 후보 자체를 뽑아야 하면 이 도구를 써라).

    **실행 전 확인 필수**: confirmed=False(기본값)로 호출하면 실제 지오코딩/공간조회는 하지
    않고, 모든 검색·공간 조건을 고정 순서로 하나도 빠짐없이 담은 확인 문구
    (confirmation_message)만 반환한다. confirmation_message만 한 글자도 바꾸지 말고 그대로
    보여라. 요약·생략·순서 변경·설명 추가를 금지한다. 승인을 받은 뒤에만, **같은 조건
    그대로에 confirmed=true만 추가**해서 다시 호출해야 실제로 실행된다. AI가 임의로 좁힌
    필터가 있다면 inference_notes에 남겨라.

    후보가 많으면 offset/max_candidates(기본 15건씩)로 나눠서 이어 호출한다(응답의
    next_offset을 다음 호출의 offset으로 넘긴다 — 이어하기 호출도 confirmed=true 유지).
    대량 후보를 타임아웃 없이 끝까지 훑으려면 eiass_start_spatial_scan(백그라운드 job)을 써라.

    **최종 보고 형식(필수)**: 결과 보고는 스캔한 사업 "전체"(scanned)를 기준으로 한다 —
    조건(반경 이내)에 해당하는 사업(matches)만 추리지 말 것. `사업명 | eia_cd | 대상
    보호구역 | 거리` 컬럼의 마크다운 표에서, 반경 밖이거나 지오코딩에 실패한 사업
    (geocode_failures)도 빠짐없이 포함하고 대상 보호구역/거리는 `해당 없음`으로 채운다.
    같은 행 데이터를 eiass_export_spatial_matches_csv로 CSV 파일로도 만들어 경로를 안내하라.

    Args:
        keyword/types/agency_code/consult_date_from/consult_date_to/progress_status/biz_gubun/
        progress_stage/max_pages: eiass_search_projects와 동일한 검색 필터.
        climate_filter: 'Y' | 'N' | ''. 기후변화영향평가 대상 여부. 비우면 전체.
        radius_m: 검색 반경(미터). 기본 1000m.
        designations: 확인할 보호구역 종류, 콤마 구분(예: '국립공원'). 비우면(기본값) 전체
            레이어를 조회한다.
        allow_admin_fallback: True(기본값)면 사업지 주소 지오코딩 실패 시 읍/면/동 단위로
            대체 지오코딩한다.
        offset/max_candidates: 이어서 조회할 때 사용(기본 15건씩 처리).
        inference_notes: AI가 추론/제안해서 좁힌 조건이 있으면 그 내용과 이유(없으면 빈 문자열).
        confirmed: 사용자 승인을 받았으면 true. false(기본값)면 미리보기만 반환.
    """
    type_codes = [c.strip().upper() for c in types.split(',') if c.strip()] or None
    stage_labels = [s.strip() for s in progress_stage.split(',') if s.strip()]
    designation_list = [d.strip() for d in designations.split(',') if d.strip()] or None
    try:
        stage_keys = core.progress_stage_keys_from_labels(stage_labels)
        common_kwargs = dict(
            keyword=keyword, type_codes=type_codes, agency_code=agency_code,
            consult_date_from=consult_date_from or None, consult_date_to=consult_date_to or None,
            progress_status=progress_status, climate_filter=climate_filter,
            biz_gubun=biz_gubun, progress_stage_keys=stage_keys,
            max_pages=max_pages,
        )
        if not confirmed:
            return _confirmation_only(core.preview_spatial_scan(
                radius_m=radius_m, designations=designation_list,
                allow_admin_fallback=allow_admin_fallback, inference_notes=inference_notes,
                **common_kwargs))
        return core.scan_projects_protected_area_adjacency(
            radius_m=radius_m, designations=designation_list, allow_admin_fallback=allow_admin_fallback,
            offset=offset, max_candidates=max_candidates, **common_kwargs)
    except (core.EiassError, requests.exceptions.RequestException) as exc:
        return _fail(exc, SVC_SEARCH_SPATIAL)


@mcp.tool()
def eiass_start_spatial_scan(
    keyword: str = '', types: str = '', agency_code: str = '', consult_date_from: str = '',
    consult_date_to: str = '', progress_status: str = '완료', climate_filter: str = '',
    biz_gubun: str = '', progress_stage: str = '',
    max_pages: int = 0, radius_m: int = 1000, designations: str = '', allow_admin_fallback: bool = True,
    batch_size: int = 10, inference_notes: str = '', confirmed: bool = False,
) -> dict:
    """대량 후보(수십~수백 건)를 타임아웃 걱정 없이 끝까지 훑는 백그라운드 공간조회
    (검색 필터 + 보호구역 인접 확인)를 시작한다. 즉시 job_id를 반환하고, 실제 조회(사업
    상세조회 + 지오코딩 + KDPA 공간조회)는 서버 안에서 백그라운드로 계속 진행된다.
    eiass_get_spatial_scan_status(job_id)로 주기적으로 진행 상황을 확인하라.

    **실행 전 확인 필수**: confirmed=False(기본값)면 스캔을 시작하지 않고
    eiass_find_projects_protected_area_adjacency와 동일한 전체 조건 확인 문구만 반환한다.
    confirmation_message만 한 글자도 바꾸지 말고 그대로 보여라. 요약·생략·순서 변경·설명
    추가를 금지한다.
    사용자 승인 후 같은 조건 + confirmed=true로 다시 호출해야 실제 백그라운드 스캔이 시작된다.

    파라미터는 eiass_find_projects_protected_area_adjacency와 동일하되 offset/max_candidates
    대신 batch_size(백그라운드 루프 1회당 처리 건수, 기본 10)를 쓴다.

    **최종 보고 형식(필수)**: 스캔이 done이 되면 스캔한 사업 "전체"(scanned)를 기준으로
    `사업명 | eia_cd | 대상 보호구역 | 거리` 컬럼의 마크다운 표를 만들고(반경 밖/지오코딩
    실패 사업도 `해당 없음`으로 포함), 같은 행 데이터를 eiass_export_spatial_matches_csv로
    CSV 파일로도 만들어 경로를 안내하라.
    """
    type_codes = [c.strip().upper() for c in types.split(',') if c.strip()] or None
    stage_labels = [s.strip() for s in progress_stage.split(',') if s.strip()]
    designation_list = [d.strip() for d in designations.split(',') if d.strip()] or None
    try:
        stage_keys = core.progress_stage_keys_from_labels(stage_labels)
    except (core.EiassError, requests.exceptions.RequestException) as exc:
        return _fail(exc, SVC_SEARCH_SPATIAL)
    common_kwargs = dict(
        keyword=keyword, type_codes=type_codes, agency_code=agency_code,
        consult_date_from=consult_date_from or None, consult_date_to=consult_date_to or None,
        progress_status=progress_status, climate_filter=climate_filter,
        biz_gubun=biz_gubun, progress_stage_keys=stage_keys,
        max_pages=max_pages,
    )
    if not confirmed:
        try:
            return _confirmation_only(core.preview_spatial_scan(
                radius_m=radius_m, designations=designation_list,
                allow_admin_fallback=allow_admin_fallback, inference_notes=inference_notes,
                **common_kwargs))
        except (core.EiassError, requests.exceptions.RequestException) as exc:
            return _fail(exc, SVC_SEARCH_SPATIAL)

    if not isinstance(batch_size, int) or not 1 <= batch_size <= MAX_SCAN_BATCH_SIZE:
        return {'error': f'batch_size는 1~{MAX_SCAN_BATCH_SIZE} 범위의 정수여야 합니다.'}
    job_id = uuid.uuid4().hex[:12]
    kwargs = dict(radius_m=radius_m, designations=designation_list, allow_admin_fallback=allow_admin_fallback,
                  batch_size=batch_size, **common_kwargs)
    job_store, job_runner = _jobs_backend()
    job_store.cleanup()
    job_store.create(job_id, 'spatial', kwargs)
    if not job_runner.submit(job_id):
        job_store.update(job_id, status='error', phase='queue_full',
                         error='스캔 대기열이 가득 찼습니다. 잠시 후 다시 시도하세요.')
        return {'error': '스캔 대기열이 가득 찼습니다. 잠시 후 다시 시도하세요.'}
    return {'job_id': job_id, 'status': 'queued'}


@mcp.tool()
def eiass_get_spatial_scan_status(job_id: str, include_results: bool = False,
                                  result_offset: int = 0, result_limit: int = 100) -> dict:
    """eiass_start_spatial_scan이 반환한 job_id로 진행 상황과 지금까지의 결과를 조회한다
    (스캔이 running이어도 즉시 응답한다). status는 'running' | 'done' | 'cancelled' | 'error'.

    **최종 보고 형식(필수)**: done이 되면 scanned(스캔한 전체) 기준으로 보고하라 — matches만
    추리지 말 것. 자세한 지침은 eiass_start_spatial_scan 참고.
    """
    job_store, _ = _jobs_backend()
    return spatial_status(job_store, job_id, include_results, result_offset,
                          min(result_limit, JOB_RESULT_PAGE_LIMIT))


@mcp.tool()
def eiass_cancel_spatial_scan(job_id: str) -> dict:
    """진행 중인 백그라운드 공간조회(eiass_start_spatial_scan)를 취소한다."""
    job_store, _ = _jobs_backend()
    job = job_store.get(job_id)
    if not job or job['kind'] != 'spatial':
        return {'error': f'알 수 없는 job_id: {job_id}'}
    job_store.request_cancel(job_id)
    return {'job_id': job_id, 'status': 'cancelling'}


@mcp.tool()
def eiass_export_spatial_matches_csv(rows_json: str, filename: str = '') -> dict:
    """공간조회(보호구역 인접) 결과를 CSV 파일로 저장한다.

    eiass_find_projects_protected_area_adjacency / eiass_start_spatial_scan /
    eiass_check_project_protected_area_adjacency로 여러 사업을 조회한 뒤 보고할 때는
    다음 두 가지를 항상 함께 한다:
    1) `사업명 | eia_cd | 대상 보호구역 | 거리` 컬럼의 마크다운 표로 결과를 채팅에 보여준다.
    2) 같은 행 데이터를 이 도구로 CSV 파일로 만들어 경로를 안내한다.

    **표/CSV 모두 반경 이내 매칭(matches)만이 아니라 이번에 "스캔한 전체"(scanned)를
    대상으로 한다** — 반경 밖이거나(대상 보호구역 없음) 지오코딩에 실패한 사업
    (geocode_failures)도 빠짐없이 행에 포함해야 한다. 그런 사업은 대상 보호구역/거리를
    `해당 없음`으로 채운다.

    Args:
        rows_json: JSON 배열 문자열. 각 원소는 "사업명","eia_cd","대상 보호구역","거리" 4개
            키를 전부 포함해야 한다(하나도 빠짐없이, 이 키 이름 그대로). 한 사업에 인접
            보호구역이 여러 개면 행을 나눠서 각각 적는다. 반경 밖/지오코딩 실패 사업은
            대상 보호구역/거리를 `해당 없음`으로 채운 행으로 넣는다.
            예: '[{"사업명":"김포 항공산업단지 조성사업","eia_cd":"E12345",
                "대상 보호구역":"천연기념물 OO","거리":"320.5m"},
                {"사업명":"XX 도로건설사업","eia_cd":"E67890",
                "대상 보호구역":"해당 없음","거리":"해당 없음"}]'
        filename: 저장할 파일명(확장자 생략 가능). 비우면 타임스탬프로 자동 생성.
    """
    try:
        rows = core.json.loads(rows_json)
    except Exception as exc:
        return {'error': f'rows_json 파싱 실패: {exc}'}
    if not isinstance(rows, list):
        return {'error': 'rows_json은 객체(dict)의 JSON 배열이어야 합니다.'}
    try:
        path = core.export_spatial_matches_csv(rows, filename=filename or None)
    except core.EiassError as exc:
        return {'error': str(exc)}
    return {'path': path, 'row_count': len(rows), 'columns': core.CSV_SPATIAL_REPORT_COLUMNS}


@mcp.tool()
def eiass_check_server_status() -> dict:
    """MCP가 사용하는 모든 EIASS HTTP 경로와 VWorld/KDPA 외부 서비스의 현재 상태를 점검한다.

    EIASS 본사이트, 통합 검색 API, 환경영향평가 상세조회, 전략·소규모·사전환경성 상세조회,
    사후환경영향조사 상세조회, PDF 다운로드 API를 각각 확인한다. PDF 다운로드는 최근 정상
    문서(없는 경우 공개 검색에서 동적으로 찾은 문서)의 첫 4 KiB만 받아 실제 PDF 응답 여부와
    첫 바이트 응답시간을 확인한 뒤 즉시 연결을 닫는다. PDF 전체 다운로드나 텍스트 추출은 하지 않는다.

    각 서비스별로 ok/kind/status_code/latency_ms/error를 반환한다. PDF 항목에는 추가로
    bytes_checked/sample_source가 포함된다. eiass_all_ok는 EIASS 경로만의 종합 상태이고,
    all_ok는 VWorld/KDPA까지 포함한다. 일부 항목만 실패해도 정상 항목과 함께 모두 보여줘라.
    """
    return core.check_server_status()


@mcp.tool()
def eiass_version() -> dict:
    """이 EIASS MCP 서버(exe)의 버전을 반환한다. 설치된 버전과 최신 버전을 비교하고
    싶을 때 사용자에게 알려줄 용도로 쓴다(최신 버전 확인은 install.ps1/install.bat이 담당)."""
    return {'version': core.__version__, **core.runtime_build_info()}


@mcp.tool()
def eiass_geocode(address: str) -> dict:
    """주소를 VWorld API로 경위도 좌표로 변환한다."""
    try:
        coord = core.geocode_address(address)
    except (core.EiassError, requests.exceptions.RequestException) as exc:
        return _fail(exc, SVC_GEO)
    if not coord:
        return {'error': f'주소를 좌표로 변환하지 못했습니다: {address}'}
    lon, lat, source = coord
    return {'address': address, 'lon': lon, 'lat': lat, 'source': source}


# ── 고아 _MEI 임시 폴더 회수 ──
# onefile 부트로더는 실행할 때마다 exe 전체(약 74MB)를 %TEMP%/_MEIxxxxxx에 풀고 "정상
# 종료할 때만" 지운다. MCP 서버는 클라이언트가 끝낼 때 강제 종료되는 일이 잦아서 이 폴더가
# 계속 남았다(실측: 개발 PC에 280개 20.7GB 잔류). v1.10.0에서 빌드를 onedir로 바꿔 애초에
# 추출을 없앴지만, 이전 onefile 버전이 이미 남긴 잔재는 사용자 PC에 그대로 있으므로 기동할
# 때마다 청소한다.

_MEIPASS_DIR_RE = re.compile(r'^_MEI[A-Za-z0-9]{4,}$')


def _sweep_orphan_meipass_dirs():
    """%TEMP%에 남은 고아 _MEI 폴더를 지운다. 실패는 전부 무시한다(부가 기능).

    사용 중인 폴더를 지우지 않으려고 삭제 전에 폴더 이름부터 바꾼다 — Windows는 안에 열린
    파일이 하나라도 있으면 폴더 rename을 거부하므로, rename 성공 자체가 "이 폴더를 쓰는
    프로세스가 없다"는 증거다. 실행 중인 서버의 폴더는 rename이 실패해 건너뛴다. mtime으로
    판정하면 며칠씩 떠 있는 서버의 폴더를 지워버리므로 나이는 기준으로 쓰지 않는다.
    """
    try:
        tmp_root = tempfile.gettempdir()
        own = getattr(sys, '_MEIPASS', None)
        own_key = os.path.normcase(os.path.abspath(own)) if own else None
        for name in os.listdir(tmp_root):
            path = os.path.join(tmp_root, name)
            if not os.path.isdir(path):
                continue
            # 이전 회수 시도가 중간에 끊겨 남은 잔해는 이미 rename 검사를 통과한 것이라 바로 지운다.
            if name.startswith('_MEI') and name.endswith('.stale'):
                shutil.rmtree(path, ignore_errors=True)
                continue
            if not _MEIPASS_DIR_RE.match(name):
                continue
            if own_key and os.path.normcase(os.path.abspath(path)) == own_key:
                continue
            staging = path + '.stale'
            try:
                os.rename(path, staging)
            except OSError:
                continue  # 사용 중이거나 권한 없음 — 다음 기동 때 다시 시도한다
            shutil.rmtree(staging, ignore_errors=True)
    except Exception:
        pass


def run_stdio():
    """MCP 클라이언트가 stdin을 정상적으로 닫을 때 종료 traceback을 남기지 않는다."""
    # 수백 개가 쌓여 있으면 삭제에 수 분이 걸릴 수 있어 MCP 핸드셰이크를 막지 않도록
    # 데몬 스레드로 돌린다. 중간에 서버가 죽어도 다음 기동 때 이어서 지운다.
    threading.Thread(target=_sweep_orphan_meipass_dirs, name='meipass-sweeper', daemon=True).start()
    _jobs_backend()
    try:
        mcp.run(transport='stdio')
    except (BrokenPipeError, EOFError):
        return
    except ValueError as exc:
        if 'closed file' in str(exc).lower() or 'i/o operation on closed' in str(exc).lower():
            return
        raise
    else:
        # PyInstaller 콘솔 부트로더가 종료 시 닫힌 TextIOWrapper를 flush하는 환경이 있어,
        # 정상적인 stdio 종료 후에만 표준 스트림을 devnull로 바꿔 shutdown traceback을 막는다.
        if getattr(sys, 'frozen', False):
            sys.stdout = open(os.devnull, 'w', encoding='utf-8')
            sys.stderr = open(os.devnull, 'w', encoding='utf-8')


if __name__ == '__main__':
    multiprocessing.freeze_support()
    run_stdio()
