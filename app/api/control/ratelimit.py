"""로그인 무차별 대입 방어 — 메모리 기반 실패 카운터.

In-memory failed-login limiter for the control plane login.
Single-process, single-operator scale — no Redis needed. Resets on success.
"""

import time

# IP(또는 프록시 IP) → 실패 시각 리스트
_FAILS: dict[str, list[float]] = {}

WINDOW_SECONDS: int = 15 * 60  # 15분 윈도
MAX_FAILS: int = 5  # 윈도 내 5회 실패 시 잠금


def _recent(ip: str, now: float) -> list[float]:
    fails = [t for t in _FAILS.get(ip, []) if now - t < WINDOW_SECONDS]
    if fails:
        _FAILS[ip] = fails
    else:
        _FAILS.pop(ip, None)
    return fails


def is_locked(ip: str) -> bool:
    """현재 잠금 상태인지."""
    return len(_recent(ip, time.time())) >= MAX_FAILS


def record_fail(ip: str) -> None:
    """로그인 실패 1회 기록."""
    _FAILS.setdefault(ip, []).append(time.time())


def reset(ip: str) -> None:
    """로그인 성공 시 카운터 초기화."""
    _FAILS.pop(ip, None)
