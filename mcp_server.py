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
from mcp.server.fastmcp import FastMCP

import eiass_core as core

mcp = FastMCP('eiass')


@mcp.tool()
def eiass_search_projects(keyword: str = '', types: str = '', agency_code: str = '', max_pages: int = 2,
                           consult_date_from: str = '', consult_date_to: str = '',
                           progress_status: str = '', climate_filter: str = '', biz_gubun: str = '') -> dict:
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
        max_pages: 평가종류별 최대 조회 페이지 수(1페이지=100건). 과도한 조회 방지를 위해 기본 2.
        consult_date_from: 'YYYY-MM-DD'. 협의완료일(사후조사는 조사년도) 하한.
        consult_date_to: 'YYYY-MM-DD'. 협의완료일(사후조사는 조사년도) 상한.
            예: "최근 1년" → consult_date_from=오늘로부터 1년 전, consult_date_to=오늘.
        progress_status: '완료' | '진행' | ''. 진행현황 필터.
        climate_filter: 'Y' | 'N' | ''. 기후변화영향평가 대상 여부(사후조사 제외).
        biz_gubun: 업종(사업구분) 필터. 다음 라벨 중 정확히 일치해야 한다(사후환경영향조사는 미지원):
            도시의 개발, 산업입지 및 산업단지의 조성, 에너지 개발, 항만 건설, 도로의 건설,
            수자원의 개발, 철도(도시철도 포함)의 건설, 공항 또는 비행장의 건설, 하천의 이용 및 개발,
            개간 및 공유수면의 매립, 관광단지의 개발, 지역개발/특정지역의 개발, 체육시설의 설치,
            폐기물처리시설 및 분뇨처리시설의 설치, 국방군사시설의 설치, 토석·모래·자갈·광물 등의 채취,
            산지의 개발, 기타
    """
    type_codes = [c.strip().upper() for c in types.split(',') if c.strip()] or None
    try:
        results = core.search_projects(
            keyword, type_codes=type_codes, agency_code=agency_code,
            max_pages=max(1, min(max_pages, core.MAX_SEARCH_PAGES)),
            consult_date_from=consult_date_from or None, consult_date_to=consult_date_to or None,
            progress_status=progress_status, climate_filter=climate_filter, biz_gubun=biz_gubun,
        )
    except core.EiassError as exc:
        return {'error': str(exc)}
    return {'count': len(results), 'projects': results}


@mcp.tool()
def eiass_find_projects_by_document_keyword(
    text_query: str, keyword: str = '', types: str = '', agency_code: str = '',
    consult_date_from: str = '', consult_date_to: str = '', progress_status: str = '완료',
    biz_gubun: str = '', stages: str = '협의의견', max_pages: int = 2, max_candidates: int = 30,
) -> dict:
    """필터(협의완료일 범위/진행상태 등)로 사업을 좁힌 뒤, 지정한 단계(기본 협의의견)의
    첨부 PDF 원문에서 text_query가 포함된 사업만 골라 리스트로 반환한다.

    "최근 1년 내 협의완료된 사업 중 협의의견에 원형보전지 관련 내용이 있는 사업을 찾아줘"
    같은 요청은 이 도구 하나로 처리한다:
      eiass_find_projects_by_document_keyword(text_query="원형보전지",
          consult_date_from="2025-07-09", consult_date_to="2026-07-09", progress_status="완료")

    주의: text_query는 단순 부분문자열 매칭이다("원형보전지"가 그대로 등장해야 함). 동의어나
    문맥상 유사 표현까지 잡으려면, 이 도구로 1차 후보를 좁힌 뒤 필요시 eiass_read_document로
    각 사업의 원문 전체를 받아 직접 의미 단위로 재검토하라. stages를 "초안,본안"처럼 넓히면
    검토의견/본문에서도 찾을 수 있지만 다운로드량이 늘어 느려진다.

    Args:
        text_query: 문서 원문에서 찾을 문자열(예: '원형보전지').
        keyword: 사업명 검색 키워드(선택). 비워도 날짜/상태 필터만으로 후보를 좁힐 수 있다.
        types: 평가종류 코드 콤마 구분. 비우면 전체.
        agency_code: 협의기관 코드(선택).
        consult_date_from/consult_date_to: 'YYYY-MM-DD'. 협의완료일 범위.
        progress_status: '완료' | '진행' | ''. 기본 '완료'(협의의견은 완료 건에만 존재).
        biz_gubun: 업종(사업구분) 필터 라벨(예: '산업입지 및 산업단지의 조성'). 정확한 목록은
            eiass_search_projects 설명 참고. 사후환경영향조사는 미지원.
        stages: 검색 대상 단계, 콤마 구분(기본 '협의의견'). 예: '협의의견,초안'.
        max_pages: 평가종류별 최대 검색 페이지 수.
        max_candidates: 원문까지 내려받아 확인할 최대 후보 수(비용 제한, 기본 30).
    """
    type_codes = [c.strip().upper() for c in types.split(',') if c.strip()] or None
    stage_list = tuple(s.strip() for s in stages.split(',') if s.strip()) or ('협의의견',)
    try:
        return core.search_projects_by_document_keyword(
            text_query, keyword=keyword, type_codes=type_codes, agency_code=agency_code,
            consult_date_from=consult_date_from or None, consult_date_to=consult_date_to or None,
            progress_status=progress_status, biz_gubun=biz_gubun, stages=stage_list,
            max_pages=max(1, min(max_pages, core.MAX_SEARCH_PAGES)), max_candidates=max_candidates,
        )
    except core.EiassError as exc:
        return {'error': str(exc)}


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

    file_seq는 eiass_get_project_documents가 반환한 stage_docs 안의 'seq' 값이다.
    """
    try:
        return core.download_document_text(file_seq, max_chars=max_chars)
    except core.EiassError as exc:
        return {'error': str(exc)}


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
