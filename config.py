"""EIASS MCP 실행 한계와 영속 저장소 경로를 한곳에서 관리한다."""
import os
import tempfile


APP_NAME = 'DOHWA EIASS Agent'
JOB_WORKER_COUNT = 2
JOB_QUEUE_SIZE = 32
JOB_RETENTION_SECONDS = 24 * 60 * 60
MAX_RETAINED_JOBS = 100
JOB_RESULT_PAGE_LIMIT = 500
PDF_EXTRACT_TIMEOUT_SECONDS = 90
PDF_MAX_BYTES = 100 * 1024 * 1024
PDF_MAX_PAGES = 3000
DOC_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
DOC_CACHE_MAX_CHARS = 100 * 1024 * 1024
DETAIL_CACHE_TTL_SECONDS = 60 * 60
DETAIL_CACHE_MAX_ITEMS = 512


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
