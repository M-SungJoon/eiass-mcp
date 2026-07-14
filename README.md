# EIASS MCP 서버

[EIASS](https://www.eiass.go.kr)(환경영향평가정보지원시스템) 사업 검색·상세조회·협의의견 원문 조회와, VWorld 지오코딩 + KDPA 보호지역 인접 조회를 Claude(AI)가 직접 쓸 수 있도록 MCP(Model Context Protocol) 도구로 제공합니다.

## 제공 도구

| 도구                                        | 기능                                                                                                                                        |
| ----------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `eiass_search_projects`                   | 사업명/협의완료일 범위/진행상태/진행구분/기후변화영향평가/사업유형 등 필터로 사업 검색                                                                                          |
| `eiass_preview_search`                    | 실제 조회 없이 검색조건/문서범위/예상 후보·문서 수/과거 패턴 힌트를 확인 문구로 반환                                                                                         |
| `eiass_find_projects_by_document_keyword` | 필터로 후보를 좁힌 뒤, 지정 단계(기본 협의의견) 원문에서 키워드가 있는 사업만 추려서 반환. `confirmed=true` 없이는 미리보기만 반환(아래 "실행 전 확인" 참고). 소규모(~50건) 조회용, `offset`으로 이어서 조회 가능 |
| `eiass_start_document_keyword_scan`       | 대량 후보(수백 건)를 타임아웃 없이 끝까지 훑는 백그라운드 스캔 시작. `confirmed=true`일 때만 실제로 시작하고 즉시 `job_id` 반환                                                     |
| `eiass_get_scan_status`                   | `job_id`로 스캔 진행 상황·중간/최종 매칭 결과 조회(스캔 중에도 즉시 응답)                                                                                           |
| `eiass_cancel_scan`                       | 진행 중인 백그라운드 스캔 취소(즉시 응답)                                                                                                                  |
| `eiass_get_project_documents`             | 사업 개요 필드 + 단계별(초안/본안/협의의견 등) 첨부문서 목록 조회                                                                                                   |
| `eiass_read_document`                     | 첨부 PDF를 다운로드해 텍스트 추출(로컬 캐시 우선)                                                                                                            |
| `eiass_check_protected_area_adjacency`    | 주소 → 지오코딩 → 반경 내 KDPA 보호지역(국립공원/천연기념물/습지보호지역/야생생물보호구역/OECM) 조회                                                                            |
| `eiass_geocode`                           | 주소 → 경위도 좌표                                                                                                                               |
| `eiass_export_matches_csv`                | 조사 결과(사업명/eia_cd/원문 파일명/유사내용 페이지번호/변경 내용 요약)를 CSV 파일로 저장                                                                                       |
| `eiass_version`                           | 현재 실행 중인 서버(exe)의 버전 반환                                                                                                                    |

### 실행 전 확인(confirm) 게이트

`eiass_find_projects_by_document_keyword`/`eiass_start_document_keyword_scan`은 **`confirmed=true`를 명시적으로 넘기지 않으면 실제로 문서를 다운로드하지 않는다.** 대신 아래 10개 항목을 **이 순서 그대로, 하나도 빠짐없이** 담은 확인 문구를 반환한다 — 사용자가 언급하지 않은 필터는 항상 `전체`로 표시:

1. 평가종류
2. 사업유형
3. 협의기관
4. 협의완료일
5. 진행현황(완료/진행중/전체)
6. 진행구분
7. 확인 문서 범위
8. 키워드 매칭
9. 예상 후보 사업 수
10. 예상 확인 문서 수

추가로:
- AI가 사용자 발화 이상으로 추론/제안해서 좁힌 조건이 있다면 `inference_notes`로 별도 표시(비워두면 "AI가 임의로 좁힌 조건 없음")
- 과거 유사 조건(같은 평가종류+사업유형) 기록이 있으면 우선순위 힌트로만 표시 — **검색 범위를 줄이는 근거로 쓰지 않으며, 신뢰도(표본 수 기준 low/medium/high)를 함께 표시한다**

사용자 승인 후 **같은 조건 그대로 `confirmed=true`만 추가**해서 다시 호출해야 실제로 실행된다.

### 조사 결과 보고 형식

`eiass_find_projects_by_document_keyword`/`eiass_start_document_keyword_scan`으로 조사를 마치면 AI는 항상:

1. `사업명 | eia_cd | 원문 파일명 | 유사내용 페이지번호 | 변경 내용 요약` 컬럼의 마크다운 표로 결과를 채팅에 보여주고,
2. 같은 행 데이터를 `eiass_export_matches_csv`로 CSV 파일로도 만들어 저장 경로를 사용자에게 안내한다.

`변경 내용 요약`은 기계적으로 만들 수 없으므로, `matches[].matched_snippets`의 원문 발췌를 근거로 AI가 직접 작성해서 채운다(빈 값 금지). CSV는 기본적으로 사용자 Downloads 폴더에 `utf-8-sig`(엑셀 호환)로 저장된다.

### 오탐(참고문헌/부록) 감지와 대응

실행 결과에는 `needs_refinement`(매칭이 과도하거나 참고문헌/부록 문맥으로 보이는 비율이 높으면 true)와 `refinement_hint`가 포함된다. true면 바로 최종 답을 내지 말고 사용자에게 문맥 조건 추가 여부를 물어봐야 한다. 각 `matched_snippets` 항목에도 `reference_like` 플래그가 있어 개별 매칭이 본문인지 참고문헌류인지 구분할 수 있다.

### 범위 밖 표본 검증 (scan scope audit)

`audit_sample_size`(기본 0)를 지정하면, 요청한 stages 밖의 다른 단계도 이번 배치 중 일부를 표본 검증해 `audit_sample`로 함께 반환한다. 좁힌 범위 밖에서도 매칭이 있을 수 있다는 걸 알려주는 안전장치이며, 전수조사가 아니므로 "매칭 없음"이 "확실히 없음"을 뜻하지는 않는다.

### 진행구분 다중선택 필터

`progress_stage`로 사업 자체의 진행구분(첨부문서 stage와는 다른 축)을 다중선택할 수 있다. 사용 가능 라벨: `초안`, `평가서`, `재협의`, `약식평가`, `변경협의`(콤마로 여러 개 지정, 예: `"초안,재협의"`). 비우면 원본 앱 기본값과 동일하게 5개 전부 선택된 것으로 취급한다(=전체, 필터 없음). 알 수 없는 라벨을 넣으면 사용 가능한 라벨 목록과 함께 에러를 반환한다. `eiass_search_projects`/`eiass_preview_search`/`eiass_find_projects_by_document_keyword`/`eiass_start_document_keyword_scan` 모두에 적용된다.

### 문서 제목(항목) 필터

`stages`가 문서 단계(초안/본안/협의의견 등)를 고르는 것과 별개로, `doc_title_contains`로 그 단계 안에서도 **파일명에 특정 단어가 포함된 문서만** 확인 대상으로 좁힐 수 있다. 초안/본안/보완 등은 챕터별로 PDF가 쪼개져 있고 파일명에 챕터명이 그대로 들어있다(예: `(본안) 0922 대기질(사업명).pdf`). "모든 단계의 대기질 항목만 확인"처럼 요청하면 `stages="초안,본안,보완"` + `doc_title_contains="대기질,기상"`으로 처리한다 — 실측 사례에서 예상 확인 문서 수가 758건 → 46건으로 줄었다.

단순 파일명 문자열 매칭(대소문자 무시)이라 "대기질 항목"을 의미 단위로 이해하는 건 아니다. 실제 챕터 파일명 표기와 다른 용어를 쓰면 관련 문서를 놓칠 수 있으므로, `eiass_preview_search`의 `estimated_documents`가 기대와 다르면 용어를 조정해서 다시 미리보기 하는 것을 권장한다. `audit_sample_size`와 함께 쓰면 stages/제목 필터로 좁힌 범위 밖도 일부 표본 검증할 수 있다.

### 대량 문서 키워드 검색이 빨라진 이유

첨부 PDF를 file_seq 기준으로 로컬 SQLite에 캐시하고(`%LOCALAPPDATA%\DOHWA EIASS Agent\doc_text_cache.sqlite3`), 사업 상세조회 결과도 서버 프로세스가 살아있는 동안 메모리에 캐시한다. 그래서:

- `text_queries="CALPUFF,CMAQ"`처럼 **여러 키워드를 한 번에** 넘기면 문서를 한 번만 열어서 전부 확인한다(키워드 수만큼 반복 다운로드하지 않음).
- 같은 후보군을 **다른 키워드로 다시 조회**하거나 `offset`으로 **이어서 조회**해도 이미 받은 문서는 재다운로드하지 않는다 (실측: 같은 배치를 다른 키워드로 재조회 시 5초대 → 0.3초대).
- 후보가 많아 한 번의 호출로는 끝낼 수 없을 때는 `eiass_start_document_keyword_scan`으로 백그라운드에 맡기고 `eiass_get_scan_status`로 폴링하면, MCP 호출 하나의 타임아웃과 무관하게 끝까지 진행된다.
- 같은 (평가종류+사업유형) 조합으로 실제 실행된 검색은 단계별 확인/매칭 건수가 로컬 패턴 캐시에 누적되어, 다음 유사 요청의 `eiass_preview_search`에서 우선순위 힌트로 쓰인다(범위 축소 근거로는 쓰이지 않음).

## 설치 — 방법 1: exe로 실행 (Python 설치 불필요, 추천)

1. 이 저장소를 clone하거나 zip으로 받아서 `mcp_server.exe`를 꺼낸다(저장소에 이미 빌드되어 포함되어 있다. 직접 최신 소스로 다시 빌드하려면 아래 "직접 빌드하기" 참고).

2. 아무 폴더에나 저장한다 (예: `C:\Tools\eiass-mcp\mcp_server.exe`).

3. 같은 폴더에 `.env` 파일을 만들고 VWorld API 키를 넣는다 (지오코딩/보호구역 조회용, [VWorld 오픈API](https://www.vworld.kr/dev/v4api.do)에서 무료 발급):
   
   ```
   VWORLD_API_KEY=발급받은_키
   ```

4. Claude/Codex에 등록한다 — `install.bat`을 더블클릭하면 자동으로 등록된다(아래 "자동 등록" 참고). 수동으로 하려면 "Claude에 등록하기" 참고.

## 자동 등록 + 업데이트 (Claude Code + Codex CLI)

`claude`, `codex` CLI가 PC에 설치되어 있으면 아래 스크립트가 둘 다 자동으로 등록해준다(찾지 못한 CLI는 건너뛴다). VWorld API 키도 대화형으로 물어봐서 `.env`까지 만들어준다.

**PowerShell이 익숙하지 않다면 `install.bat`을 그냥 더블클릭하면 된다** — 우클릭 메뉴나 실행 정책(ExecutionPolicy) 같은 걸 몰라도 된다. 콘솔 창에서 결과를 보여준 뒤 Enter를 눌러야 닫히므로, 성공/실패 여부를 놓치지 않고 확인할 수 있다.

터미널에서 직접 실행하고 싶다면:
```
powershell -ExecutionPolicy Bypass -File install.ps1
```

실행 후 Claude Code/Codex를 재시작하면 `eiass_*` 도구를 바로 쓸 수 있다. Claude Desktop은 CLI가 없어 자동 등록은 지원하지 않고, 실행 후 안내되는 JSON 스니펫을 `claude_desktop_config.json`에 직접 추가하면 된다.

**git 없이도 최신 버전으로 갱신된다.** `install.ps1`은 실행될 때마다 GitHub API로 `mcp_server.exe`의 최신 커밋을 확인하고(같은 폴더의 `.eiass_mcp_version`에 기록된 커밋과 비교), 다르면 `raw.githubusercontent.com`에서 새 exe를 직접 받아 교체한다 — git clone이나 pull이 전혀 필요 없다. `mcp_server.exe`가 아예 없는 상태로 `install.bat`/`install.ps1`만 받아서 실행해도 최초 1회 자동으로 받아온다. 저장소를 push해서 서버 쪽을 고쳐도 각 사용자 PC에는 자동으로 반영되지 않으므로, **업데이트를 받으려면 이 스크립트를 다시 실행해야 한다**(완전 자동 업데이트는 아님). Claude Code/Codex가 실행 중이라 exe가 잠겨 있으면 업데이트를 건너뛰고 기존 버전으로 계속 진행하니, 안내 메시지가 뜨면 앱을 완전히 종료한 뒤 다시 실행한다. 업데이트를 건너뛰려면 `-SkipUpdateCheck` 옵션을 준다.

### 버전 확인

커밋 ID만으로는 어느 게 더 최신인지 한눈에 알기 어려워서, 저장소 루트에 사람이 읽는 [`VERSION`](VERSION) 파일(예: `1.1.0`, [시맨틱 버저닝](https://semver.org/lang/ko/))을 둔다. `install.ps1`/`install.bat`을 실행하면 매번 (SkipUpdateCheck 여부와 무관하게) 아래 두 줄을 항상 보여준다:

```
현재 설치된 버전: 1.1.0 (commit ff4a6a6)
Git에 푸시된 최신 버전: 1.1.0 (commit ff4a6a6)
```

업데이트 필요 여부 자체는 지금까지처럼 `mcp_server.exe`의 커밋 SHA로 정확히 판단하고, `VERSION`은 그 결과를 사람이 읽기 좋게 보여주는 용도다. 로컬 상태는 `.eiass_mcp_version` 파일에 `커밋 SHA` / `버전` 두 줄로 저장된다. Claude 안에서 직접 확인하고 싶으면 `eiass_version` 도구로 현재 연결된 서버(exe)의 버전을 물어볼 수 있다.

**exe/소스를 수정해서 새로 배포할 때는 반드시 `VERSION` 파일과 `eiass_core.py`의 `__version__` 상수를 같이 올린다**(시맨틱 버저닝: 기존 도구의 동작이 바뀌지 않는 자잘한 수정=PATCH, 새 도구/필드 추가처럼 하위 호환되는 기능 추가=MINOR, 기존 도구 시그니처/동작이 깨지는 변경=MAJOR).

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
