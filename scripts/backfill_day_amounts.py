"""동결 payroll entry 의 일별 금액(days[].*_amount) backfill — idempotent.

DayDetail 에 금액 필드가 생기기 전 확정된 기간은 일별 금액이 비어 있다
(명세서/콘솔에서 "—"). 근태·시급 원본이 그대로라 다시 계산해 채울 수 있다.

안전장치 (payroll_backfill_service 참조):
- confirmed 기간만, payroll_events 는 건드리지 않는 읽기 전용 재계산
- 재계산이 동결 스냅샷(일별 분/rate, 구간, 스칼라 급여)과 완전히 일치할 때만
  금액 4필드 patch. 하나라도 다르면 skip + 사유 출력 (확정 후 원본이 바뀐 것)
- 이미 금액이 있는 entry 는 no-op

사용:
    python scripts/backfill_day_amounts.py --period <uuid>            # dry-run
    python scripts/backfill_day_amounts.py --period <uuid> --apply
    python scripts/backfill_day_amounts.py --all-confirmed            # dry-run
    python scripts/backfill_day_amounts.py --all-confirmed --store <uuid> --apply

기본은 dry-run (backfill_store_codes.py 와 같은 관례) — 실제 반영은 --apply.
스키마 마이그레이션이 아닌 '데이터 작업'이라 서버 재시작이 필요 없다.
"""

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import UUID

# `python scripts/backfill_day_amounts.py` 로 직접 실행해도 app 패키지를 찾도록
# 서버 루트를 경로에 넣는다 (python -m scripts.backfill_day_amounts 도 동작).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings  # noqa: E402
from app.services.payroll_backfill_service import (  # noqa: E402
    payroll_backfill_service,
)


def _print_result(result: dict) -> None:
    header = (
        f"  {result['start_date']} ~ {result['end_date']} "
        f"({result['period_id']})"
    )
    print(header)
    print(
        f"    updated={result['updated']} "
        f"unchanged={result['unchanged']} "
        f"skipped={len(result['skipped'])}"
    )
    for skip in result["skipped"]:
        print(f"    [skip] {skip['member_name']}: {skip['reason']}")


async def main(
    period_id: UUID | None, all_confirmed: bool, store_id: UUID | None, apply: bool
) -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    results: list[dict] = []
    async with Session() as db:
        if all_confirmed:
            results = await payroll_backfill_service.backfill_all_confirmed(
                db, store_id=store_id
            )
        else:
            results = [
                await payroll_backfill_service.backfill_frozen_day_amounts(
                    db, period_id
                )
            ]
        if apply:
            await db.commit()
        else:
            await db.rollback()
    await engine.dispose()

    for result in results:
        _print_result(result)
    updated = sum(r["updated"] for r in results)
    skipped = sum(len(r["skipped"]) for r in results)
    unchanged = sum(r["unchanged"] for r in results)
    mode = "APPLIED" if apply else "DRY-RUN (nothing written; pass --apply)"
    print(
        f"\n{mode}: {len(results)} period(s), {updated} entry(ies) updated, "
        f"{unchanged} already had amounts, {skipped} skipped."
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Backfill daily amounts into frozen payroll entries"
    )
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--period", help="pay period UUID")
    target.add_argument(
        "--all-confirmed", action="store_true", help="every confirmed period"
    )
    p.add_argument("--store", help="limit --all-confirmed to one store UUID")
    p.add_argument(
        "--apply", action="store_true", help="write changes (default: dry-run)"
    )
    args = p.parse_args()
    asyncio.run(
        main(
            UUID(args.period) if args.period else None,
            args.all_confirmed,
            UUID(args.store) if args.store else None,
            args.apply,
        )
    )
    sys.exit(0)
