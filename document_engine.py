"""문서 후보 배치 실행과 후보별 단일 결과 정규화."""
import eiass_core as core
from models import candidate_key


def run_batch(payload, candidates, should_cancel, session=None):
    options = dict(payload)
    options.pop('batch_size', None)
    on_progress = options.pop('_on_progress', None)
    options.update(candidates=candidates, offset=0, max_candidates=len(candidates),
                   should_cancel=should_cancel, session=session, on_progress=on_progress)
    return core.search_projects_by_document_keyword(**options)


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
