"""영속 JobStore를 사용하는 bounded worker 기반 스캔 실행기."""
import base64
import json
import queue
import sys
import threading
import time
import uuid

import eiass_core as core
from config import (JOB_HEARTBEAT_INTERVAL_SECONDS, JOB_QUEUE_SIZE, JOB_WORKER_COUNT,
                    SCAN_MONITOR_POLL_SECONDS, SCAN_NORMAL_REPORT_SECONDS,
                    SCAN_UNCHANGED_REPORT_SECONDS)
from document_engine import outcomes as document_outcomes, run_batch as run_document_batch
from job_store import JobStore
from spatial_engine import outcomes as spatial_outcomes, run_batch as run_spatial_batch


_HEARTBEAT_DIAGNOSTICS = {}
_HEARTBEAT_DIAGNOSTICS_LOCK = threading.Lock()


class ScanRunner:
    def __init__(self, store=None, worker_count=JOB_WORKER_COUNT, queue_size=JOB_QUEUE_SIZE):
        self.store = store or JobStore()
        self.owner_id = uuid.uuid4().hex
        self.queue = queue.Queue(maxsize=queue_size)
        self._queued = set()
        self._lock = threading.Lock()
        self._workers = []
        for index in range(worker_count):
            worker = threading.Thread(target=self._worker, name=f'eiass-scan-{index + 1}', daemon=True)
            worker.start()
            self._workers.append(worker)
        self.store.register_runner(self.owner_id)
        self._maintenance_once()
        self._maintenance_thread = threading.Thread(
            target=self._maintenance, name='eiass-scan-maintenance', daemon=True)
        self._maintenance_thread.start()
        self.store.cleanup()

    def _maintenance_once(self):
        self.store.heartbeat_runner(self.owner_id)
        for job_id in self.store.recover_interrupted(self.owner_id):
            if not self.submit(job_id):
                break

    def _maintenance(self):
        while True:
            threading.Event().wait(JOB_HEARTBEAT_INTERVAL_SECONDS)
            try:
                self._maintenance_once()
            except Exception:
                # 일시적인 SQLite 잠금은 다음 heartbeat에서 다시 시도한다.
                continue

    def submit(self, job_id):
        with self._lock:
            if job_id in self._queued:
                return True
            try:
                self.queue.put_nowait(job_id)
            except queue.Full:
                return False
            self._queued.add(job_id)
            return True

    def _worker(self):
        while True:
            job_id = self.queue.get()
            try:
                try:
                    self._run(job_id)
                except Exception as exc:
                    # 오류 상태를 기록하는 SQLite 호출 자체가 실패해도 워커 스레드는 죽지 않는다.
                    # job heartbeat가 stale해지면 maintenance가 lease를 회수해 다시 실행한다.
                    try:
                        self.store.update(job_id, status='error', phase='worker_failure',
                                          error=f'스캔 워커 내부 오류: {exc}', clear_owner=True)
                    except Exception:
                        pass
            finally:
                with self._lock:
                    self._queued.discard(job_id)
                self.queue.task_done()

    def _run(self, job_id):
        if not self.store.claim(job_id, self.owner_id):
            return
        job = self.store.get(job_id)
        if not job or job['cancel']:
            if job:
                self.store.update(job_id, status='cancelled', phase='cancelled', clear_owner=True)
            return
        payload = job['payload']
        kind = job['kind']
        session = core._session()
        # job heartbeat 펌프: 워커가 이 job을 처리하는 동안 5초마다 heartbeat를 갱신한다.
        # 문서 다운로드/추출은 문서 하나가 수 분씩 걸릴 수 있어 배치 경계에서만 heartbeat를
        # 갱신하면 그동안 "멈춘 것"처럼 보였다(사용자가 실제로 겪어 스캔을 포기함). 이 펌프는
        # 한 작업이 오래 걸려도 프로세스가 살아있음을 상태에 남긴다. 진행량은 checked/
        # discovery_count가 별도로 보여주므로, "살아있지만 진행 없음"도 구분된다.
        stop_heartbeat = threading.Event()

        def _heartbeat_pump():
            failures = 0
            total_failures = 0
            last_error = None
            while not stop_heartbeat.wait(JOB_HEARTBEAT_INTERVAL_SECONDS):
                try:
                    if not self.store.touch_heartbeat(job_id, owner_id=self.owner_id):
                        return
                    if failures:
                        diagnostic = {
                            'consecutive_failures': 0, 'total_failures': total_failures,
                            'last_error': last_error, 'recovered_at': time.time(),
                        }
                        with _HEARTBEAT_DIAGNOSTICS_LOCK:
                            _HEARTBEAT_DIAGNOSTICS[job_id] = diagnostic
                        self.store.merge_meta(job_id, {'heartbeat_diagnostics': diagnostic})
                    failures = 0
                except Exception as exc:
                    failures += 1
                    total_failures += 1
                    last_error = f'{type(exc).__name__}: {exc}'
                    diagnostic = {
                        'consecutive_failures': failures, 'total_failures': total_failures,
                        'last_error': last_error, 'last_failure_at': time.time(),
                    }
                    with _HEARTBEAT_DIAGNOSTICS_LOCK:
                        _HEARTBEAT_DIAGNOSTICS[job_id] = diagnostic
                    print(f'[EIASS heartbeat] job={job_id} failure={failures}: {last_error}',
                          file=sys.stderr, flush=True)
        pump = threading.Thread(target=_heartbeat_pump, name=f'eiass-hb-{job_id[:8]}', daemon=True)
        pump.start()
        try:
            if not job['snapshot_complete']:
                self.store.update(job_id, status='discovering', phase='candidate_snapshot')
                # 후보 수집은 수천 건이면 수십 개의 순차 요청이라 오래 걸린다. 페이지마다 heartbeat를
                # 갱신해 "살아있음 + 지금까지 N건 수집"을 상태에 남기고(안 그러면 checked:0으로 멈춘
                # 것처럼 보인다), 취소 요청도 페이지 사이에 반영되게 한다.
                disco_meta = dict(job['meta'])

                def _on_discovery_progress(found):
                    disco_meta['discovery_count'] = found
                    self.store.update(job_id, phase='candidate_snapshot', meta=disco_meta)

                date_filter_exclusions = []
                candidates = core.search_projects(
                    payload.get('keyword', ''), type_codes=payload.get('type_codes'),
                    agency_code=payload.get('agency_code', ''), max_pages=payload.get('max_pages', 0), session=session,
                    consult_date_from=payload.get('consult_date_from'), consult_date_to=payload.get('consult_date_to'),
                    progress_status=payload.get('progress_status', ''),
                    climate_filter=payload.get('climate_filter', ''), biz_gubun=payload.get('biz_gubun', ''),
                    progress_stage_keys=payload.get('progress_stage_keys'),
                    date_filter_exclusions=date_filter_exclusions,
                    should_cancel=lambda: self.store.cancel_requested(job_id),
                    on_progress=_on_discovery_progress)
                disco_meta['date_filter_exclusions'] = date_filter_exclusions
                self.store.save_candidates(job_id, candidates, meta=disco_meta)
            self.store.update(job_id, status='running', phase='document_scan' if kind == 'document' else 'spatial_scan')
            pending = self.store.candidates(job_id, only_pending=True)
            batch_size = int(payload['batch_size'])
            progress_lock = threading.Lock()
            file_bytes = {}
            completed_files = set()
            for start in range(0, len(pending), batch_size):
                if not self.store.owner_is(job_id, self.owner_id):
                    return  # 다른 runner가 stale lease를 회수했으므로 이전 실행 결과를 쓰지 않는다.
                if self.store.cancel_requested(job_id):
                    raise core.ScanCancelled('사용자 요청으로 취소했습니다.')
                batch_meta = pending[start:start + batch_size]
                candidates = [item[2] for item in batch_meta]
                ordinal_by_key = {key: ordinal for key, ordinal, _ in batch_meta}
                should_cancel = lambda: self.store.cancel_requested(job_id)
                if kind == 'document':
                    def _on_work_progress(progress):
                        if not self.store.owner_is(job_id, self.owner_id):
                            raise core.ScanCancelled('작업 lease가 다른 runner로 이전되었습니다.')
                        with progress_lock:
                            file_seq = progress.get('file_seq')
                            if file_seq:
                                file_bytes[file_seq] = max(
                                    file_bytes.get(file_seq, 0), int(progress.get('bytes_received') or 0))
                                if progress.get('phase') == 'prefetch_documents':
                                    completed_files.add(file_seq)
                            work_progress = dict(
                                progress, progress_at=time.time(),
                                documents_completed=len(completed_files),
                                bytes_received_total=sum(file_bytes.values()),
                                candidates_completed=self.store.get(job_id)['checked'])
                        self.store.merge_meta(
                            job_id, {'work_progress': work_progress},
                            phase=progress.get('phase', 'document_scan'))

                    def _checkpoint_candidate(candidate, candidate_result):
                        normalized_one = document_outcomes(
                            [candidate], candidate_result, ordinal_by_key)
                        self.store.save_outcomes(job_id, normalized_one)
                        current = self.store.get(job_id)['meta']
                        updates = {
                            'stage_stats': _merge_stage_stats(
                                current.get('stage_stats', {}),
                                candidate_result.get('stage_stats', {})),
                        }
                        if candidate_result.get('needs_refinement'):
                            updates['needs_refinement'] = True
                            hints = list(current.get('refinement_hints', []))
                            hint = candidate_result.get('refinement_hint')
                            if hint:
                                hints.append(hint)
                            updates['refinement_hints'] = hints
                        if candidate_result.get('audit_sample'):
                            samples = list(current.get('audit_samples', []))
                            samples.append(candidate_result['audit_sample'])
                            updates['audit_samples'] = samples
                        self.store.merge_meta(job_id, updates)

                    batch_payload = dict(payload, _on_progress=_on_work_progress)
                    try:
                        result = run_document_batch(
                            batch_payload, candidates, should_cancel, session,
                            on_candidate_complete=_checkpoint_candidate)
                        normalized = None
                        meta = None
                    except TypeError as exc:
                        # 기존 플러그인/테스트 대체 구현은 새 callback 인자를 모를 수 있다.
                        if 'on_candidate_complete' not in str(exc):
                            raise
                        result = run_document_batch(
                            batch_payload, candidates, should_cancel, session)
                        normalized = document_outcomes(candidates, result, ordinal_by_key)
                        meta = self.store.get(job_id)['meta']
                        meta['stage_stats'] = _merge_stage_stats(
                            meta.get('stage_stats', {}), result.get('stage_stats', {}))
                else:
                    result = run_spatial_batch(payload, candidates, should_cancel, session)
                    normalized = spatial_outcomes(candidates, result, ordinal_by_key)
                    meta = self.store.get(job_id)['meta']
                if not self.store.owner_is(job_id, self.owner_id):
                    return
                if normalized is not None:
                    self.store.save_outcomes(job_id, normalized, meta=meta)
            if self.store.owner_is(job_id, self.owner_id):
                self.store.update(job_id, status='done', phase='completed', clear_owner=True)
        except core.ScanCancelled:
            if self.store.owner_is(job_id, self.owner_id):
                self.store.update(job_id, status='cancelled', phase='cancelled', clear_owner=True)
        except Exception as exc:
            # 개별 문서/사업 실패는 안쪽 루프가 이미 잡아서 스캔을 계속 진행시킨다. 여기까지 온 건
            # 후보 검색 같은 스캔 전체를 세우는 오류이므로, 외부 서비스 탓이면 어느 서버인지 남긴다.
            if self.store.owner_is(job_id, self.owner_id):
                self.store.update(job_id, status='error', phase='failed',
                                  error=_describe_job_failure(exc, kind), clear_owner=True)
        finally:
            stop_heartbeat.set()
            session.close()


