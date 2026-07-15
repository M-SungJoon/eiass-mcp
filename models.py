"""백그라운드 작업에서 공유하는 작은 데이터 모델 도우미."""
import time


def candidate_key(candidate):
    return '|'.join(str(candidate.get(name, '')) for name in ('view_type', 'eia_cd', 'revirpt_seq'))


def now_epoch():
    return time.time()
