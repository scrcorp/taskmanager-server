"""Export 파일명에 쓸 스코프 라벨 — 매장 필터를 사람이 읽는 한 조각으로.

파일명 규칙 자체는 app/utils/download.export_filename 이 갖고 있고, 여기는
"그래서 이 파일은 어느 매장 것인가" 만 답한다. 여러 export 엔드포인트가 각자
매장 이름을 조회해 문자열을 조립하면 매장 1개/여러 개/전체의 표기가 금방
갈라지므로(=폴더에서 정렬이 안 됨) 한 곳에 모은다.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Store

# 매장 필터가 없을 때 — 조직명 대신 "전체" 를 쓴다. 받는 사람은 어차피 자기
# 조직 파일만 보므로 조직명은 정보가 아니고, 전체/일부 구분이 정보다.
ALL_STORES = "AllStores"


async def resolve_store_scope(
    db: AsyncSession,
    organization_id: UUID,
    store_ids: list[UUID] | set[UUID] | None,
) -> str:
    """매장 필터 → 파일명 조각.

    - 없음/전체 → `AllStores`
    - 1개       → 매장명 (한글 그대로 — filename* 로 전송된다)
    - 2개       → `매장A_매장B`
    - 3개 이상  → `매장A+2` (이름을 다 붙이면 파일명이 못 쓰게 길어진다)

    조회 못 한 id 는 무시한다 — 파일명 때문에 export 가 실패하면 안 된다.
    """
    ids = list(store_ids or [])
    if not ids:
        return ALL_STORES
    rows = (
        await db.execute(
            select(Store.name).where(
                Store.organization_id == organization_id, Store.id.in_(ids)
            )
        )
    ).scalars().all()
    # 정렬은 Python 에서 — DB collation(한글/ASCII 순서)이 환경마다 달라서
    # 같은 선택인데 파일명이 갈리면 안 된다.
    names = sorted(n for n in rows if n)
    if not names:
        return ALL_STORES
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]}_{names[1]}"
    return f"{names[0]}+{len(names) - 1}"