def _describe_job_failure(exc, kind):
    """스캔 job이 실패한 이유를 사용자가 읽을 문장으로 만든다.

    장시간 도는 작업이라 "왜 멈췄는지"가 특히 중요하다. 외부 서비스 문제가 아니면 원래 메시지를
    그대로 둔다(입력 오류에까지 헬스체크를 돌리지 않는다).
    """
    if not core.is_network_error(exc):
        return str(exc)
    services = (('eiass_site', 'eiass_search_api') if kind == 'document'
                else ('eiass_site', 'eiass_search_api', 'vworld', 'kdpa'))
    return core.explain_failure(exc, services).get('error', str(exc))


def _merge_stage_stats(total, batch):
    for stage, stats in batch.items():
        current = total.setdefault(stage, {'checked': 0, 'matched': 0})
        current['checked'] += stats.get('checked', 0)
        current['matched'] += stats.get('matched', 0)
    return total


_IMMEDIATE_ACTIVITY_STATES = {
    'active_slow', 'server_slow', 'local_resource_pressure', 'timed_out', 'stalled',
}
_TERMINAL_JOB_STATES = {'done', 'cancelled', 'error'}


def _progress_message(status, unchanged=False):
    job_status = status['status']
    checked = status.get('checked') or 0
    total = status.get('candidates_total')
    percent = status.get('progress_percent')
    work = status.get('work_progress') or {}
    state = status.get('activity_state')
    if job_status == 'done':
        heading = f"스캔이 완료되었습니다 — 후보 {checked}/{total or checked}건을 확인했습니다."
    elif job_status == 'cancelled':
        heading = f"스캔이 취소되었습니다 — 후보 {checked}/{total or '?'}건까지 확인했습니다."
    elif job_status == 'error':
        heading = f"스캔이 오류로 중단되었습니다 — {status.get('error') or '원인 미상'}"
    elif job_status == 'discovering':
        heading = f"스캔 후보 목록을 수집 중입니다 — 현재 {status.get('discovery_count') or 0}건."
    else:
        ratio = f" ({percent:.1f}%)" if percent is not None else ''
        heading = f"스캔 진행 중 — 후보 {checked}/{total or '?'}건{ratio}을 확인했습니다."
    if unchanged and job_status not in _TERMINAL_JOB_STATES:
        heading = '처리 건수에는 변화가 없지만 작업은 계속 실행 중입니다. ' + heading
    lines = [heading]
    if job_status not in ('queued', 'discovering'):
        lines.append(
            f"결과: 일치 {status.get('match_count', 0)}건 · 매칭 없음 "
            f"{status.get('checked_no_match_count', 0)}건 · 제외/실패 {status.get('skipped_count', 0)}건")
    current = ' / '.join(filter(None, (work.get('current_candidate'), work.get('current_file'))))
    if current:
        lines.append(f"현재: {current} ({work.get('phase') or status.get('current_phase')})")
    if state:
        lines.append(
            f"상태: {state} · 마지막 작업 활동 {status.get('seconds_since_activity', 0):.0f}초 전 · "
            f"heartbeat {status.get('seconds_since_heartbeat', 0):.0f}초 전")
    return '\n'.join(lines)


