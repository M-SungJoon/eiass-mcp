# EIASS MCP 서버

[EIASS](https://www.eiass.go.kr)(환경영향평가정보지원시스템) 사업 검색·상세조회·협의의견 원문 조회와, VWorld 지오코딩 + KDPA 보호지역 인접 조회를 Claude(AI)가 직접 쓸 수 있도록 MCP(Model Context Protocol) 도구로 제공합니다.

## 제공 도구

| 도구 | 기능 |
|---|---|
| `eiass_search_projects` | 사업명/협의완료일 범위/진행상태/기후변화영향평가/업종 등 필터로 사업 검색 |
| `eiass_find_projects_by_document_keyword` | 필터로 후보를 좁힌 뒤, 협의의견(등) 원문에서 키워드가 있는 사업만 추려서 반환 |
| `eiass_get_project_documents` | 사업 개요 필드 + 단계별(초안/본안/협의의견 등) 첨부문서 목록 조회 |
| `eiass_read_document` | 첨부 PDF를 다운로드해 텍스트 추출 |
| `eiass_check_protected_area_adjacency` | 주소 → 지오코딩 → 반경 내 KDPA 보호지역(국립공원/천연기념물/습지보호지역/야생생물보호구역/OECM) 조회 |
| `eiass_geocode` | 주소 → 경위도 좌표 |

## 설치 — 방법 1: exe로 실행 (Python 설치 불필요, 추천)

1. 이 저장소를 clone하거나 zip으로 받아서 `mcp_server.exe`를 꺼낸다(저장소에 이미 빌드되어 포함되어 있다. 직접 최신 소스로 다시 빌드하려면 아래 "직접 빌드하기" 참고).
2. 아무 폴더에나 저장한다 (예: `C:\Tools\eiass-mcp\mcp_server.exe`).
3. 같은 폴더에 `.env` 파일을 만들고 VWorld API 키를 넣는다 (지오코딩/보호구역 조회용, [VWorld 오픈API](https://www.vworld.kr/dev/v4api.do)에서 무료 발급):
   ```
   VWORLD_API_KEY=발급받은_키
   ```
4. Claude 설정에 등록한다 (아래 "Claude에 등록하기" 참고). `command`에 exe 경로를 직접 지정하면 되고 `args`는 필요 없다.

## 설치 — 방법 2: Python으로 실행

1. Python 3.10 이상 설치
2. 이 저장소를 clone하거나 zip으로 받는다
3. `pip install -r requirements.txt`
4. 같은 폴더에 `.env` 파일을 만들고 `VWORLD_API_KEY=...` 추가
5. Claude 설정에 등록 (아래 참고)

## Claude에 등록하기

### Claude Code
프로젝트 루트에 `.mcp.json`을 만든다 (exe 방식 예시):
```json
{
  "mcpServers": {
    "eiass": {
      "command": "C:/Tools/eiass-mcp/mcp_server.exe"
    }
  }
}
```
Python 방식이면:
```json
{
  "mcpServers": {
    "eiass": {
      "command": "python",
      "args": ["mcp_server.py"],
      "cwd": "C:/Tools/eiass-mcp"
    }
  }
}
```
Claude Code를 재시작하면 "eiass" 서버 신뢰 여부를 물어본다 → 승인.

### Claude Desktop
`%APPDATA%\Claude\claude_desktop_config.json`에 위와 동일한 형식으로 `mcpServers` 항목을 추가하고 Claude Desktop을 재시작한다.

## 직접 빌드하기

`eiass_core.py`/`mcp_server.py`를 수정한 뒤 exe를 새로 만들려면, **반드시 이 저장소 전용의 깨끗한 venv**에서 빌드해야 한다(시스템 Python에 다른 프로젝트용 패키지가 잔뜩 깔려 있으면 PyInstaller가 그것들까지 끌고 들어가 exe가 수백MB로 부풀고 느려진다):
```
python -m venv .mcpbuild_venv
.mcpbuild_venv\Scripts\pip install -r requirements-build.txt
.mcpbuild_venv\Scripts\python build_mcp.py
```

## 참고

- EIASS/KDPA는 정부 사이트 인증서 문제로 SSL 검증을 끄고 접속한다(원본 데스크톱 앱과 동일).
- `eiass_find_projects_by_document_keyword`의 텍스트 검색은 단순 부분문자열 매칭이다. 동의어/문맥 유사도까지 필요하면 1차 후보를 좁힌 뒤 `eiass_read_document`로 원문을 받아 AI가 다시 판단해야 한다.
- 이 저장소는 DOHWA EIASS agent 데스크톱 앱의 검색/조회 로직 일부를 PyQt 의존성 없이 재구현한 것이다.
