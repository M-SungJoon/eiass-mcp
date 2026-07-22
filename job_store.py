"""재시작 가능한 MCP 스캔 작업 저장소.

후보와 결과를 별도 행으로 저장해 긴 스캔의 결과 본문이 서버 메모리에 누적되지 않게 한다.
"""
import json
import os
import sqlite3
import threading
import time
import tempfile
import uuid

from config import (JOB_LEASE_TIMEOUT_SECONDS, JOB_RETENTION_SECONDS,
                    MAX_RETAINED_JOBS, job_db_path, shared_limit_db_path)
from models import candidate_key


_PROCESS_INSTANCE_ID = uuid.uuid4().hex


class _ClosingConnection(sqlite3.Connection):
    """Windows에서도 WAL 파일 핸들을 남기지 않는 SQLite 연결."""
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class JobStore:
    def __init__(self, path=None):
        self.path = path or job_db_path()
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.lock = threading.RLock()
        try:
            self._init_db()
        except sqlite3.OperationalError:
            if path:
                raise
            self.path = os.path.join(tempfile.gettempdir(), 'DOHWA EIASS Agent', 'mcp_jobs.sqlite3')
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30, factory=_ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        return conn

    def _init_db(self):
        with self.lock, self._connect() as conn:
            conn.executescript('''
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                payload_json TEXT NOT NULL, candidates_total INTEGER, checked INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0, current_phase TEXT NOT NULL,
                error TEXT, meta_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL,
                updated_at REAL NOT NULL, heartbeat_at REAL NOT NULL, resume_count INTEGER NOT NULL DEFAULT 0,
                owner_id TEXT, snapshot_complete INTEGER NOT NULL DEFAULT 0,
                progress_seq INTEGER NOT NULL DEFAULT 0, progress_changed_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS job_candidates (
                job_id TEXT NOT NULL, candidate_key TEXT NOT NULL, ordinal INTEGER NOT NULL,
                payload_json TEXT NOT NULL, PRIMARY KEY (job_id, candidate_key)
            );
            CREATE TABLE IF NOT EXISTS job_results (
                job_id TEXT NOT NULL, candidate_key TEXT NOT NULL, ordinal INTEGER NOT NULL,
                outcome TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY (job_id, candidate_key)
            );
            CREATE INDEX IF NOT EXISTS idx_job_results_page ON job_results(job_id, outcome, ordinal);
            CREATE TABLE IF NOT EXISTS runner_instances (
                owner_id TEXT PRIMARY KEY, heartbeat_at REAL NOT NULL
            );
            ''')
            columns = {row[1] for row in conn.execute('PRAGMA table_info(jobs)')}
            if 'owner_id' not in columns:
                conn.execute('ALTER TABLE jobs ADD COLUMN owner_id TEXT')
            if 'snapshot_complete' not in columns:
                conn.execute('ALTER TABLE jobs ADD COLUMN snapshot_complete INTEGER NOT NULL DEFAULT 0')
                conn.execute('''UPDATE jobs SET snapshot_complete=1
                             WHERE candidates_total IS NOT NULL''')
            if 'progress_seq' not in columns:
                conn.execute('ALTER TABLE jobs ADD COLUMN progress_seq INTEGER NOT NULL DEFAULT 0')
            if 'progress_changed_at' not in columns:
                conn.execute('ALTER TABLE jobs ADD COLUMN progress_changed_at REAL NOT NULL DEFAULT 0')
                conn.execute('UPDATE jobs SET progress_changed_at=updated_at WHERE progress_changed_at=0')

    @staticmethod
    def _decode(row):
        if not row:
            return None
        result = dict(row)
        result['payload'] = json.loads(result.pop('payload_json'))
        result['meta'] = json.loads(result.pop('meta_json'))
        result['cancel'] = bool(result.pop('cancel_requested'))
        result['snapshot_complete'] = bool(result['snapshot_complete'])
        return result

    def create(self, job_id, kind, payload):
        now = time.time()
        with self.lock, self._connect() as conn:
            conn.execute('''INSERT INTO jobs
                (job_id,kind,status,payload_json,current_phase,created_at,updated_at,heartbeat_at,
                 progress_changed_at)
                VALUES (?,?, 'queued', ?, 'queued', ?, ?, ?, ?)''',
                         (job_id, kind, json.dumps(payload, ensure_ascii=False), now, now, now, now))

    def get(self, job_id):
        with self.lock, self._connect() as conn:
            return self._decode(conn.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone())

    def update(self, job_id, *, status=None, phase=None, error=None, meta=None, checked=None,
               owner_id=None, clear_owner=False):
        now = time.time()
        fields, values = [
            'updated_at=?', 'heartbeat_at=?', 'progress_seq=progress_seq+1',
            'progress_changed_at=?'], [now, now, now]
        for column, value in (('status', status), ('current_phase', phase), ('error', error), ('checked', checked)):
            if value is not None:
                fields.append(column + '=?')
                values.append(value)
        if meta is not None:
            fields.append('meta_json=?')
            values.append(json.dumps(meta, ensure_ascii=False))
        if owner_id is not None:
            fields.append('owner_id=?')
            values.append(owner_id)
        elif clear_owner:
            fields.append('owner_id=NULL')
        values.append(job_id)
        with self.lock, self._connect() as conn:
            conn.execute('UPDATE jobs SET ' + ', '.join(fields) + ' WHERE job_id=?', values)

    def merge_meta(self, job_id, updates, phase=None):
        """동시 진행 callback이 서로의 meta 필드를 덮어쓰지 않도록 한 transaction에서 병합한다."""
        now = time.time()
        with self.lock, self._connect() as conn:
            row = conn.execute(
                'SELECT meta_json,current_phase FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if not row:
                return False
            meta = json.loads(row['meta_json'])
            before = self._progress_signature(meta, row['current_phase'])
            meta.update(updates)
            after = self._progress_signature(meta, phase or row['current_phase'])
            bump = before != after
            progress_sql = (',progress_seq=progress_seq+1,progress_changed_at=?' if bump else '')
            progress_values = [now] if bump else []
            if phase is None:
                conn.execute(f'''UPDATE jobs SET meta_json=?,updated_at=?,heartbeat_at=?
                                 {progress_sql} WHERE job_id=?''',
                             (json.dumps(meta, ensure_ascii=False), now, now,
                              *progress_values, job_id))
            else:
                conn.execute(f'''UPDATE jobs SET meta_json=?,current_phase=?,updated_at=?,heartbeat_at=?
                                 {progress_sql} WHERE job_id=?''',
                             (json.dumps(meta, ensure_ascii=False), phase, now, now,
                              *progress_values, job_id))
            return True

    @staticmethod
    def _progress_signature(meta, phase):
        """heartbeat용 시각만 달라진 갱신은 사용자에게 보일 진행 변화로 세지 않는다."""
        work = dict(meta.get('work_progress') or {})
        for key in ('last_activity_at', 'progress_at', 'document_elapsed_ms'):
            work.pop(key, None)
        visible = {
            'phase': phase,
            'discovery_count': meta.get('discovery_count'),
            'work_progress': work,
            'stage_stats': meta.get('stage_stats'),
            'needs_refinement': meta.get('needs_refinement'),
            'heartbeat_diagnostics': meta.get('heartbeat_diagnostics'),
        }
        return json.dumps(visible, ensure_ascii=False, sort_keys=True, default=str)

    def touch_heartbeat(self, job_id, owner_id=None):
        """job의 heartbeat_at만 현재 시각으로 갱신한다(다른 필드는 건드리지 않는다).

        문서 다운로드/추출은 문서 하나가 수 분씩 걸릴 수 있는데, 그동안 heartbeat를 갱신할
        지점이 배치 경계밖에 없어 "완전히 멈춘 것"처럼 보였다. 워커가 job을 잡고 있는 동안
        이걸 주기적으로 호출해, 한 작업이 오래 걸려도 프로세스가 살아있음을 상태에 남긴다.
        실제 처리 진행량은 checked/discovery_count가 따로 보여준다."""
        now = time.time()
        with self.lock, self._connect() as conn:
            if owner_id is None:
                query, params = 'UPDATE jobs SET heartbeat_at=? WHERE job_id=?', (now, job_id)
            else:
                query = 'UPDATE jobs SET heartbeat_at=? WHERE job_id=? AND owner_id=?'
                params = (now, job_id, owner_id)
            return conn.execute(query, params).rowcount > 0

    def owner_is(self, job_id, owner_id):
        with self.lock, self._connect() as conn:
            row = conn.execute('SELECT owner_id FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            return bool(row and row[0] == owner_id)

    def request_cancel(self, job_id):
        now = time.time()
        with self.lock, self._connect() as conn:
            return conn.execute('''UPDATE jobs SET cancel_requested=1,
                status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,
                current_phase=CASE WHEN status='queued' THEN 'cancelled' ELSE current_phase END,
                owner_id=CASE WHEN status='queued' THEN NULL ELSE owner_id END,
                updated_at=?, heartbeat_at=?, progress_seq=progress_seq+1,
                progress_changed_at=? WHERE job_id=?''', (now, now, now, job_id)).rowcount > 0

    def cancel_requested(self, job_id):
        with self.lock, self._connect() as conn:
            row = conn.execute('SELECT cancel_requested FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            return bool(row and row[0])

    def save_candidates(self, job_id, candidates, meta=None):
        rows = [(job_id, candidate_key(c), i, json.dumps(c, ensure_ascii=False)) for i, c in enumerate(candidates)]
        with self.lock, self._connect() as conn:
            conn.executemany('INSERT OR IGNORE INTO job_candidates VALUES (?,?,?,?)', rows)
            now = time.time()
            if meta is None:
                conn.execute('''UPDATE jobs SET candidates_total=?, snapshot_complete=1,
                             updated_at=?, heartbeat_at=?, progress_seq=progress_seq+1,
                             progress_changed_at=? WHERE job_id=?''',
                             (len(candidates), now, now, now, job_id))
            else:
                conn.execute('''UPDATE jobs SET candidates_total=?, snapshot_complete=1,
                             meta_json=?, updated_at=?, heartbeat_at=?, progress_seq=progress_seq+1,
                             progress_changed_at=? WHERE job_id=?''',
                             (len(candidates), json.dumps(meta, ensure_ascii=False), now, now, now, job_id))

    def candidates(self, job_id, only_pending=False):
        query = 'SELECT c.candidate_key,c.ordinal,c.payload_json FROM job_candidates c'
        if only_pending:
            query += ' LEFT JOIN job_results r ON r.job_id=c.job_id AND r.candidate_key=c.candidate_key WHERE c.job_id=? AND r.candidate_key IS NULL'
        else:
            query += ' WHERE c.job_id=?'
        query += ' ORDER BY c.ordinal'
        with self.lock, self._connect() as conn:
            return [(r['candidate_key'], r['ordinal'], json.loads(r['payload_json'])) for r in conn.execute(query, (job_id,))]

    def has_candidate_snapshot(self, job_id):
        with self.lock, self._connect() as conn:
            row = conn.execute('SELECT snapshot_complete FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            return bool(row and row[0])

    def save_outcomes(self, job_id, outcomes, meta=None):
        now = time.time()
        rows = [(job_id, key, ordinal, outcome, json.dumps(payload, ensure_ascii=False), now)
                for key, ordinal, outcome, payload in outcomes]
        with self.lock, self._connect() as conn:
            conn.executemany('INSERT OR REPLACE INTO job_results VALUES (?,?,?,?,?,?)', rows)
            checked = conn.execute('SELECT COUNT(*) FROM job_results WHERE job_id=?', (job_id,)).fetchone()[0]
            if meta is None:
                conn.execute('''UPDATE jobs SET checked=?, updated_at=?, heartbeat_at=?,
                             progress_seq=progress_seq+1, progress_changed_at=? WHERE job_id=?''',
                             (checked, now, now, now, job_id))
            else:
                conn.execute('''UPDATE jobs SET checked=?, meta_json=?, updated_at=?, heartbeat_at=?,
                             progress_seq=progress_seq+1, progress_changed_at=? WHERE job_id=?''',
                             (checked, json.dumps(meta, ensure_ascii=False), now, now, now, job_id))

    def result_counts(self, job_id):
        with self.lock, self._connect() as conn:
            return dict(conn.execute('SELECT outcome, COUNT(*) FROM job_results WHERE job_id=? GROUP BY outcome', (job_id,)).fetchall())

    def results(self, job_id, outcome, offset, limit):
        with self.lock, self._connect() as conn:
            rows = conn.execute('''SELECT payload_json FROM job_results
                WHERE job_id=? AND outcome=? ORDER BY ordinal LIMIT ? OFFSET ?''',
                                (job_id, outcome, limit, offset)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def results_for_outcomes(self, job_id, outcomes, offset, limit):
        placeholders = ','.join('?' for _ in outcomes)
        query = f'''SELECT payload_json FROM job_results WHERE job_id=?
                    AND outcome IN ({placeholders}) ORDER BY ordinal LIMIT ? OFFSET ?'''
        params = [job_id, *outcomes, limit, offset]
        with self.lock, self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [json.loads(row[0]) for row in rows]

    def register_runner(self, owner_id):
        now = time.time()
        with self.lock, self._connect() as conn:
            conn.execute('INSERT OR REPLACE INTO runner_instances VALUES (?,?)', (owner_id, now))

    def heartbeat_runner(self, owner_id):
        now = time.time()
        with self.lock, self._connect() as conn:
            conn.execute('INSERT OR REPLACE INTO runner_instances VALUES (?,?)', (owner_id, now))

    def claim(self, job_id, owner_id):
        now = time.time()
        with self.lock, self._connect() as conn:
            return conn.execute('''UPDATE jobs SET status='running', current_phase='starting',
                owner_id=?, updated_at=?, heartbeat_at=?, progress_seq=progress_seq+1,
                progress_changed_at=?
                WHERE job_id=? AND status='queued' AND cancel_requested=0''',
                                (owner_id, now, now, now, job_id)).rowcount > 0

    def recover_interrupted(self, owner_id=None):
        owner_id = owner_id or f'manual-{os.getpid()}-{id(self)}'
        now = time.time()
        cutoff = now - JOB_LEASE_TIMEOUT_SECONDS
        with self.lock, self._connect() as conn:
            conn.execute('INSERT OR REPLACE INTO runner_instances VALUES (?,?)', (owner_id, now))
            conn.execute("""UPDATE jobs SET status='cancelled', current_phase='cancelled', owner_id=NULL,
                         updated_at=?, heartbeat_at=?, progress_seq=progress_seq+1,
                         progress_changed_at=? WHERE cancel_requested=1
                         AND status IN ('queued','running','discovering')""", (now, now, now))
            conn.execute("""UPDATE jobs SET status='queued', current_phase='recovery_pending',
                         owner_id=NULL, resume_count=resume_count+1, updated_at=?, heartbeat_at=?,
                         progress_seq=progress_seq+1, progress_changed_at=?
                         WHERE status IN ('running','discovering') AND cancel_requested=0
                         AND (heartbeat_at<? OR owner_id IS NULL OR NOT EXISTS (
                             SELECT 1 FROM runner_instances r
                             WHERE r.owner_id=jobs.owner_id AND r.heartbeat_at>=?
                         ))""", (now, now, now, cutoff, cutoff))
            conn.execute('DELETE FROM runner_instances WHERE heartbeat_at<? AND owner_id<>?',
                         (cutoff, owner_id))
            return [r[0] for r in conn.execute("SELECT job_id FROM jobs WHERE status='queued' AND cancel_requested=0")]

    def cleanup(self):
        cutoff = time.time() - JOB_RETENTION_SECONDS
        with self.lock, self._connect() as conn:
            ids = [r[0] for r in conn.execute("SELECT job_id FROM jobs WHERE status IN ('done','cancelled','error') AND updated_at<?", (cutoff,))]
            ids += [r[0] for r in conn.execute("SELECT job_id FROM jobs WHERE status IN ('done','cancelled','error') ORDER BY updated_at DESC LIMIT -1 OFFSET ?", (MAX_RETAINED_JOBS,))]
            for job_id in set(ids):
                conn.execute('DELETE FROM job_results WHERE job_id=?', (job_id,))
                conn.execute('DELETE FROM job_candidates WHERE job_id=?', (job_id,))
                conn.execute('DELETE FROM jobs WHERE job_id=?', (job_id,))


class SharedSlotLimiter:
    """SQLite lease로 같은 PC의 여러 MCP 프로세스가 공유하는 bounded semaphore."""

    def __init__(self, name, capacity, lease_seconds, path=None):
        self.name = name
        self.capacity = max(1, int(capacity))
        self.lease_seconds = max(30, int(lease_seconds))
        self.path = path or shared_limit_db_path()
        try:
            self._init_db()
        except (OSError, sqlite3.OperationalError):
            if path:
                raise
            self.path = os.path.join(tempfile.gettempdir(), 'DOHWA EIASS Agent', 'mcp_limits.sqlite3')
            self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with self._connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS shared_slots (
                slot_name TEXT NOT NULL, token TEXT PRIMARY KEY, expires_at REAL NOT NULL,
                owner_pid INTEGER NOT NULL DEFAULT 0,
                owner_instance TEXT NOT NULL DEFAULT '')''')
            columns = {row[1] for row in conn.execute('PRAGMA table_info(shared_slots)')}
            if 'owner_pid' not in columns:
                conn.execute(
                    'ALTER TABLE shared_slots ADD COLUMN owner_pid INTEGER NOT NULL DEFAULT 0')
            if 'owner_instance' not in columns:
                conn.execute(
                    "ALTER TABLE shared_slots ADD COLUMN owner_instance TEXT NOT NULL DEFAULT ''")
            conn.execute('CREATE INDEX IF NOT EXISTS idx_shared_slots_name ON shared_slots(slot_name,expires_at)')

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5, factory=_ClosingConnection)

    @staticmethod
    def _pid_is_alive(pid, owner_instance=''):
        if pid == os.getpid():
            return owner_instance == _PROCESS_INSTANCE_ID
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False

    def acquire(self, should_cancel=None, deadline=None, on_wait=None):
        token = f'{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex}'
        while True:
            if should_cancel and should_cancel():
                return None
            now = time.time()
            if deadline is not None and time.monotonic() >= deadline:
                return None
            try:
                with self._connect() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    conn.execute('DELETE FROM shared_slots WHERE expires_at<?', (now,))
                    owners = list(conn.execute(
                        'SELECT DISTINCT owner_pid,owner_instance FROM shared_slots'))
                    for owner_pid, owner_instance in owners:
                        if not self._pid_is_alive(owner_pid, owner_instance):
                            conn.execute('''DELETE FROM shared_slots
                                         WHERE owner_pid=? AND owner_instance=?''',
                                         (owner_pid, owner_instance))
                    used = conn.execute(
                        'SELECT COUNT(*) FROM shared_slots WHERE slot_name=?', (self.name,)).fetchone()[0]
                    if used < self.capacity:
                        conn.execute('''INSERT INTO shared_slots
                                     (slot_name,token,expires_at,owner_pid,owner_instance)
                                     VALUES (?,?,?,?,?)''',
                                     (self.name, token, now + self.lease_seconds, os.getpid(),
                                      _PROCESS_INSTANCE_ID))
                        return token
            except sqlite3.OperationalError:
                pass
            if on_wait:
                on_wait()
            time.sleep(0.2)

    def release(self, token):
        if not token:
            return
        try:
            with self._connect() as conn:
                conn.execute('DELETE FROM shared_slots WHERE token=?', (token,))
        except sqlite3.OperationalError:
            pass
