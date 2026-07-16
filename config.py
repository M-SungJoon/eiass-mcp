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
