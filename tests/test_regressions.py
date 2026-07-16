# -*- coding: utf-8 -*-
import threading
import time
import unittest
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

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


class OutageDiagnosisTests(unittest.TestCase):
    """실패 원인을 어느 서버 탓으로 돌리는지 검증한다.

    핵심은 "느릴 뿐 살아있는 서버"를 장애로 단정하지 않는 것이다. 장애로 단정하면 사용자에게
    조회 불가라고 잘못 안내하게 된다.
    """

    OK = {'ok': True, 'kind': 'ok', 'status_code': 200, 'latency_ms': 100, 'error': None}
    DOWN = {'ok': False, 'kind': 'down', 'status_code': 503, 'latency_ms': 80, 'error': None}
    SLOW = {'ok': False, 'kind': 'slow', 'status_code': None, 'latency_ms': 30000,
            'error': 'Read timed out.'}

    def setUp(self):
        self.original_probe = core.probe_services

    def tearDown(self):
        core.probe_services = self.original_probe

    def _stub_probes(self, mapping):
        def _fake(names, session=None, use_cache=True):
            return {n: mapping[n] for n in names if n in mapping}
        core.probe_services = _fake

    def test_down_service_is_named_as_outage(self):
        self._stub_probes({'eiass_site': self.OK, 'eiass_search_api': self.DOWN})
        result = mcp_server._fail(requests.exceptions.ConnectionError('refused'), mcp_server.SVC_SEARCH)
        self.assertTrue(result['outage'])
        self.assertEqual(result['affected_services'], ['eiass_search_api'])
        self.assertIn('EIASS 검색 API', result['error'])

    def test_slow_service_is_not_reported_as_outage(self):
        self._stub_probes({'eiass_site': self.OK, 'eiass_search_api': self.SLOW})
        result = mcp_server._fail(requests.exceptions.ReadTimeout('timed out'), mcp_server.SVC_SEARCH)
        self.assertFalse(result['outage'])
        self.assertTrue(result['degraded'])
        self.assertIn('지연', result['error'])

    def test_validation_error_does_not_probe_services(self):
        calls = []

        def _fake(names, session=None, use_cache=True):
            calls.append(names)
            return {}
        core.probe_services = _fake
        result = mcp_server._fail(core.EiassError('consult_date_from은 YYYY-MM-DD 형식이어야 합니다.'),
                                  mcp_server.SVC_SEARCH)
        self.assertEqual(calls, [])
        self.assertEqual(result, {'error': 'consult_date_from은 YYYY-MM-DD 형식이어야 합니다.'})

    def test_only_services_the_tool_uses_are_blamed(self):
        # EIASS도 같이 죽어 있어도 지오코딩 실패의 범인으로 지목하면 오진이다.
        self._stub_probes({'vworld': self.DOWN, 'eiass_site': self.DOWN})
        result = mcp_server._fail(requests.exceptions.ConnectionError('x'), mcp_server.SVC_GEO)
        self.assertEqual(result['affected_services'], ['vworld'])

    def test_healthy_services_are_not_blamed(self):
        self._stub_probes({'eiass_site': self.OK, 'eiass_search_api': self.OK})
        result = mcp_server._fail(requests.exceptions.ConnectionError('원인 불명'), mcp_server.SVC_SEARCH)
        self.assertFalse(result['outage'])
        self.assertFalse(result['degraded'])
        self.assertIn('서버 장애는 아닙니다', result['error'])

    def test_http_status_failures_count_as_network_errors(self):
        self.assertTrue(core.is_network_error(core.EiassNetworkError('문서 다운로드 실패: HTTP 503')))
        self.assertFalse(core.is_network_error(core.EiassError('max_candidates는 1 이상이어야 합니다.')))

    def test_wrapped_requests_error_is_detected_through_cause_chain(self):
        try:
            try:
                raise requests.exceptions.ReadTimeout('timed out')
            except requests.exceptions.ReadTimeout as exc:
                raise core.EiassError('조회 실패') from exc
        except core.EiassError as exc:
            self.assertTrue(core.is_network_error(exc))


class RetrySessionTests(unittest.TestCase):
    """느린 서버 때문에 멀쩡한 요청이 실패로 확정되지 않는지 검증한다."""

    def _serve(self, failures, delay=0.0):
        state = {'hits': 0}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                state['hits'] += 1
                if state['hits'] <= failures:
                    if delay:
                        time.sleep(delay)
                    else:
                        self.send_response(503)
                        self.end_headers()
                        return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'OK')

        class QuietServer(HTTPServer):
            def handle_error(self, request, client_address):
                pass  # 클라이언트 타임아웃으로 끊기는 건 이 테스트에선 정상이다

        server = QuietServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}/', state

    def test_transient_5xx_is_retried_until_success(self):
        url, state = self._serve(failures=2)
        response = core._session().get(url, timeout=(5, 5))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(state['hits'], 3)

    def test_read_timeout_from_slow_server_is_retried(self):
        url, state = self._serve(failures=2, delay=2.0)
        response = core._session().get(url, timeout=(5, 0.5))
        self.assertEqual(response.status_code, 200)

    def test_health_check_session_does_not_retry(self):
        # 진단은 "지금" 상태를 봐야 하므로 재시도하면 안 된다.
        url, state = self._serve(failures=2)
        response = core._session(retry=False).get(url, timeout=(5, 5))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(state['hits'], 1)


if __name__ == '__main__':
    unittest.main()
