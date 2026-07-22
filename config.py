"""EIASS MCP 실행 한계와 영속 저장소 경로를 한곳에서 관리한다."""
import os
import tempfile


def bounded_env_int(name, default, minimum, maximum):
    """환경변수 정수를 안전한 실행 범위로 제한한다."""
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw not in (None, '') else default
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


APP_NAME = 'DOHWA EIASS Agent'
JOB_WORKER_COUNT = 2
JOB_QUEUE_SIZE = 32
JOB_RETENTION_SECONDS = 24 * 60 * 60
MAX_RETAINED_JOBS = 100
JOB_RESULT_PAGE_LIMIT = 500
MAX_SCAN_BATCH_SIZE = 25
MAX_DOCUMENT_SCAN_BATCH_SIZE = 10
JOB_HEARTBEAT_INTERVAL_SECONDS = 5
JOB_LEASE_TIMEOUT_SECONDS = 30
SCAN_MONITOR_POLL_SECONDS = bounded_env_int('EIASS_SCAN_MONITOR_POLL', 30, 5, 60)
SCAN_NORMAL_REPORT_SECONDS = bounded_env_int('EIASS_SCAN_NORMAL_REPORT', 60, 30, 300)
SCAN_UNCHANGED_REPORT_SECONDS = bounded_env_int('EIASS_SCAN_UNCHANGED_REPORT', 300, 60, 900)
PDF_PROCESS_START_TIMEOUT_SECONDS = bounded_env_int('EIASS_PDF_START_TIMEOUT', 15, 5, 60)
PDF_EXTRACT_TIMEOUT_SECONDS = bounded_env_int('EIASS_PDF_EXTRACT_TIMEOUT', 90, 30, 300)
# 100MB는 일반 문서 경계이고, 500MB는 안전장치를 적용한 절대 상한이다. 단순히 상한만
# 높이면 대형 PDF가 일반 다운로드/추출 슬롯을 점유하므로 크기별 정책과 별도 슬롯을 쓴다.
PDF_MAX_MB = bounded_env_int('EIASS_PDF_MAX_MB', 500, 100, 500)
PDF_MAX_BYTES = PDF_MAX_MB * 1024 * 1024
PDF_LARGE_THRESHOLD_BYTES = 100 * 1024 * 1024
PDF_XLARGE_THRESHOLD_BYTES = 300 * 1024 * 1024
PDF_LARGE_CONCURRENCY = 1
PDF_LARGE_DOWNLOAD_TIMEOUT_SECONDS = bounded_env_int(
    'EIASS_PDF_LARGE_DOWNLOAD_TIMEOUT', 600, 240, 900)
PDF_LARGE_WALL_TIMEOUT_SECONDS = bounded_env_int(
    'EIASS_PDF_LARGE_WALL_TIMEOUT', 900, 360, 1200)
PDF_LARGE_EXTRACT_TIMEOUT_SECONDS = bounded_env_int(
    'EIASS_PDF_LARGE_EXTRACT_TIMEOUT', 240, 90, 300)
PDF_XLARGE_DOWNLOAD_TIMEOUT_SECONDS = bounded_env_int(
    'EIASS_PDF_XLARGE_DOWNLOAD_TIMEOUT', 900, 600, 1200)
PDF_XLARGE_WALL_TIMEOUT_SECONDS = bounded_env_int(
    'EIASS_PDF_XLARGE_WALL_TIMEOUT', 1200, 900, 1800)
PDF_XLARGE_EXTRACT_TIMEOUT_SECONDS = bounded_env_int(
    'EIASS_PDF_XLARGE_EXTRACT_TIMEOUT', 300, 240, 600)
PDF_MIN_FREE_BYTES = bounded_env_int('EIASS_PDF_MIN_FREE_MB', 3072, 1024, 16384) * 1024 * 1024
PDF_DISK_SPACE_MULTIPLIER = 4
PDF_EXTRACT_RSS_LIMIT_BYTES = bounded_env_int(
    'EIASS_PDF_EXTRACT_RSS_LIMIT_MB', 2048, 512, 4096) * 1024 * 1024