def make_monitor_cursor(status, reported_at=None):
    payload = {
        'seq': int(status.get('progress_seq') or 0),
        'reported_at': float(reported_at or time.time()),
        'activity_state': status.get('activity_state') or '',
    }
    raw = json.dumps(payload, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _parse_monitor_cursor(cursor, status):
    if not cursor:
        return {'seq': int(status.get('progress_seq') or 0),
                'reported_at': time.time(),
                'activity_state': status.get('activity_state') or ''}
    try:
        raw = base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4))
        payload = json.loads(raw.decode('ascii'))
        return {'seq': int(payload['seq']), 'reported_at': float(payload['reported_at']),
                'activity_state': str(payload.get('activity_state') or '')}
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise ValueError('유효하지 않은 monitor_cursor입니다.')


def wait_document_update(store, job_id, monitor_cursor='', timeout_seconds=SCAN_MONITOR_POLL_SECONDS):
    """사용자 메시지 주기와 내부 상태 확인을 분리하는 bounded long-poll."""
    timeout_seconds = max(5, min(60, int(timeout_seconds)))
    initial = document_status(store, job_id)
    if initial.get('error'):
        return initial
    baseline = _parse_monitor_cursor(monitor_cursor, initial)
    deadline = time.monotonic() + timeout_seconds
    while True:
        status = document_status(store, job_id)
        if status.get('error'):
            return status
        now = time.time()
        state = status.get('activity_state') or ''
        seq = int(status.get('progress_seq') or 0)
        changed = seq > baseline['seq']
        state_changed = state != baseline['activity_state']
        report_age = max(0.0, now - baseline['reported_at'])
        terminal = status['status'] in _TERMINAL_JOB_STATES
        immediate = state_changed and (
            state in _IMMEDIATE_ACTIVITY_STATES or
            baseline['activity_state'] in _IMMEDIATE_ACTIVITY_STATES)
        reason = None
        unchanged = False
        if terminal:
            reason = 'terminal'
        elif immediate:
            reason = 'state_transition'
        elif changed and report_age >= SCAN_NORMAL_REPORT_SECONDS:
            reason = 'normal_interval'
        elif not changed and report_age >= SCAN_UNCHANGED_REPORT_SECONDS:
            reason = 'unchanged_keepalive'
            unchanged = True
        if reason:
            status['progress_message'] = _progress_message(status, unchanged=unchanged)
            return {
                'should_notify': True, 'reason': reason,
                'monitor_cursor': make_monitor_cursor(status, now),
                'next_poll_seconds': SCAN_MONITOR_POLL_SECONDS,
                'progress_message': status['progress_message'], 'scan_status': status,
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                'should_notify': False, 'reason': 'poll_timeout',
                'monitor_cursor': monitor_cursor or make_monitor_cursor(initial, baseline['reported_at']),
                'next_poll_seconds': SCAN_MONITOR_POLL_SECONDS,
                'progress_message': None, 'scan_status': status,
            }
        time.sleep(min(1.0, remaining))


