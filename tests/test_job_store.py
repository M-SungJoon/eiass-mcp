# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
import time

from job_store import JobStore
from models import candidate_key
import scan_engine


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = JobStore(os.path.join(self.temp.name, 'jobs.sqlite3'))
        self.candidate = {'name': 'A', 'view_type': 'eia', 'eia_cd': 'E1', 'revirpt_seq': '1'}

    def tearDown(self):
        self.temp.cleanup()

    def test_candidate_has_one_replaceable_outcome(self):
        self.store.create('job', 'document', {'batch_size': 1})
        self.store.save_candidates('job', [self.candidate])
        key = candidate_key(self.candidate)
        self.store.save_outcomes('job', [(key, 0, 'skipped', {'eia_cd': 'E1', 'reason': 'first'})])
        self.store.save_outcomes('job', [(key, 0, 'match', {'eia_cd': 'E1'})])
        self.assertEqual(self.store.result_counts('job'), {'match': 1})
        self.assertEqual(self.store.get('job')['checked'], 1)

    def test_running_jobs_are_queued_for_restart_recovery(self):
        self.store.create('job', 'spatial', {'batch_size': 1})
        self.store.update('job', status='running', phase='spatial_scan')
        self.assertEqual(self.store.recover_interrupted(), ['job'])
        job = self.store.get('job')
        self.assertEqual(job['status'], 'queued')
        self.assertEqual(job['current_phase'], 'recovery_pending')
        self.assertEqual(job['resume_count'], 1)

    def test_pending_candidates_exclude_saved_results(self):
        second = dict(self.candidate, eia_cd='E2')
        self.store.create('job', 'document', {'batch_size': 2})
        self.store.save_candidates('job', [self.candidate, second])
        self.store.save_outcomes('job', [(candidate_key(self.candidate), 0, 'no_match', {'eia_cd': 'E1'})])
        self.assertEqual([row[2]['eia_cd'] for row in self.store.candidates('job', only_pending=True)], ['E2'])

    def test_runner_persists_snapshot_and_result(self):
        original_search = scan_engine.core.search_projects
        original_session = scan_engine.core._session
        original_run = scan_engine.run_document_batch
        try:
            class Session:
                def close(self):
                    pass
            scan_engine.core._session = Session
            scan_engine.core.search_projects = lambda *args, **kwargs: [self.candidate]
            scan_engine.run_document_batch = lambda payload, candidates, cancel, session: {
                'matches': [], 'skipped': [], 'checked_no_match': [{'eia_cd': 'E1', 'name': 'A'}],
                'stage_stats': {}, 'needs_refinement': False,
            }
            runner = scan_engine.ScanRunner(self.store, worker_count=1, queue_size=1)
            self.store.create('job', 'document', {'batch_size': 1, 'keyword': '', 'type_codes': None,
                                                   'agency_code': '', 'max_pages': 0, 'text_queries': ['x']})
            self.assertTrue(runner.submit('job'))
            for _ in range(50):
                if self.store.get('job')['status'] == 'done':
                    break
                time.sleep(0.01)
            self.assertEqual(self.store.get('job')['status'], 'done')
            self.assertEqual(self.store.result_counts('job'), {'no_match': 1})
        finally:
            scan_engine.core.search_projects = original_search
            scan_engine.core._session = original_session
            scan_engine.run_document_batch = original_run


if __name__ == '__main__':
    unittest.main()
