# -*- coding: utf-8 -*-
import unittest
from datetime import date

import eiass_core as core
import mcp_server


class _Response:
    def __init__(self, text='', status_code=200, payload=None):
        self.text = text
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.original_search = core.search_projects
        self.original_detail = core.get_project_detail
        self.original_document = core._get_full_document_text
        self.original_version_cache = dict(core._KDPA_VERSION_CACHE)
        self.original_session = core._session

    def tearDown(self):
        core.search_projects = self.original_search
        core.get_project_detail = self.original_detail
        core._get_full_document_text = self.original_document
        core._KDPA_VERSION_CACHE.clear()
        core._KDPA_VERSION_CACHE.update(self.original_version_cache)
        core._session = self.original_session
        mcp_server._jobs.clear()

    def test_repeated_full_search_page_is_rejected(self):
        row = "<tr><td class='title'><a href=\"javascript:view('eia','EIA1','1')\">x</a></td></tr>"

        class Session:
            def get(self, *args, **kwargs):
                return _Response()

            def post(self, *args, **kwargs):
                return _Response('<table class="disTm"><tbody>' + row * 100 + '</tbody></table>')

        with self.assertRaises(core.EiassError):
            core.search_projects(keyword='x', type_codes=['E'], session=Session())

    def test_date_filter_rejects_invalid_format_and_unknown_dates(self):
        with self.assertRaises(core.EiassError):
            core.search_projects(keyword='x', consult_date_from='2024.01.01', session=object())
        unknown = {'type': '환경영향평가', 'date': '-', 'comp_date': '-', 'progress_status': ''}
        self.assertFalse(core._passes_extra_filters(unknown, date(2024, 1, 1), date(2024, 12, 31)))

    def test_after_survey_uses_completion_date_when_date_is_dash(self):
        item = {'type': core.TYPE_NAME_MAP['A'], 'date': '-', 'comp_date': '2024.01.01', 'progress_status': ''}
        self.assertTrue(core._passes_extra_filters(item, date(2024, 1, 1), date(2024, 12, 31)))

    def test_unknown_completion_date_is_reported_as_date_filter_exclusion(self):
        exclusions = []
        item = {'name': 'unknown', 'eia_cd': 'x', 'type': 'E', 'date': '-', 'comp_date': '-', 'progress_status': ''}
        self.assertFalse(core._passes_extra_filters(item, date(2024, 1, 1), date(2024, 12, 31),
                                                    date_filter_exclusions=exclusions))
        self.assertEqual(exclusions[0]['eia_cd'], 'x')

    def test_invalid_paging_is_rejected(self):
        candidates = [{'name': 'A', 'eia_cd': '1', 'view_type': 'eia', 'revirpt_seq': '1'}]
        with self.assertRaises(core.EiassError):
            core.search_projects_by_document_keyword(['q'], candidates=candidates, max_candidates=0)
        with self.assertRaises(core.EiassError):
            core.scan_projects_protected_area_adjacency(candidates=candidates, offset=-1)

    def test_partial_document_failure_has_one_candidate_outcome(self):
        candidate = {'name': 'A', 'eia_cd': '1', 'view_type': 'eia', 'revirpt_seq': '1'}
        core.search_projects = lambda *args, **kwargs: [candidate]
        core.get_project_detail = lambda *args, **kwargs: {
            'fields': {}, 'stage_docs': {'본안': {'pdf': [
                {'seq': 'bad', 'name': 'bad.pdf', 'is_pdf': True},
                {'seq': 'ok', 'name': 'ok.pdf', 'is_pdf': True},
            ]}}
        }

        def document(seq, session=None):
            if seq == 'bad':
                raise RuntimeError('download failed')
            return {'text': 'nothing', 'page_offsets': [7], 'pages': 1, 'from_cache': False}

        core._get_full_document_text = document
        result = core.search_projects_by_document_keyword(['q'], stages=('본안',))
        self.assertEqual([x['eia_cd'] for x in result['skipped']], ['1'])
        self.assertEqual(result['checked_no_match'], [])
        self.assertEqual(result['matches'], [])

    def test_inside_polygon_distance_is_zero(self):
        geometry = {'type': 'Polygon', 'coordinates': [[[-1, -1], [1, -1], [1, 1], [-1, 1], [-1, -1]]]}
        self.assertEqual(core._feature_min_distance_m(0, 0, geometry), 0.0)

    def test_kdpa_version_failure_uses_default_with_diagnostics(self):
        class FailingSession:
            def post(self, *args, **kwargs):
                raise ConnectionError('offline')

        core._KDPA_VERSION_CACHE.update(version=None, fetched_at=0)
        version, diagnostic = core._kdpa_latest_version(FailingSession(), return_diagnostics=True)
        self.assertEqual(version, core.KDPA_DEFAULT_VERSION)
        self.assertEqual(diagnostic['source'], 'default')
        self.assertTrue(diagnostic['errors'])

    def test_default_kdpa_query_uses_two_nonduplicated_layers(self):
        class Session:
            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append(kwargs['params'])
                return _Response(payload={'features': []})

        session = Session()
        core.find_nearby_protected_areas(127.0, 37.0, session=session, version='2025_ver')
        self.assertEqual(len(session.calls), 2)

    def test_background_job_uses_one_candidate_snapshot(self):
        candidates = [
            {'name': 'A', 'eia_cd': '1', 'view_type': 'eia', 'revirpt_seq': '1'},
            {'name': 'B', 'eia_cd': '2', 'view_type': 'eia', 'revirpt_seq': '1'},
            {'name': 'C', 'eia_cd': '3', 'view_type': 'eia', 'revirpt_seq': '1'},
        ]
        search_calls, batches = [], []

        class Session:
            def close(self):
                pass

        core._session = Session
        core.search_projects = lambda *args, **kwargs: search_calls.append(kwargs) or list(candidates)

        def scan(*args, **kwargs):
            offset = kwargs['offset']
            snapshot = kwargs['candidates']
            batches.append((offset, [item['eia_cd'] for item in snapshot]))
            current = snapshot[offset:offset + kwargs['max_candidates']]
            checked = len(current)
            return {
                'checked': checked, 'candidates_total': len(snapshot), 'matches': [], 'skipped': [],
                'checked_no_match': [{'eia_cd': item['eia_cd']} for item in current],
                'audit_sample': None, 'needs_refinement': False, 'refinement_hint': None,
                'stage_stats': {}, 'has_more': offset + checked < len(snapshot),
                'next_offset': offset + checked if offset + checked < len(snapshot) else None,
            }

        original_scan = core.search_projects_by_document_keyword
        core.search_projects_by_document_keyword = scan
        try:
            mcp_server._jobs['job'] = {
                'status': 'queued', 'checked': 0, 'candidates_total': None, 'matches': [], 'skipped': [],
                'checked_no_match': [], 'error': None, 'cancel': False, 'stage_stats': {},
                'needs_refinement': False, 'refinement_hints': [], 'audit_samples': [],
                'current_phase': 'queued', 'updated_at': 0,
            }
            mcp_server._run_scan_job('job', {
                'text_queries': ['q'], 'max_candidates': 2, 'keyword': 'x', 'type_codes': ['E'],
                'agency_code': '', 'max_pages': 0, 'consult_date_from': None, 'consult_date_to': None,
                'progress_status': '완료', 'biz_gubun': '', 'progress_stage_keys': None,
                'match_mode': 'any', 'stages': ('본안',), 'doc_title_contains': None,
                'audit_sample_size': 0,
            })
        finally:
            core.search_projects_by_document_keyword = original_scan

        self.assertEqual(len(search_calls), 1)
        self.assertEqual(batches, [(0, ['1', '2', '3']), (2, ['1', '2', '3'])])
        self.assertEqual(mcp_server._jobs['job']['status'], 'done')


if __name__ == '__main__':
    unittest.main()