def document_status(store, job_id, include_results=False, offset=0, limit=100):
    job = store.get(job_id)
    if not job or job['kind'] != 'document':
        return {'error': f'알 수 없는 job_id: {job_id}'}
    counts = store.result_counts(job_id)
    now = time.time()
    work_progress = job['meta'].get('work_progress') or {}
    heartbeat_diagnostics = job['meta'].get('heartbeat_diagnostics') or {}
    with _HEARTBEAT_DIAGNOSTICS_LOCK:
        heartbeat_diagnostics = dict(
            heartbeat_diagnostics, **_HEARTBEAT_DIAGNOSTICS.get(job_id, {}))
    heartbeat_age = max(0.0, now - job['heartbeat_at'])
    activity_at = work_progress.get('last_activity_at') or work_progress.get('progress_at') or job['updated_at']
    activity_age = max(0.0, now - activity_at)
    if job['status'] in ('running', 'discovering'):
        activity_state = work_progress.get('activity_state') or 'running'
        if (heartbeat_diagnostics.get('consecutive_failures', 0) > 0 or
                activity_state == 'local_resource_pressure'):
            activity_state = 'local_resource_pressure'
        if heartbeat_age > JOB_HEARTBEAT_INTERVAL_SECONDS * 3 and activity_age > 60:
            activity_state = 'stalled'
    elif job['status'] == 'done':
        activity_state = 'completed'
    else:
        activity_state = job['status']

    progress_percent = (round(job['checked'] * 100 / job['candidates_total'], 1)
                        if job['candidates_total'] else None)
    result = {'status': job['status'], 'activity_state': activity_state,
              'checked': job['checked'], 'candidates_total': job['candidates_total'],
              'progress_percent': progress_percent,
              'progress_seq': job.get('progress_seq', 0),
              'progress_changed_at': job.get('progress_changed_at'),
              'match_count': counts.get('match', 0), 'checked_no_match_count': counts.get('no_match', 0),
              'skipped_count': counts.get('skipped', 0), 'stage_stats': job['meta'].get('stage_stats', {}),
              'needs_refinement': bool(job['meta'].get('needs_refinement')),
              'refinement_hints': list(dict.fromkeys(filter(None, job['meta'].get('refinement_hints', [])))),
              'audit_samples': job['meta'].get('audit_samples', []),
              'date_filter_exclusions': job['meta'].get('date_filter_exclusions', []),
              'discovery_count': job['meta'].get('discovery_count'),
              'work_progress': work_progress or None,
              'heartbeat_diagnostics': heartbeat_diagnostics or None,
              'seconds_since_heartbeat': round(heartbeat_age, 1),
              'seconds_since_activity': round(activity_age, 1),
              'error': job['error'], 'current_phase': job['current_phase'],
              'updated_at': job['updated_at'], 'heartbeat_at': job['heartbeat_at'], 'resume_count': job['resume_count']}
    result['notification_policy'] = {
        'internal_poll_seconds': SCAN_MONITOR_POLL_SECONDS,
        'normal_report_seconds': SCAN_NORMAL_REPORT_SECONDS,
        'unchanged_keepalive_seconds': SCAN_UNCHANGED_REPORT_SECONDS,
        'immediate_on_state_change_or_terminal': True,
    }
    result['progress_message'] = _progress_message(result)
    if include_results:
        if offset < 0 or limit <= 0:
            return {'error': 'result_offset은 0 이상, result_limit은 1 이상이어야 합니다.'}
        result.update(matches=store.results(job_id, 'match', offset, limit),
                      skipped=store.results(job_id, 'skipped', offset, limit),
                      checked_no_match=store.results(job_id, 'no_match', offset, limit),
                      result_offset=offset, result_limit=limit)
    return result


