"""스토어 코드(stores.code) backfill — idempotent.

기존 스토어 이름이 "IFO - Il Fiora" 처럼 'CODE - Name' 패턴이면 code 를 채운다.
- code 가 이미 있는 스토어, 패턴에 안 맞는 스토어(예: CORP_ABR_3 공란 2곳)는 건드리지 않는다.
- 기본 dry-run. 실제 반영은 --apply.
- --clean-names 주면 이름에서 "CODE - " 접두를 제거(기본은 code 만 채우고 이름 유지).

사용:
    python scripts/backfill_store_codes.py            # dry-run (미리보기)
    python scripts/backfill_store_codes.py --apply     # 반영
    python scripts/backfill_store_codes.py --apply --clean-names

이 스크립트는 스키마 마이그레이션과 분리된 '데이터 작업'이다(prod 에서 검증하며 실행).
코드 없는 스토어(공란 2곳)는 콘솔 Store 편집 UI 에서 수동 부여한다.
"""
import argparse
import asyncio
import re
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.config import settings
from app.models.organization import Store

# "CODE - Name" — CODE 는 2~5 영숫자, 구분자 ' - '.
_PREFIX_RE = re.compile(r"^([A-Za-z0-9]{2,5})\s*-\s*(.+)$")


async def main(apply: bool, clean_names: bool) -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    changed = 0
    skipped = 0
    async with Session() as db:
        stores = (
            await db.execute(
                select(Store).where(Store.deleted_at.is_(None))
            )
        ).scalars().all()
        # org 내 code 중복 방지용 — 이미 쓰인 코드 집합.
        used: dict = {}
        for s in stores:
            if s.code:
                used.setdefault(s.organization_id, set()).add(s.code.upper())
        for s in stores:
            if s.code:
                continue
            m = _PREFIX_RE.match((s.name or "").strip())
            if not m:
                skipped += 1
                continue
            code = m.group(1).upper()
            org_used = used.setdefault(s.organization_id, set())
            if code in org_used:
                print(f"  [skip dup] {s.name!r} -> {code} (org 내 코드 중복)")
                skipped += 1
                continue
            org_used.add(code)
            new_name = m.group(2).strip() if clean_names else s.name
            action = f"code={code}" + (f", name={new_name!r}" if clean_names else "")
            print(f"  {s.name!r} -> {action}")
            if apply:
                s.code = code
                if clean_names:
                    s.name = new_name
            changed += 1
        if apply:
            await db.commit()
    await engine.dispose()
    mode = "APPLIED" if apply else "DRY-RUN (no changes written; pass --apply)"
    print(f"\n{mode}: {changed} store(s) to update, {skipped} skipped.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Backfill stores.code from 'CODE - Name' pattern")
    p.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    p.add_argument("--clean-names", action="store_true", help="also strip 'CODE - ' prefix from name")
    args = p.parse_args()
    asyncio.run(main(args.apply, args.clean_names))
    sys.exit(0)
