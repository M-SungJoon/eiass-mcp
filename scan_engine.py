"""영속 JobStore를 사용하는 bounded worker 기반 스캔 실행기."""
import queue
import threading

import eiass_core as core
from config import JOB_QUEUE_SIZE, JOB_WORKER_COUNT
from document_engine import outcomes as document_outcomes, run_batch as run_document_batch
from job_store import JobStore
from spatial_engine import outcomes as spatial_outcomes, run_batch as run_spatial_batch


class ScanRunner:
    def __init__(self, store=None, worker_count=JOB_WORKER_COUNT, queue_size=JOB_QUEUE_SIZE):
        self.store = store or JobStore()
        self.queue = queue.Queue(maxsize=queue_size)
        self._queued = set()
        self._lock = threading.Lock()
        self._workers = []
        for index in range(worker_count):
            worker = threading.Thread(target=self._worker, name=f'eiass-scan-{index + 1}', daemon=True)
            worker.start()
            self._workers.append(worker)
        for job_id in self.store.recover_interrupted():
            self.submit(job_id)
        self.store.cleanup()

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
                self._run(job_id)
            finally:
                with self._lock:
                    self._queued.discard(job_id)
                self.queue.task_done()

    def _run(self, job_id):
        job = self.store.get(job_id)
        if not job or job['cancel']:
            if job:
                self.store.update(job_id, status='cancelled', phase='cancelled')
            return
        payload = job['payload']
        kind = job['kind']
        session = core._session()
        try:
            if not self.store.has_candidates(job_id):
                self.store.update(job_id, status='discovering', phase='candidate_snapshot')
                candidates = core.search_projects(
                    payload.get('keyword', ''), type_codes=payload.get('type_codes'),
                    agency_code=payload.get('agency_code', ''), max_pages=payload.get('max_pages', 0), session=session,
                    consult_date_from=payload.get('consult_date_from'), consult_date_to=payload.get('consult_date_to'),
                    progress_status=payload.get('progress_status', ''), biz_gubun=payload.get('biz_gubun', ''),
                    progress_stage_keys=payload.get('progress_stage_keys'))
                self.store.save_candidates(job_id, candidates)
            self.store.update(job_id, status='running', phase='document_scan' if kind == 'document' else 'spatial_scan')
            pending = self.store.candidates(job_id, only_pending=True)
            batch_size = int(payload['batch_size'])
            for start in range(0, len(pending), batch_size):
                if self.store.cancel_requested(job_id):
                    raise core.ScanCancelled('사용자 요청으로 취소했습니다.')
                batch_meta = pending[start:start + batch_size]
                candidates = [item[2] for item in batch_meta]
                ordinal_by_key = {key: ordinal for key, ordinal, _ in batch_meta}
                should_cancel = lambda: self.store.cancel_requested(job_id)
                if kind == 'document':
                    result = run_document_batch(payload, candidates, should_cancel, session)
                    normalized = document_outcomes(candidates, result, ordinal_by_key)
                    meta = self.store.get(job_id)['meta']
                    meta['stage_stats'] = _merge_stage_stats(meta.get('stage_stats', {}), result.get('stage_stats', {}))
                    if result.get('needs_refinement'):
                        meta['needs_refinement'] = True
                        meta.setdefault('refinement_hints', []).append(result.get('refinement_hint'))
                else:
                    result = run_spatial_batch(payload, candidates, should_cancel, session)
                    normalized = spatial_outcomes(candidates, result, ordinal_by_key)
                    meta = self.store.get(job_id)['meta']
                self.store.save_outcomes(job_id, normalized)
                self.store.update(job_id, meta=meta)
            self.store.update(job_id, status='done', phase='completed')
        except core.ScanCancelled:
            self.store.update(job_id, status='cancelled', phase='cancelled')
        except Exception as exc:
            self.store.update(job_id, status='error', phase='failed', error=str(exc))
        finally:
            session.close()


def _merge_stage_stats(total, batch):
    for stage, stats in batch.items():
        current = total.setdefault(stage, {'checked': 0, 'matched': 0})
        current['checked'] += stats.get('checked', 0)
        current['matched'] += stats.get('matched', 0)
    return total


def document_status(store, job_id, include_results=False, offset=0, limit=100):
    job = store.get(job_id)
    if not job or job['kind'] != 'document':
        return {'error': f'알 수 없는 job_id: {job_id}'}
    counts = store.result_counts(job_id)
    result = {'status': job['status'], 'checked': job['checked'], 'candidates_total': job['candidates_total'],
              'match_count': counts.get('match', 0), 'checked_no_match_count': counts.get('no_match', 0),
              'skipped_count': counts.get('skipped', 0), 'stage_stats': job['meta'].get('stage_stats', {}),
              'needs_refinement': bool(job['meta'].get('needs_refinement')),
              'refinement_hints': list(dict.fromkeys(filter(None, job['meta'].get('refinement_hints', [])))),
              'audit_samples': [], 'error': job['error'], 'current_phase': job['current_phase'],
              'updated_at': job['updated_at'], 'heartbeat_at': job['heartbeat_at'], 'resume_count': job['resume_count']}
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
              'current_phase': job['current_phase'], 'updated_at': job['updated_at'],
              'heartbeat_at': job['heartbeat_at'], 'resume_count': job['resume_count']}
    if include_results:
        if offset < 0 or limit <= 0:
            return {'error': 'result_offset은 0 이상, result_limit은 1 이상이어야 합니다.'}
        result.update(scanned=store.results(job_id, 'match', offset, limit) + store.results(job_id, 'no_match', offset, limit),
                      matches=store.results(job_id, 'match', offset, limit),
                      geocode_failures=store.results(job_id, 'geocode_failure', offset, limit),
                      spatial_failures=store.results(job_id, 'spatial_failure', offset, limit),
                      result_offset=offset, result_limit=limit)
    return result
