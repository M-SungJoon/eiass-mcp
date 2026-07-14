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
import uuid

from mcp.server.fastmcp import FastMCP

import eiass_core as core

mcp = FastMCP('eiass')

# ── 백그라운드 스캔 job 레지스트리 ──
# 대량 후보(수백 건)를 협의의견/본안 등에서 키워드로 훑을 때, 한 번의 MCP tool 호출 안에서
# 다 끝내려 하면 타임아웃이 난다. eiass_start_document_keyword_scan은 즉시 반환하고,
# 실제 스캔은 백그라운드 스레드가 offset을 이어가며 계속 진행한다.
_jobs_lock = threading.Lock()
_jobs = {}


def _run_scan_job(job_id, kwargs):
    job = _jobs[job_id]
    session = core._session()
    offset = 0
    try:
        while True:
            with _jobs_lock:
                if job['cancel']:
                    job['status'] = 'cancelled'
                    return
            result = core.search_projects_by_document_keyword(session=session, offset=offset, **kwargs)
            with _jobs_lock:
                job['checked'] += result['checked']
                job['candidates_total'] = result['candidates_total']
                job['matches'].extend(result['matches'])
                job['skipped'].extend(result['skipped'])
                job['audit_samples'].append(result['audit_sample']) if result['audit_sample'] else None
                if result['needs_refinement']:
                    job['needs_refinement'] = True
                    job['refinement_hints'].append(result['refinement_hint'])
                for stage, stats in result['stage_stats'].items():
                    acc = job['stage_stats'].setdefault(stage, {'checked': 0, 'matched': 0})
                    acc['checked'] += stats['checked']
                    acc['matched'] += stats['matched']
            if not result['has_more']:
                break
            offset = result['next_offset']
        with _jobs_lock:
            job['status'] = 'done'
    except Exception as exc:
        with _jobs_lock:
            job['status'] = 'error'
            job['error'] = str(exc)


@mcp.tool()
def eiass_search_projects(keyword: str = '', types: str = '', agency_code: str = '', max_pages: int = 0,
                           consult_date_from: str = '', consult_date_to: str = '',
                           progress_status: str = '', climate_filter: str = '', biz_gubun: str = '',
                           progress_stage: str = '') -> dict:
    """EIASS(환경영향평가정보지원시스템)에서 원본 앱과 동일한 필터로 사업을 검색한다.

    유사 사업을 찾을 때는 이 도구로 후보 목록을 뽑은 뒤, 각 후보에 대해
    eiass_get_project_documents로 '협의의견' 첨부문서를 확인하고
    eiass_read_document로 원문을 읽어 내용을 비교하라. 협의완료일 범위 +
    협의의견 원문 키워드를 한 번에 조회하려면 eiass_find_projects_by_document_keyword를 써라.

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
    try:
        stage_keys = core.progress_stage_keys_from_labels(stage_labels)
        results = core.search_projects(
            keyword, type_codes=type_codes, agency_code=agency_code,
            max_pages=max(0, max_pages),
            consult_date_from=consult_date_from or None, consult_date_to=consult_date_to or None,
            progress_status=progress_status, climate_filter=climate_filter, biz_gubun=biz_gubun,
            progress_stage_keys=stage_keys,
        )
    except core.EiassError as exc:
        return {'error': str(exc)}
    return {'count': len(results), 'projects': results}


@mcp.tool()
def eiass_preview_search(
    text_queries: str, match_mode: str = 'any', keyword: str = '', types: str = '', agency_code: str = '',
    consult_date_from: str = '', consult_date_to: str = '', progress_status: str = '완료',
    biz_gubun: str = '', progress_stage: str = '', stages: str = '협의의견', doc_title_contains: str = '',
    max_pages: int = 0, inference_notes: str = '',
) -> dict:
    """실제로 문서를 다운로드하지 않고, 이 조건으로 검색하면 무엇을 하게 될지 미리 보여준다.
    eiass_find_projects_by_document_keyword / eiass_start_document_keyword_scan을
    confirmed=True 없이 호출하면 내부적으로 이 함수와 같은 내용을 반환하므로, 보통은 이
    도구를 따로 부르지 않고 그 두 도구를 confirmed=False(기본값)로 먼저 불러도 된다 —
    이 도구는 "확인 문구만 다시 보고 싶을 때"나 조건을 조정해보며 비교할 때 쓴다.

    반환된 confirmation_message를 사용자에게 그대로 보여주고 승인을 받은 뒤에만
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
    stage_list = tuple(s.strip() for s in stages.split(',') if s.strip()) or ('협의의견',)
    query_list = [q.strip() for q in text_queries.split(',') if q.strip()]
    title_terms = [t.strip() for t in doc_title_contains.split(',') if t.strip()] or None
    progress_stage_labels = [s.strip() for s in progress_stage.split(',') if s.strip()]
    try:
        stage_keys = core.progress_stage_keys_from_labels(progress_stage_labels)
        return core.preview_document_keyword_search(
            query_list, match_mode=match_mode, keyword=keyword, type_codes=type_codes, agency_code=agency_code,
            consult_date_from=consult_date_from or None, consult_date_to=consult_date_to or None,
            progress_status=progress_status, biz_gubun=biz_gubun, progress_stage_keys=stage_keys,
            stages=stage_list, doc_title_contains=title_terms,
            max_pages=max(0, max_pages), inference_notes=inference_notes,
        )
    except core.EiassError as exc:
        return {'error': str(exc)}


