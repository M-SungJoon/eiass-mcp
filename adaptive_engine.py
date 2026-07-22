"""재현 가능한 적응형 조사 순서와 전수조사 승격 판정.

AI는 사용자와 확정한 검색 조건을 바꾸지 않은 채 priority_terms 등 전략만 제안한다. 이 모듈은
고정 조건의 후보 스냅샷 안에서 우선표본과 계층 감사표본을 선택하고, 성공 근거가 부족하면 남은
후보 전부를 전수조사하도록 결정한다.
"""
import hashlib
import json
import math

from config import (ADAPTIVE_AUDIT_SIZE, ADAPTIVE_INITIAL_SAMPLE_SIZE,
                    ADAPTIVE_MAX_CANDIDATES, ADAPTIVE_MAX_FAILURE_PERCENT,
                    ADAPTIVE_MAX_PERCENT, ADAPTIVE_MIN_PERCENT,
                    ADAPTIVE_ROUND_SIZE, ADAPTIVE_SATURATION_ROUNDS)
from models import candidate_key


def _bounded(value, default, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def normalize_strategy(raw_json='', default_terms=None):
    """AI가 낸 전략 JSON을 공개된 안전 범위로 정규화한다."""
    if raw_json in (None, ''):
        supplied = {}
    elif isinstance(raw_json, dict):
        supplied = dict(raw_json)
    else:
        try:
            supplied = json.loads(raw_json)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f'adaptive_strategy_json이 유효한 JSON 객체가 아닙니다: {exc}') from exc
    if not isinstance(supplied, dict):
        raise ValueError('adaptive_strategy_json은 JSON 객체여야 합니다.')

    terms = supplied.get('priority_terms')
    if terms is None:
        terms = default_terms or []
    if isinstance(terms, str):
        terms = [part.strip() for part in terms.split(',')]
    if not isinstance(terms, list):
        raise ValueError('priority_terms는 문자열 배열이어야 합니다.')
    terms = list(dict.fromkeys(str(term).strip() for term in terms if str(term).strip()))[:30]

    objective = str(supplied.get('objective') or 'coverage').strip().lower()
    if objective not in ('coverage', 'discovery'):
        raise ValueError("objective는 'coverage' 또는 'discovery'여야 합니다.")
    strategy = {
        'objective': objective,
        'priority_terms': terms,
        'initial_sample_size': _bounded(
            supplied.get('initial_sample_size'), ADAPTIVE_INITIAL_SAMPLE_SIZE, 10, 100),
        'round_size': _bounded(supplied.get('round_size'), ADAPTIVE_ROUND_SIZE, 10, 100),
        'audit_size': _bounded(supplied.get('audit_size'), ADAPTIVE_AUDIT_SIZE, 5, 50),
        'max_adaptive_candidates': _bounded(
            supplied.get('max_adaptive_candidates'), ADAPTIVE_MAX_CANDIDATES, 30, 1000),
        'max_adaptive_percent': _bounded(
            supplied.get('max_adaptive_percent'), ADAPTIVE_MAX_PERCENT, 25, 90),
        'min_adaptive_percent': _bounded(
            supplied.get('min_adaptive_percent'), ADAPTIVE_MIN_PERCENT, 10, 80),
        'saturation_rounds': _bounded(
            supplied.get('saturation_rounds'), ADAPTIVE_SATURATION_ROUNDS, 1, 5),
        'max_failure_percent': _bounded(
            supplied.get('max_failure_percent'), ADAPTIVE_MAX_FAILURE_PERCENT, 0, 25),
        'minimum_matches': _bounded(supplied.get('minimum_matches'), 1, 0, 1000),
    }
    if strategy['audit_size'] >= strategy['round_size']:
        strategy['audit_size'] = max(5, strategy['round_size'] // 3)
    if strategy['min_adaptive_percent'] > strategy['max_adaptive_percent']:
        strategy['min_adaptive_percent'] = strategy['max_adaptive_percent']
    return strategy


def initial_state(total, strategy):
    target = min(
        total,
        strategy['max_adaptive_candidates'],
        max(strategy['initial_sample_size'], math.ceil(total * strategy['max_adaptive_percent'] / 100)),
    )
    minimum = min(target, max(
        strategy['initial_sample_size'], math.ceil(total * strategy['min_adaptive_percent'] / 100)))
    return {
        'survey_phase': 'adaptive', 'coverage_status': 'adaptive_running',
        'rounds_completed': 0, 'adaptive_checked': 0, 'adaptive_matches': 0,
        'adaptive_skipped': 0, 'consecutive_saturated_rounds': 0,
        'last_round_new_matches': None, 'last_round_audit_matches': None,
        'adaptive_target': target, 'adaptive_minimum': minimum,
        'current_round_keys': [], 'current_round_roles': {}, 'decision_log': [],
    }


def _stable_rank(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()


def _candidate_text(candidate):
    return ' '.join(str(value) for value in candidate.values()
                    if isinstance(value, (str, int, float))).lower()


def _year(candidate):
    for key in ('consult_date', 'consultDate', 'consult_dt', 'date'):
        value = str(candidate.get(key) or '')
        if len(value) >= 4 and value[:4].isdigit():
            return value[:4]
    return 'unknown'


def _stratum(candidate):
    return '|'.join((
        str(candidate.get('view_type') or candidate.get('type') or 'unknown'),
        str(candidate.get('agency') or candidate.get('agency_name') or 'unknown'),
        _year(candidate),
    ))


def _similarity(candidate, matched_candidates):
    score = 0
    for matched in matched_candidates:
        if candidate.get('agency') and candidate.get('agency') == matched.get('agency'):
            score += 8
        if candidate.get('view_type') and candidate.get('view_type') == matched.get('view_type'):
            score += 4
        if _year(candidate) == _year(matched):
            score += 3
    return score


def select_round(pending_rows, strategy, state, matched_candidates=None):
    """우선 후보(exploit)와 전체 분포 감사 후보(audit)를 섞은 다음 라운드를 반환한다."""
    pending_rows = list(pending_rows)
    if not pending_rows:
        return [], {}
    remaining_budget = max(0, state['adaptive_target'] - state['adaptive_checked'])
    if not remaining_budget:
        return [], {}
    round_index = int(state.get('rounds_completed') or 0)
    wanted = strategy['initial_sample_size'] if round_index == 0 else strategy['round_size']
    wanted = min(wanted, remaining_budget, len(pending_rows))
    audit_count = 0 if round_index == 0 else min(strategy['audit_size'], wanted // 2)
    exploit_count = wanted - audit_count
    matched_candidates = matched_candidates or []
    terms = [term.lower() for term in strategy['priority_terms']]

    def score(row):
        candidate = row[2]
        text = _candidate_text(candidate)
        term_score = sum(100 for term in terms if term.lower() in text)
        return term_score + _similarity(candidate, matched_candidates)

    ordered = sorted(
        pending_rows,
        key=lambda row: (-score(row), _stable_rank(f"exploit|{round_index}|{row[0]}")),
    )
    exploit = ordered[:exploit_count]
    selected_keys = {row[0] for row in exploit}
    remaining = [row for row in pending_rows if row[0] not in selected_keys]

    # 감사표본은 단계/기관/연도 계층을 round-robin하며 고정 해시 순서로 뽑는다.
    strata = {}
    for row in remaining:
        strata.setdefault(_stratum(row[2]), []).append(row)
    for name, rows in strata.items():
        rows.sort(key=lambda row: _stable_rank(f"audit|{round_index}|{name}|{row[0]}"))
    audit = []
    names = sorted(strata, key=lambda name: _stable_rank(f"stratum|{round_index}|{name}"))
    while names and len(audit) < audit_count:
        next_names = []
        for name in names:
            rows = strata[name]
            if rows and len(audit) < audit_count:
                audit.append(rows.pop(0))
            if rows:
                next_names.append(name)
        names = next_names
    selected = exploit + audit
    roles = {row[0]: 'exploit' for row in exploit}
    roles.update({row[0]: 'audit' for row in audit})
    return selected, roles


def evaluate_round(state, strategy, round_outcomes, total):
    """라운드 결과를 누적하고 continue/success/fallback 중 하나를 결정한다."""
    outcomes = list(round_outcomes.values())
    new_matches = sum(1 for outcome, _ in outcomes if outcome == 'match')
    skipped = sum(1 for outcome, _ in outcomes if outcome == 'skipped')
    roles = state.get('current_round_roles') or {}
    audit_matches = sum(
        1 for key, (outcome, _) in round_outcomes.items()
        if roles.get(key) == 'audit' and outcome == 'match')
    checked = len(outcomes)
    state['rounds_completed'] = int(state.get('rounds_completed') or 0) + 1
    state['adaptive_checked'] = int(state.get('adaptive_checked') or 0) + checked
    state['adaptive_matches'] = int(state.get('adaptive_matches') or 0) + new_matches
    state['adaptive_skipped'] = int(state.get('adaptive_skipped') or 0) + skipped
    state['last_round_new_matches'] = new_matches
    state['last_round_audit_matches'] = audit_matches
    state['consecutive_saturated_rounds'] = (
        int(state.get('consecutive_saturated_rounds') or 0) + 1
        if new_matches == 0 and audit_matches == 0 else 0)
    state['current_round_keys'] = []
    state['current_round_roles'] = {}
    coverage = state['adaptive_checked'] / total if total else 1.0
    failure_rate = state['adaptive_skipped'] / state['adaptive_checked'] if state['adaptive_checked'] else 0.0
    entry = {
        'round': state['rounds_completed'], 'checked': checked,
        'new_matches': new_matches, 'audit_matches': audit_matches, 'skipped': skipped,
        'adaptive_checked_total': state['adaptive_checked'],
        'candidate_coverage_percent': round(coverage * 100, 1),
        'failure_percent': round(failure_rate * 100, 1),
    }

    action = 'continue'
    reason = '다음 우선·감사 표본으로 조사 범위를 확장합니다.'
    if failure_rate * 100 > strategy['max_failure_percent']:
        action = 'fallback'
        reason = '적응형 조사 실패율이 허용 기준을 초과해 미확인 후보를 포함한 전수조사로 전환합니다.'
    elif (state['adaptive_checked'] >= state['adaptive_minimum'] and
          state['adaptive_matches'] >= strategy['minimum_matches'] and
          state['consecutive_saturated_rounds'] >= strategy['saturation_rounds'] and
          audit_matches == 0):
        action = 'success'
        reason = '연속 포화 라운드와 계층 감사표본 무적중 기준을 충족했습니다.'
    elif state['adaptive_checked'] >= state['adaptive_target']:
        action = 'fallback'
        reason = '적응형 조사 상한까지 포화 근거가 부족해 남은 후보 전수조사로 전환합니다.'
    entry.update(action=action, reason=reason)
    state.setdefault('decision_log', []).append(entry)
    state['coverage_status'] = {
        'success': 'adaptive_complete', 'fallback': 'coverage_insufficient',
    }.get(action, 'adaptive_running')
    return action, reason
