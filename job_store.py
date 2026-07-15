"""재시작 가능한 MCP 스캔 작업 저장소.

후보와 결과를 별도 행으로 저장해 긴 스캔의 결과 본문이 서버 메모리에 누적되지 않게 한다.
"""
import json
import os
import sqlite3
import threading
import time
import tempfile

from config import JOB_RETENTION_SECONDS, MAX_RETAINED_JOBS, job_db_path
from models import candidate_key


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
                updated_at REAL NOT NULL, heartbeat_at REAL NOT NULL, resume_count INTEGER NOT NULL DEFAULT 0
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
            ''')

    @staticmethod
    def _decode(row):
        if not row:
            return None
        result = dict(row)
        result['payload'] = json.loads(result.pop('payload_json'))
        result['meta'] = json.loads(result.pop('meta_json'))
        result['cancel'] = bool(result.pop('cancel_requested'))
        return result

    def create(self, job_id, kind, payload):
        now = time.time()
        with self.lock, self._connect() as conn:
            conn.execute('''INSERT INTO jobs
                (job_id,kind,status,payload_json,current_phase,created_at,updated_at,heartbeat_at)
                VALUES (?,?, 'queued', ?, 'queued', ?, ?, ?)''',
                         (job_id, kind, json.dumps(payload, ensure_ascii=False), now, now, now))

    def get(self, job_id):
        with self.lock, self._connect() as conn:
            return self._decode(conn.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone())

    def update(self, job_id, *, status=None, phase=None, error=None, meta=None, checked=None):
        now = time.time()
        fields, values = ['updated_at=?', 'heartbeat_at=?'], [now, now]
        for column, value in (('status', status), ('current_phase', phase), ('error', error), ('checked', checked)):
            if value is not None:
                fields.append(column + '=?')
                values.append(value)
        if meta is not None:
            fields.append('meta_json=?')
            values.append(json.dumps(meta, ensure_ascii=False))
        values.append(job_id)
        with self.lock, self._connect() as conn:
            conn.execute('UPDATE jobs SET ' + ', '.join(fields) + ' WHERE job_id=?', values)

    def request_cancel(self, job_id):
        now = time.time()
        with self.lock, self._connect() as conn:
            return conn.execute('UPDATE jobs SET cancel_requested=1, updated_at=? WHERE job_id=?', (now, job_id)).rowcount > 0

    def cancel_requested(self, job_id):
        with self.lock, self._connect() as conn:
            row = conn.execute('SELECT cancel_requested FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            return bool(row and row[0])

    def save_candidates(self, job_id, candidates):
        rows = [(job_id, candidate_key(c), i, json.dumps(c, ensure_ascii=False)) for i, c in enumerate(candidates)]
        with self.lock, self._connect() as conn:
            conn.executemany('INSERT OR IGNORE INTO job_candidates VALUES (?,?,?,?)', rows)
            conn.execute('UPDATE jobs SET candidates_total=?, updated_at=?, heartbeat_at=? WHERE job_id=?',
                         (len(candidates), time.time(), time.time(), job_id))

    def candidates(self, job_id, only_pending=False):
        query = 'SELECT c.candidate_key,c.ordinal,c.payload_json FROM job_candidates c'
        if only_pending:
            query += ' LEFT JOIN job_results r ON r.job_id=c.job_id AND r.candidate_key=c.candidate_key WHERE c.job_id=? AND r.candidate_key IS NULL'
        else:
            query += ' WHERE c.job_id=?'
        query += ' ORDER BY c.ordinal'
        with self.lock, self._connect() as conn:
            return [(r['candidate_key'], r['ordinal'], json.loads(r['payload_json'])) for r in conn.execute(query, (job_id,))]

    def has_candidates(self, job_id):
        with self.lock, self._connect() as conn:
            return conn.execute('SELECT 1 FROM job_candidates WHERE job_id=? LIMIT 1', (job_id,)).fetchone() is not None

    def save_outcomes(self, job_id, outcomes):
        now = time.time()
        rows = [(job_id, key, ordinal, outcome, json.dumps(payload, ensure_ascii=False), now)
                for key, ordinal, outcome, payload in outcomes]
        with self.lock, self._connect() as conn:
            conn.executemany('INSERT OR REPLACE INTO job_results VALUES (?,?,?,?,?,?)', rows)
            checked = conn.execute('SELECT COUNT(*) FROM job_results WHERE job_id=?', (job_id,)).fetchone()[0]
            conn.execute('UPDATE jobs SET checked=?, updated_at=?, heartbeat_at=? WHERE job_id=?',
                         (checked, now, now, job_id))

    def result_counts(self, job_id):
        with self.lock, self._connect() as conn:
            return dict(conn.execute('SELECT outcome, COUNT(*) FROM job_results WHERE job_id=? GROUP BY outcome', (job_id,)).fetchall())

    def results(self, job_id, outcome, offset, limit):
        with self.lock, self._connect() as conn:
            rows = conn.execute('''SELECT payload_json FROM job_results
                WHERE job_id=? AND outcome=? ORDER BY ordinal LIMIT ? OFFSET ?''',
                                (job_id, outcome, limit, offset)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def recover_interrupted(self):
        now = time.time()
        with self.lock, self._connect() as conn:
            conn.execute("""UPDATE jobs SET status='queued', current_phase='recovery_pending',
                         resume_count=resume_count+1, updated_at=?, heartbeat_at=?
                         WHERE status IN ('running','discovering')""", (now, now))
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