@mcp.tool()
def eiass_find_projects_by_document_keyword(
    text_queries: str, match_mode: str = 'any', keyword: str = '', types: str = '', agency_code: str = '',
    consult_date_from: str = '', consult_date_to: str = '', progress_status: str = '완료',
    biz_gubun: str = '', progress_stage: str = '', stages: str = '협의의견', doc_title_contains: str = '',
    max_pages: int = 0, offset: int = 0, max_candidates: int = 30,
    inference_notes: str = '', confirmed: bool = False, audit_sample_size: int = 0,
) -> dict:
    """필터(협의완료일 범위/진행상태 등)로 사업을 좁힌 뒤, 지정한 단계(기본 협의의견)의
    첨부 PDF 원문에서 키워드가 있는 사업만 골라 리스트로 반환한다. 후보가 적을 때(대략
    50건 이하) 한 번에 끝낼 수 있는 소규모 조회용이다 — 그보다 큰 후보군을 타임아웃 없이
    끝까지 훑으려면 eiass_start_document_keyword_scan(백그라운드 job)을 써라.

    **실행 전 확인 필수**: confirmed=False(기본값)로 호출하면 실제 조회는 하지 않고,
    적용될 검색조건/문서범위/예상 후보·문서 수/과거 패턴 힌트가 담긴 확인 문구
    (confirmation_message)만 반환한다. 이걸 사용자에게 보여주고 승인을 받은 뒤,
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
        biz_gubun: 사업유형(사업구분) 필터 라벨(예: '산업입지 및 산업단지의 조성'). 정확한 목록은
            eiass_search_projects 설명 참고. 사후환경영향조사는 미지원.
        progress_stage: 진행구분 다중선택, 콤마 구분. 사용 가능 라벨: 초안, 평가서, 재협의,
            약식평가, 변경협의. 비우면 5개 전부(=전체, 필터 없음)로 취급한다.
        stages: 검색 대상 단계, 콤마 구분(기본 '협의의견'). 예: '협의의견,초안'.
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
    stage_list = tuple(s.strip() for s in stages.split(',') if s.strip()) or ('협의의견',)
    query_list = [q.strip() for q in text_queries.split(',') if q.strip()]
    title_terms = [t.strip() for t in doc_title_contains.split(',') if t.strip()] or None
    progress_stage_labels = [s.strip() for s in progress_stage.split(',') if s.strip()]
    try:
        stage_keys = core.progress_stage_keys_from_labels(progress_stage_labels)
        common_kwargs = dict(
            match_mode=match_mode, keyword=keyword, type_codes=type_codes, agency_code=agency_code,
            consult_date_from=consult_date_from or None, consult_date_to=consult_date_to or None,
            progress_status=progress_status, biz_gubun=biz_gubun, progress_stage_keys=stage_keys,
            stages=stage_list, doc_title_contains=title_terms,
            max_pages=max(0, max_pages),
        )
        if not confirmed:
            return core.preview_document_keyword_search(
                query_list, inference_notes=inference_notes, **common_kwargs)
        return core.search_projects_by_document_keyword(
            query_list, offset=offset, max_candidates=max_candidates,
            audit_sample_size=audit_sample_size, **common_kwargs,
        )
    except core.EiassError as exc:
        return {'error': str(exc)}


@mcp.tool()
def eiass_start_document_keyword_scan(
    text_queries: str, match_mode: str = 'any', keyword: str = '', types: str = '', agency_code: str = '',
    consult_date_from: str = '', consult_date_to: str = '', progress_status: str = '완료',
    biz_gubun: str = '', progress_stage: str = '', stages: str = '협의의견', doc_title_contains: str = '',
    max_pages: int = 0, batch_size: int = 10, inference_notes: str = '', confirmed: bool = False,
    audit_sample_size: int = 0,
) -> dict:
    """대량 후보(수십~수백 건)를 타임아웃 걱정 없이 끝까지 훑는 백그라운드 스캔을 시작한다.
    즉시 job_id를 반환하고, 실제 조회(사업 상세조회 + PDF 다운로드/추출 + 키워드 매칭)는
    서버 안에서 백그라운드로 계속 진행된다. eiass_get_scan_status(job_id)로 주기적으로
    진행 상황과 지금까지의 매칭 결과를 확인하라 — done이 될 때까지 몇 번이고 다시 불러도 된다.
    상태조회/취소는 스캔이 실행 중이어도 즉시 응답한다(백그라운드 스레드와 별도로 처리됨).

    **실행 전 확인 필수**: confirmed=False(기본값)면 스캔을 시작하지 않고 eiass_preview_search와
    동일한 확인 문구만 반환한다. 사용자 승인 후 같은 조건 + confirmed=true로 다시 호출해야
    실제 백그라운드 스캔이 시작된다.

    파라미터는 eiass_find_projects_by_document_keyword와 동일하되 offset/max_candidates
    대신 batch_size(백그라운드 루프 1회당 처리 건수, 기본 10)를 쓴다. needs_refinement 등
    오탐 신호는 eiass_get_scan_status 결과의 각 배치 처리 시 누적되지 않으므로, 스캔이
    끝난 뒤 eiass_find_projects_by_document_keyword로 소규모 재확인하거나 매칭 스니펫의
    reference_like 필드를 직접 검토하라.

    **최종 보고 형식(필수)**: 스캔이 done이 되면 (1) `사업명 | eia_cd | 원문 파일명 |
    유사내용 페이지번호 | 변경 내용 요약` 컬럼의 마크다운 표로 결과를 보여주고,
    (2) 같은 행 데이터를 eiass_export_matches_csv로 CSV 파일로도 만들어 경로를 안내하라.
    '변경 내용 요약'은 matches의 matched_snippets 원문 발췌를 근거로 AI가 직접 작성한다.
    """
    type_codes = [c.strip().upper() for c in types.split(',') if c.strip()] or None
    stage_list = tuple(s.strip() for s in stages.split(',') if s.strip()) or ('협의의견',)
    query_list = [q.strip() for q in text_queries.split(',') if q.strip()]
    title_terms = [t.strip() for t in doc_title_contains.split(',') if t.strip()] or None
    progress_stage_labels = [s.strip() for s in progress_stage.split(',') if s.strip()]
    try:
        stage_keys = core.progress_stage_keys_from_labels(progress_stage_labels)
    except core.EiassError as exc:
        return {'error': str(exc)}
    common_kwargs = dict(
        match_mode=match_mode, keyword=keyword, type_codes=type_codes, agency_code=agency_code,
        consult_date_from=consult_date_from or None, consult_date_to=consult_date_to or None,
        progress_status=progress_status, biz_gubun=biz_gubun, progress_stage_keys=stage_keys,
        stages=stage_list, doc_title_contains=title_terms,
        max_pages=max(0, max_pages),
    )
    if not confirmed:
        try:
            return core.preview_document_keyword_search(query_list, inference_notes=inference_notes, **common_kwargs)
        except core.EiassError as exc:
            return {'error': str(exc)}

    job_id = uuid.uuid4().hex[:12]
    job = {
        'status': 'running', 'checked': 0, 'candidates_total': None,
        'matches': [], 'skipped': [], 'error': None, 'cancel': False,
        'stage_stats': {}, 'needs_refinement': False, 'refinement_hints': [], 'audit_samples': [],
    }
    with _jobs_lock:
        _jobs[job_id] = job

    kwargs = dict(text_queries=query_list, max_candidates=max(1, batch_size),
                   audit_sample_size=audit_sample_size, **common_kwargs)
    threading.Thread(target=_run_scan_job, args=(job_id, kwargs), daemon=True).start()
    return {'job_id': job_id, 'status': 'running'}


@mcp.tool()
def eiass_get_scan_status(job_id: str, include_matches: bool = True) -> dict:
    """eiass_start_document_keyword_scan이 반환한 job_id로 진행 상황과 지금까지의 매칭
    결과를 조회한다(스캔이 running이어도 즉시 응답한다). status는
    'running' | 'done' | 'cancelled' | 'error'.

    needs_refinement가 true면 매칭이 과도하거나 참고문헌/부록 문맥으로 보이는 매칭이 많다는
    뜻이다 — refinement_hints를 참고해 스캔 결과를 그대로 최종 답으로 쓰지 말고, 사용자에게
    문맥 조건 추가 여부를 먼저 확인하라. audit_samples는 stages 밖 표본 검증 결과(요청 시)다.
    """
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return {'error': f'알 수 없는 job_id: {job_id}'}
        result = {
            'status': job['status'],
            'checked': job['checked'],
            'candidates_total': job['candidates_total'],
            'match_count': len(job['matches']),
            'skipped_count': len(job['skipped']),
            'stage_stats': dict(job['stage_stats']),
            'needs_refinement': job['needs_refinement'],
            'refinement_hints': list(dict.fromkeys(job['refinement_hints'])),
            'audit_samples': list(job['audit_samples']),
            'error': job['error'],
        }
        if include_matches:
            result['matches'] = list(job['matches'])
            result['skipped'] = list(job['skipped'])
    return result


@mcp.tool()
def eiass_cancel_scan(job_id: str) -> dict:
    """진행 중인 백그라운드 스캔(eiass_start_document_keyword_scan)을 취소한다."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return {'error': f'알 수 없는 job_id: {job_id}'}
        job['cancel'] = True
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

    file_seq는 eiass_get_project_documents가 반환한 stage_docs 안의 'seq' 값이다.
    """
    try:
        return core.download_document_text(file_seq, max_chars=max_chars)
    except core.EiassError as exc:
        return {'error': str(exc)}


@mcp.tool()
def eiass_export_matches_csv(rows_json: str, filename: str = '') -> dict:
    """조사 결과(eiass_find_projects_by_document_keyword / eiass_start_document_keyword_scan의
    matches)를 CSV 파일로 저장한다.

    문서 키워드 조사 결과를 사용자에게 보고할 때는 다음 두 가지를 항상 함께 한다:
    1) 아래와 같은 컬럼으로 마크다운 표를 작성해 채팅에 보여준다:
       `사업명 | eia_cd | 원문 파일명 | 유사내용 페이지번호 | 변경 내용 요약`
    2) 조사한 사업의 전체 리스트를 이 도구로 CSV 파일로 만들어 제시한다(생성된 경로를
       사용자에게 알려준다).

    Args:
        rows_json: JSON 배열 문자열. 각 원소는 아래 5개 키를 전부 포함한 객체여야 한다
            (하나도 빠짐없이, 이 키 이름 그대로):
            "사업명", "eia_cd", "원문 파일명", "유사내용 페이지번호", "변경 내용 요약".
            "변경 내용 요약"은 matched_snippets의 원문 발췌를 근거로 AI가 직접 작성해야
            한다(빈 문자열 금지) — 같은 사업/파일이라도 매칭된 위치(페이지)가 다르면
            행을 나눠서 각각 요약한다.
            예: '[{"사업명":"OO사업","eia_cd":"E12345","원문 파일명":"협의의견.pdf",
                "유사내용 페이지번호":"12","변경 내용 요약":"CALPUFF 모델 적용 관련 조건 추가"}]'
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
def eiass_check_protected_area_adjacency(address: str, radius_m: int = 1000) -> dict:
    """사업 위치(주소)와 인근 KDPA 보호지역(국립공원/천연기념물/습지보호지역/야생생물보호구역/OECM)의
    인접 여부와 거리를 확인한다. 내부적으로 VWorld로 지오코딩 후 KDPA WFS를 반경 검색한다.

    Args:
        address: 지번 또는 도로명 주소.
        radius_m: 검색 반경(미터). 기본 1000m.
    """
    try:
        coord = core.geocode_address(address)
    except core.EiassError as exc:
        return {'error': str(exc)}
    if not coord:
        return {'error': f'주소를 좌표로 변환하지 못했습니다: {address}'}
    lon, lat, source = coord
    areas = core.find_nearby_protected_areas(lon, lat, radius_m=radius_m)
    return {
        'address': address,
        'lon': lon, 'lat': lat, 'geocode_source': source,
        'radius_m': radius_m,
        'nearby_count': len(areas),
        'nearby_protected_areas': areas,
    }


@mcp.tool()
def eiass_version() -> dict:
    """이 EIASS MCP 서버(exe)의 버전을 반환한다. 설치된 버전과 최신 버전을 비교하고
    싶을 때 사용자에게 알려줄 용도로 쓴다(최신 버전 확인은 install.ps1/install.bat이 담당)."""
    return {'version': core.__version__}


@mcp.tool()
def eiass_geocode(address: str) -> dict:
    """주소를 VWorld API로 경위도 좌표로 변환한다."""
    try:
        coord = core.geocode_address(address)
    except core.EiassError as exc:
        return {'error': str(exc)}
    if not coord:
        return {'error': f'주소를 좌표로 변환하지 못했습니다: {address}'}
    lon, lat, source = coord
    return {'address': address, 'lon': lon, 'lat': lat, 'source': source}


if __name__ == '__main__':
    mcp.run(transport='stdio')
