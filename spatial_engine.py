"""공간 인접성 후보 배치 실행과 후보별 단일 결과 정규화."""
import eiass_core as core
from models import candidate_key


def run_batch(payload, candidates, should_cancel, session=None):
    options = dict(payload)
    options.pop('batch_size', None)
    options.update(candidates=candidates, offset=0, max_candidates=len(candidates), should_cancel=should_cancel, session=session)
    return core.scan_projects_protected_area_adjacency(**options)


def outcomes(candidates, result, ordinal_by_key):
    rows = {}
    keys_by_eia = {str(c.get('eia_cd')): candidate_key(c) for c in candidates}
    for item in result['scanned']:
        key = candidate_key(item)
        rows[key if key in ordinal_by_key else keys_by_eia.get(str(item.get('eia_cd')), key)] = ('match' if item.get('nearby_protected_areas') else 'no_match', item)
    for item in result['geocode_failures']:
        rows[keys_by_eia.get(str(item.get('eia_cd')), candidate_key(item))] = ('geocode_failure', item)
    for item in result['spatial_failures']:
        key = keys_by_eia.get(str(item.get('eia_cd')), candidate_key(item))
        if key in rows:
            rows[key] = ('spatial_failure', rows[key][1])
    return [(candidate_key(c), ordinal_by_key[candidate_key(c)], *rows.get(candidate_key(c),
            ('geocode_failure', {'name': c.get('name'), 'eia_cd': c.get('eia_cd'), 'reason': '처리 결과가 반환되지 않았습니다.'})))
            for c in candidates]