PDF_MAX_PAGES = 3000
# 문서 스캔 처리량 튜닝.
# 한 배치의 문서를 이만큼 동시에 내려받아 캐시를 데운다(다운로드는 네트워크 대기라 병렬화
# 효과가 크다). EIASS는 정부 사이트라 과한 동시요청은 rate-limit/차단 위험이 있으니, 차단이
# 보이면 이 값을 낮춘다. 이 값은 job별이 아니라 프로세스 전체 상한으로도 사용한다.
# 환경변수가 잘못되거나 과도해도 서버 기동 실패/과부하가 나지 않게 1~10으로 제한한다.
DOC_DOWNLOAD_CONCURRENCY = bounded_env_int('EIASS_DOC_CONCURRENCY', 3, 1, 6)
PDF_EXTRACT_CONCURRENCY = bounded_env_int('EIASS_PDF_EXTRACT_CONCURRENCY', 2, 1, 4)
# 문서 다운로드 재시도 횟수(discovery와 달리 대량 다운로드는 빨리 실패하고 건너뛰는 게 낫다).
# 느린 문서 하나가 재시도로 스캔을 오래 잡는 것을 막는다(실패는 로그로 남기고 계속 진행).
DOC_DOWNLOAD_RETRY_TOTAL = bounded_env_int('EIASS_DOC_RETRY', 1, 0, 3)
DOC_CONNECT_TIMEOUT_SECONDS = bounded_env_int('EIASS_DOC_CONNECT_TIMEOUT', 8, 3, 30)
DOC_FIRST_BYTE_TIMEOUT_SECONDS = bounded_env_int('EIASS_DOC_FIRST_BYTE_TIMEOUT', 20, 5, 60)
DOC_DOWNLOAD_READ_TIMEOUT_SECONDS = bounded_env_int('EIASS_DOC_READ_TIMEOUT', 30, 10, 120)
DOC_DOWNLOAD_TOTAL_TIMEOUT_SECONDS = bounded_env_int('EIASS_DOC_TOTAL_TIMEOUT', 240, 60, 900)
DOC_LOW_SPEED_BYTES_PER_SECOND = bounded_env_int(
    'EIASS_DOC_LOW_SPEED_BPS', 128 * 1024, 16 * 1024, 5 * 1024 * 1024)
DOC_LOW_SPEED_GRACE_SECONDS = bounded_env_int('EIASS_DOC_LOW_SPEED_GRACE', 30, 10, 120)
DOC_LOW_SPEED_WINDOW_SECONDS = bounded_env_int('EIASS_DOC_LOW_SPEED_WINDOW', 60, 20, 180)
DOC_TOTAL_TIMEOUT_SECONDS = bounded_env_int('EIASS_DOC_WALL_TIMEOUT', 360, 120, 1200)
DOC_PROGRESS_INTERVAL_SECONDS = bounded_env_int('EIASS_DOC_PROGRESS_INTERVAL', 5, 1, 30)
DETAIL_PREFETCH_READ_TIMEOUT_SECONDS = bounded_env_int('EIASS_DETAIL_READ_TIMEOUT', 15, 5, 60)
DOC_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
DOC_CACHE_MAX_CHARS = 100 * 1024 * 1024
DOC_CACHE_MAX_ITEM_CHARS = bounded_env_int(
    'EIASS_DOC_CACHE_MAX_ITEM_MB', 25, 5, 100) * 1024 * 1024
DETAIL_CACHE_TTL_SECONDS = 60 * 60
DETAIL_CACHE_MAX_ITEMS = 512

# 적응형 조사는 사용자 선택 후에만 실행되며, 이 기본값은 AI가 제시한 전략 JSON의 빈 필드를
# 안전 범위로 채운다. 완전성 신호가 부족하면 자동으로 전수조사로 승격된다.
ADAPTIVE_INITIAL_SAMPLE_SIZE = bounded_env_int('EIASS_ADAPTIVE_INITIAL_SAMPLE', 30, 10, 100)
ADAPTIVE_ROUND_SIZE = bounded_env_int('EIASS_ADAPTIVE_ROUND_SIZE', 30, 10, 100)
ADAPTIVE_AUDIT_SIZE = bounded_env_int('EIASS_ADAPTIVE_AUDIT_SIZE', 10, 5, 50)
ADAPTIVE_MAX_CANDIDATES = bounded_env_int('EIASS_ADAPTIVE_MAX_CANDIDATES', 300, 30, 1000)
ADAPTIVE_MAX_PERCENT = bounded_env_int('EIASS_ADAPTIVE_MAX_PERCENT', 60, 25, 90)
ADAPTIVE_MIN_PERCENT = bounded_env_int('EIASS_ADAPTIVE_MIN_PERCENT', 25, 10, 80)
ADAPTIVE_SATURATION_ROUNDS = bounded_env_int('EIASS_ADAPTIVE_SATURATION_ROUNDS', 2, 1, 5)
ADAPTIVE_MAX_FAILURE_PERCENT = bounded_env_int('EIASS_ADAPTIVE_MAX_FAILURE_PERCENT', 5, 0, 25)
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
    override = os.environ.get('EIASS_JOB_DB_PATH')
    if override:
        return os.path.abspath(os.path.expandvars(override))
    return os.path.join(app_data_dir(), 'mcp_jobs.sqlite3')


def shared_limit_db_path():
    """여러 MCP 프로세스가 같은 PC에서 공유하는 다운로드/추출 슬롯 저장소."""
    override = os.environ.get('EIASS_LIMIT_DB_PATH')
    if override:
        return os.path.abspath(os.path.expandvars(override))
    return os.path.join(app_data_dir(), 'mcp_limits.sqlite3')
