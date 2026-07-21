"""EIASS MCP 실행 한계와 영속 저장소 경로를 한곳에서 관리한다."""
import os
import tempfile


APP_NAME = 'DOHWA EIASS Agent'
JOB_WORKER_COUNT = 2
JOB_QUEUE_SIZE = 32
JOB_RETENTION_SECONDS = 24 * 60 * 60
MAX_RETAINED_JOBS = 100
JOB_RESULT_PAGE_LIMIT = 500
JOB_HEARTBEAT_INTERVAL_SECONDS = 5
JOB_LEASE_TIMEOUT_SECONDS = 30
PDF_EXTRACT_TIMEOUT_SECONDS = 90
PDF_MAX_BYTES = 100 * 1024 * 1024
PDF_MAX_PAGES = 3000
# 문서 스캔 처리량 튜닝.
# 한 배치의 문서를 이만큼 동시에 내려받아 캐시를 데운다(다운로드는 네트워크 대기라 병렬화
# 효과가 크다). EIASS는 정부 사이트라 과한 동시요청은 rate-limit/차단 위험이 있으니, 차단이
# 보이면 이 값을 낮춘다. 환경변수 EIASS_DOC_CONCURRENCY로도 덮어쓸 수 있다.
DOC_DOWNLOAD_CONCURRENCY = int(os.environ.get('EIASS_DOC_CONCURRENCY') or 10)
# 문서 다운로드 재시도 횟수(discovery와 달리 대량 다운로드는 빨리 실패하고 건너뛰는 게 낫다).
# 느린 문서 하나가 재시도로 스캔을 오래 잡는 것을 막는다(실패는 로그로 남기고 계속 진행).
DOC_DOWNLOAD_RETRY_TOTAL = int(os.environ.get('EIASS_DOC_RETRY') or 1)
DOC_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
DOC_CACHE_MAX_CHARS = 100 * 1024 * 1024
DETAIL_CACHE_TTL_SECONDS = 60 * 60
DETAIL_CACHE_MAX_ITEMS = 512
# 실패 원인을 설명하려고 찍어본 서비스 상태를 재사용하는 시간. 장애 중에는 스캔의 모든 항목이
# 실패하는데 항목마다 서비스를 다시 찔러보면 진단이 본 작업보다 비싸진다. 설명에만 쓰고
# 호출을 막는 데는 쓰지 않는다.
HEALTH_CACHE_TTL_SECONDS = 60


def app_data_dir():
    preferred = os.path.join(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir(), APP_NAME)
    try:
        os.makedirs(preferred, exist_ok=True)
        return preferred
    except OSError:
        fallback = os.path.join(tempfile.gettempdir(), APP_NAME)
        os.makedirs(fallback, exist_ok=True)
        return fallback


def job_db_path():
    return os.path.join(app_data_dir(), 'mcp_jobs.sqlite3')