def spatial_status(store, job_id, include_results=False, offset=0, limit=100):
    job = store.get(job_id)
    if not job or job['kind'] != 'spatial':
        return {'error': f'알 수 없는 job_id: {job_id}'}
    counts = store.result_counts(job_id)
    result = {'status': job['status'], 'checked': job['checked'], 'candidates_total': job['candidates_total'],
              'scanned_count': counts.get('match', 0) + counts.get('no_match', 0) + counts.get('spatial_failure', 0),
              'match_count': counts.get('match', 0), 'geocode_failure_count': counts.get('geocode_failure', 0),
              'spatial_failure_count': counts.get('spatial_failure', 0), 'error': job['error'],
              'date_filter_exclusions': job['meta'].get('date_filter_exclusions', []),
              'discovery_count': job['meta'].get('discovery_count'),
              'current_phase': job['current_phase'], 'updated_at': job['updated_at'],
              'heartbeat_at': job['heartbeat_at'], 'resume_count': job['resume_count']}
    if include_results:
        if offset < 0 or limit <= 0:
            return {'error': 'result_offset은 0 이상, result_limit은 1 이상이어야 합니다.'}
        result.update(scanned=store.results_for_outcomes(
                          job_id, ('match', 'no_match', 'spatial_failure'), offset, limit),
                      matches=store.results(job_id, 'match', offset, limit),
                      geocode_failures=store.results(job_id, 'geocode_failure', offset, limit),
                      spatial_failures=store.results(job_id, 'spatial_failure', offset, limit),
                      result_offset=offset, result_limit=limit)
    return result
