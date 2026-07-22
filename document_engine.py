"""문서 후보 배치 실행과 후보별 단일 결과 정규화."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import inspect

import eiass_core as core
from config import DOC_DOWNLOAD_CONCURRENCY
from models import candidate_key


class ScanPayloadError(RuntimeError):
    """백그라운드 작업 제어값이 문서 검색 호출 경계로 누출된 경우."""


class RepeatedCandidateFailure(RuntimeError):
    """동일한 후보 처리 예외가 반복되어 작업 전체 결함/장애로 판단된 경우."""


_SCAN_CONTROL_KEYS = frozenset({
    'batch_size', 'survey_method', 'adaptive_strategy', '_on_progress',
})
_RUNTIME_QUERY_KEYS = frozenset({
    'session', 'candidates', 'offset', 'max_candidates', 'should_cancel', 'on_progress',
})
_DOCUMENT_QUERY_KEYS = (
    frozenset(inspect.signature(core.search_projects_by_document_keyword).parameters)
    - _RUNTIME_QUERY_KEYS
)
_REPEATED_FAILURE_LIMIT = 3


def _document_query_options(payload):
    """job payload에서 core 문서 검색 함수가 선언한 검색 인자만 분리한다.

    제어 인자는 명시적으로 소비하고, 그 밖의 알 수 없는 키는 조용히 버리지 않는다. 새 제어값을
    추가하면서 이 경계를 갱신하지 않으면 첫 후보를 열기 전에 작업 오류로 드러나야 한다.
    """
    keys = set(payload)
    unexpected = sorted(keys - _SCAN_CONTROL_KEYS - _DOCUMENT_QUERY_KEYS)
    if unexpected:
        raise ScanPayloadError(
            '지원하지 않는 문서 스캔 payload 키가 있습니다: ' + ', '.join(unexpected))
    return {key: value for key, value in payload.items() if key in _DOCUMENT_QUERY_KEYS}


def _merge_stage_stats(total, batch):
    for stage, stats in batch.items():
        current = total.setdefault(stage, {'checked': 0, 'matched': 0})
        current['checked'] += stats.get('checked', 0)
        current['matched'] += stats.get('matched', 0)


def run_batch(payload, candidates, should_cancel, session=None, on_candidate_complete=None):
    """후보를 독립 작업으로 처리해 느린 PDF 하나가 다른 후보의 저장을 막지 않게 한다."""
    options = _document_query_options(payload)
    on_progress = payload.get('_on_progress')
    audit_remaining = max(0, int(options.pop('audit_sample_size', 0) or 0))
    candidates = list(candidates)
    aggregate = {
        'candidates_total': len(candidates), 'offset': 0, 'checked': 0,
        'next_offset': None, 'has_more': False,
        'doc_title_contains': options.get('doc_title_contains') or None,
        'skipped': [], 'matches': [], 'checked_no_match': [], 'stage_stats': {},
        'needs_refinement': False, 'refinement_hint': None, 'audit_sample': None,
        'date_filter_exclusions': [], 'search_summary': '',
    }

    def scan_one(index, candidate):
        worker_session = core._session()
        try:
            def report(progress):
                if on_progress:
                    on_progress(dict(progress, candidate_index=index,
                                     current_candidate=progress.get('current_candidate') or candidate.get('name'),
                                     current_eia_cd=progress.get('current_eia_cd') or candidate.get('eia_cd')))

            one_options = dict(options)
            one_options.update(
                candidates=[candidate], offset=0, max_candidates=1,
                audit_sample_size=1 if index < audit_remaining else 0,
                should_cancel=should_cancel, session=worker_session, on_progress=report)
            return core.search_projects_by_document_keyword(**one_options)
        finally:
            worker_session.close()

    max_workers = max(1, min(DOC_DOWNLOAD_CONCURRENCY, len(candidates)))
    completed = 0
    failure_counts = {}
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='eiass-candidate') as pool:
        future_map = {pool.submit(scan_one, index, candidate): candidate
                      for index, candidate in enumerate(candidates)}
        for future in as_completed(future_map):
            if should_cancel and should_cancel():
                raise core.ScanCancelled('사용자 요청으로 취소되었습니다.')
            candidate = future_map[future]
            try:
                result = future.result()
            except core.ScanCancelled:
                raise
            except TypeError as exc:
                for pending in future_map:
                    pending.cancel()
                raise ScanPayloadError(f'문서 검색 호출 계약 오류: {exc}') from exc
            except Exception as exc:
                signature = (type(exc).__name__, str(exc))
                failure_counts[signature] = failure_counts.get(signature, 0) + 1
                if failure_counts[signature] >= _REPEATED_FAILURE_LIMIT:
                    for pending in future_map:
                        pending.cancel()
                    raise RepeatedCandidateFailure(
                        f'동일 후보 처리 오류가 {_REPEATED_FAILURE_LIMIT}회 반복되어 조기 중단했습니다: '
                        f'{signature[0]}: {signature[1]}') from exc
                result = {
                    'matches': [], 'checked_no_match': [],
                    'skipped': [{'name': candidate.get('name'), 'eia_cd': candidate.get('eia_cd'),
                                 'reason': f'후보 처리 실패: {exc}'}],
                    'stage_stats': {}, 'needs_refinement': False,
                    'refinement_hint': None, 'audit_sample': None,
                    'date_filter_exclusions': [], 'checked': 1,
                }
            completed += 1
            aggregate['checked'] += result.get('checked', 1)
            aggregate['matches'].extend(result.get('matches', []))
            aggregate['checked_no_match'].extend(result.get('checked_no_match', []))
            aggregate['skipped'].extend(result.get('skipped', []))
            _merge_stage_stats(aggregate['stage_stats'], result.get('stage_stats', {}))
            aggregate['date_filter_exclusions'].extend(result.get('date_filter_exclusions', []))
            if result.get('needs_refinement'):
                aggregate['needs_refinement'] = True
                aggregate['refinement_hint'] = result.get('refinement_hint')
            if result.get('audit_sample'):
                aggregate['audit_sample'] = result['audit_sample']
            if on_candidate_complete:
                on_candidate_complete(candidate, result)
            if on_progress:
                on_progress({'phase': 'candidate_checkpoint', 'activity_state': 'running',
                             'batch_completed': completed, 'batch_total': len(candidates),
                             'current_candidate': candidate.get('name'),
                             'current_eia_cd': candidate.get('eia_cd')})

    aggregate['date_filter_exclusions'] = list(dict.fromkeys(aggregate['date_filter_exclusions']))
    total_snippets = sum(
        len(item.get('matched_snippets', [])) for item in aggregate['matches'])
    reference_like = sum(
        1 for item in aggregate['matches'] for snippet in item.get('matched_snippets', [])
        if snippet.get('reference_like'))
    match_rate = len(aggregate['matches']) / aggregate['checked'] if aggregate['checked'] else 0.0
    reference_like_ratio = reference_like / total_snippets if total_snippets else 0.0
    if aggregate['checked'] >= 5 and (match_rate > 0.5 or reference_like_ratio > 0.4):
        aggregate['needs_refinement'] = True
        aggregate['refinement_hint'] = (
            f'매칭률이 높거나({match_rate:.0%}) 매칭 문맥 중 참고문헌/부록 비율이 높습니다'
            f'({reference_like_ratio:.0%}, {reference_like}/{total_snippets}건). '
            '최종 답변 전에 문맥 키워드를 추가할지 확인하는 것을 권장합니다.')
    aggregate['search_summary'] = (
        f"후보 {aggregate['checked']}건 처리: 매칭 {len(aggregate['matches'])}건 / "
        f"매칭 없음 {len(aggregate['checked_no_match'])}건 / 미확인·제외 {len(aggregate['skipped'])}건")
    return aggregate


def outcomes(candidates, result, ordinal_by_key):
    rows = {}
    keys_by_eia = {str(c.get('eia_cd')): candidate_key(c) for c in candidates}
    for outcome, items in (('match', result['matches']), ('skipped', result['skipped']), ('no_match', result['checked_no_match'])):
        for item in items:
            key = candidate_key(item)
            rows[key if key in ordinal_by_key else keys_by_eia.get(str(item.get('eia_cd')), key)] = (outcome, item)
    return [(candidate_key(c), ordinal_by_key[candidate_key(c)], *rows.get(candidate_key(c),
            ('skipped', {'name': c.get('name'), 'eia_cd': c.get('eia_cd'), 'reason': '처리 결과가 반환되지 않았습니다.'})))
            for c in candidates]
