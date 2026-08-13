# EIASS MCP 서버

[EIASS](https://www.eiass.go.kr)(환경영향평가정보지원시스템)의 사업 검색·상세조회·협의의견 원문 조회와, VWorld 지오코딩 + KDPA 보호지역 인접 조회를 Claude 같은 AI가 직접 쓸 수 있도록 MCP(Model Context Protocol) 도구로 제공합니다.

> 이 저장소는 **배포 전용**입니다. 설치 스크립트와 릴리즈만 여기에 있고, 소스 코드는 비공개 저장소에서 관리합니다.

## 설치 (Windows)

**[`install.bat`](install.bat) 하나만 받아서 더블클릭하면 끝납니다.** 터미널을 열 필요도, 명령을 칠 필요도, 설치 폴더를 고를 필요도 없습니다. 배포본 다운로드 → VWorld API 키 입력 → AI 클라이언트 등록까지 한 번에 처리합니다.

터미널이 편하시면 아래 한 줄도 같은 일을 합니다:

```powershell
iex ((irm https://raw.githubusercontent.com/M-SungJoon/eiass-mcp/main/install.ps1).TrimStart([char]0xFEFF))
```

설치 위치는 `%LOCALAPPDATA%\Programs\EIASS MCP`로 고정됩니다. 경로가 고정돼 있어 업데이트해도 재등록이 필요 없습니다.

```
C:\Users\<사용자>\AppData\Local\Programs\EIASS MCP\
  .env                     ← VWorld API 키. 업데이트해도 유지됩니다.
  EIASS MCP 업데이트.bat   ← 다음 업데이트 때 더블클릭
  mcp_server\              ← 배포본. 업데이트 때 이 폴더만 통째로 교체됩니다.
```

### VWorld API 키

보호구역 인접 조회(지오코딩)에 필요합니다. [VWorld 오픈API](https://www.vworld.kr/dev/v4api.do)에서 무료로 발급받을 수 있고, 설치 중에 물어보므로 붙여넣기만 하면 됩니다.

직접 만들려면 `mcp_server` 폴더 **안이 아니라 그 위(설치 폴더)에** `.env`를 두세요 — 업데이트가 `mcp_server` 폴더를 통째로 갈아끼우기 때문입니다.

```
VWORLD_API_KEY=발급받은_키
```

### 지원 클라이언트

Claude Code, Claude Desktop, Codex CLI, Antigravity에 자동 등록됩니다. 설치 후 해당 앱을 재시작하면 `eiass_*` 도구를 바로 쓸 수 있습니다.

### 이미 예전 방식으로 설치했다면

`install.bat`을 한 번 실행하면 자동으로 이주됩니다. VWorld API 키를 새 위치로 옮기고, 등록을 갱신하고, 예전 배포본을 정리합니다. 키를 다시 발급받을 필요는 없습니다.

## 업데이트

설치 폴더의 **`EIASS MCP 업데이트.bat`을 더블클릭**하면 됩니다(처음 받은 `install.bat`을 다시 실행해도 같습니다). 최신 릴리즈를 확인해 다르면 내려받아 검증한 뒤 교체합니다.

v2.0.0부터는 **서버가 스스로 알려줍니다.** 구버전이면 도구를 실행하기 전에 멈추고 업데이트 여부를 먼저 물어봅니다. "나중에 하겠다"고 답하면 원래 요청한 작업을 그대로 이어서 진행하고, 그날은 다시 묻지 않습니다.

AI 클라이언트가 실행 중이라 파일이 잠겨 있으면 교체를 건너뛰고 기존 버전으로 계속 진행하니, 안내가 뜨면 앱을 완전히 종료한 뒤 다시 실행하세요.

## 제공 도구

| 도구                                             | 기능                                                                                        |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `eiass_search_projects`                        | 사업명/협의완료일 범위/진행상태/진행구분/기후변화영향평가/사업유형 등 필터로 사업 검색                 |
| `eiass_preview_search`                         | 실제 조회 없이 검색조건·문서범위·예상 후보/문서 수를 확인 문구로 반환                          |
| `eiass_find_projects_by_document_keyword`      | 후보를 좁힌 뒤 보고서 원문에서 키워드가 있는 사업만 추려서 반환 (소규모 ~50건)                 |
| `eiass_start_document_keyword_scan`            | 대량 후보(수백 건)를 타임아웃 없이 끝까지 훑는 백그라운드 스캔 시작                            |
| `eiass_get_scan_status` / `eiass_cancel_scan`  | 스캔 진행 상황·중간/최종 결과 조회, 진행 중인 스캔 취소                                       |
| `eiass_get_project_documents`                  | 사업 개요 + 단계별(초안/본안/협의의견 등) 첨부문서 목록 조회                                   |
| `eiass_read_document`                          | 첨부 PDF를 다운로드해 텍스트 추출                                                              |
| `eiass_check_protected_area_adjacency`         | 주소 → 지오코딩 → 반경 내 보호지역(국립공원/천연기념물/습지보호지역/야생생물보호구역/OECM) 조회 |
| `eiass_check_project_protected_area_adjacency` | EIASS 사업지 주소(도로명·지번 복합 표기) 전용 보호구역 인접조회                                 |
| `eiass_find_projects_protected_area_adjacency` | 검색 필터로 후보를 뽑은 뒤 사업지별 보호구역 인접까지 한 번에 확인                             |
| `eiass_start_spatial_scan`                     | 대량 후보의 보호구역 인접조회를 백그라운드로 실행                                              |
| `eiass_geocode`                                | 주소 → 경위도 좌표                                                                             |
| `eiass_export_matches_csv`                     | 문서 키워드 조사 결과를 CSV로 저장 (스캔한 전체 사업 기준)                                      |
| `eiass_export_spatial_matches_csv`             | 보호구역 인접 조회 결과를 CSV로 저장 (반경 밖 사업도 포함)                                      |
| `eiass_check_server_status`                    | EIASS·VWorld·KDPA 각 서비스의 접속 가능 여부·응답시간 점검                                     |
| `eiass_version`                                | 설치된 버전과 배포된 최신/안정 버전 확인                                                       |

## 쓰기 전에 알아두면 좋은 것

### 실행 전 확인 절차

대량 문서를 내려받는 도구는 **사용자 승인 없이 실행되지 않습니다.** 먼저 적용될 검색 조건을 전부 나열한 확인 문구를 보여주고, 승인해야 실제로 조회합니다.

이때 AI가 사용자가 말하지 않은 조건을 임의로 좁혔다면 그 항목에 `← AI 추론` 표시가 붙고, 이유가 따로 표시됩니다. 의도한 조건이 아니면 승인하지 말고 알려주시면 됩니다.

```
적용 조건:
- 사업명 키워드: 전체
- 평가종류: 환경영향평가   ← AI 추론

⚠️ 아래 조건은 사용자가 직접 말하지 않아 AI가 추론한 것입니다:
- 평가종류: 환경영향평가

추론 이유: 사용자는 '산업단지 사례'라고만 했음
```

### 조사 결과는 "스캔한 전체" 기준

결과 표에는 키워드가 발견된 사업만이 아니라 **이번에 확인한 모든 사업**이 들어갑니다. 문서를 열어봤지만 키워드가 없던 사업, 첨부문서가 없어 건너뛴 사업도 함께 표시되므로, 조사 범위를 그대로 확인할 수 있습니다.

### 무료 플랜 사용자용 경량 모드

MCP 도구 정의는 대화마다 통째로 AI 컨텍스트에 실립니다. 무료 플랜은 한도가 빡빡해 이것만으로도 버거울 수 있어, 도구 수와 응답 크기를 줄인 경량 모드를 제공합니다. 설치 폴더의 `.env`에 아래를 추가하고 클라이언트를 재시작하세요.

```
EIASS_PROFILE=lite
```

## 문제가 생기면

- **도구가 안 보임**: 설치 후 AI 클라이언트를 완전히 종료했다가 다시 켜세요.
- **보호구역 조회만 실패**: VWorld API 키가 없거나 잘못된 경우입니다. 설치 폴더의 `.env`를 확인하세요.
- **검색이 전부 실패**: `eiass_check_server_status`로 EIASS 사이트 자체의 장애 여부를 확인할 수 있습니다.
- **업데이트 안내를 끄고 싶음**: 외부 통신이 막힌 환경이라면 `.env`에 `EIASS_UPDATE_CHECK=0`을 추가하세요.

## 라이선스·문의

[Issues](https://github.com/M-SungJoon/eiass-mcp/issues)로 문의해 주세요. 버전별 변경 내용은 [Releases](https://github.com/M-SungJoon/eiass-mcp/releases)에서 확인할 수 있습니다.
