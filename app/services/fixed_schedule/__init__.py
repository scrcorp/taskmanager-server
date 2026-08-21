"""고정 근무(Fixed Schedule) 도메인 패키지.

SoT: docs/99_inbox/2026-08-20-고정근무-구현계약.md §2·§3.

모듈 구성 (소유권 지도 §8)
- expand.py       — 패턴 → Occurrence 펼치기. **순수 함수, DB/IO 없음.**
- read.py         — virtual 합성(merge_virtual)            (server-patterns)
- patterns.py     — 패턴 그룹 CRUD·이동·종료                (server-patterns)
- validation.py   — 블록 겹침·기존 스케줄 겹침·가용성 검사  (server-patterns)
- materialize.py  — 창 안 실체화(sweep)·occurrence 편집/삭제/revert (server-patterns)

여기서는 공개 심볼만 재export 한다. 하위 모듈이 아직 없으면 import 하지 않는다
(패키지 import 만으로 DB 세션이 열리지 않도록 expand 는 항상 안전하다).
"""

from app.services.fixed_schedule.expand import Occurrence, dow_sun0, expand

__all__ = ["Occurrence", "dow_sun0", "expand"]
