"""Unit tests — attendance_device_service module (mock / no DB).

DB 안 쓰는 순수 함수 검증.

[작성됨] — 이번 phase
- generate_clockin_pin (6자리 / zero-pad)

[작성 필요] — 추후
- generate_device_token  (cryptographic 강도, 길이)
- hash_token             (해시 결정성, 다른 입력에 다른 출력)
- generate_device_name   (포맷 'Terminal-XXXX')

DB 사용하는 케이스는 tests/integration/services/test_attendance_device_service.py 에.
"""

from __future__ import annotations

import re

from app.services.attendance_device_service import generate_clockin_pin


def test_generate_clockin_pin_returns_six_digit_string() -> None:
    """6자리 숫자 문자열 반환."""
    pin = generate_clockin_pin()
    assert isinstance(pin, str)
    assert len(pin) == 6
    assert re.fullmatch(r"\d{6}", pin) is not None


def test_generate_clockin_pin_zero_padded() -> None:
    """작은 숫자 (예: 0~999) 도 6자리로 zero-pad. 64회 통계적 검증."""
    for _ in range(64):
        pin = generate_clockin_pin()
        assert len(pin) == 6
