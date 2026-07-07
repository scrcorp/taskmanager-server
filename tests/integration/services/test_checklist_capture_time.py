"""Integration — checklist 파일 저장을 files(레지스트리) + file_usages(중앙 usage)로.

대상: complete_item / resubmit / upsert_review / add_review_content / delete_review_content
      / replace_for_new_work_role + file_service.gc_orphan_files

검증:
- 사진 메타(capture_time/source)는 files.metadata 에만 저장
- 같은 path 재업로드 = files 1행 재사용(복사 X) + file_usages 여러 개
- usage 삭제는 file_usages 행만 지움(blob 안 건드림) → 고아 files 는 GC 가 회수
- 응답 계약(키 집합 + capture_time/capture_source/received_at) 유지
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, exists, select

from app.config import settings
from app.models.checklist import ChecklistInstance, ChecklistInstanceItem
from app.models.file import File, FileUsage
from app.schemas.common import PhotoMeta
from app.services.checklist_instance_service import checklist_instance_service
from app.services.file_service import file_service
from app.utils.exceptions import CaptureTimeRequiredError

pytestmark = pytest.mark.asyncio

CT = datetime(2026, 6, 22, 9, 30, tzinfo=timezone.utc)

# build_detail_response files[] 응답 계약 — 이 키 집합이 바뀌면 콘솔/앱이 깨진다.
EXPECTED_FILE_KEYS = {
    "id", "context", "context_id", "file_url", "thumb_url",
    "file_type", "sort_order", "capture_time", "capture_source", "received_at",
}


@pytest_asyncio.fixture
async def photo_instance(db, test_user, test_store_id):
    """photo-verification 항목 1개를 가진 체크리스트 인스턴스 (테스트 셋업)."""
    inst = ChecklistInstance(
        organization_id=test_user["organization_id"],
        store_id=test_store_id,
        user_id=test_user["id"],
        work_date=date(2026, 6, 22),
        total_items=1,
        completed_items=0,
        status="pending",
    )
    db.add(inst)
    await db.flush()
    item = ChecklistInstanceItem(
        instance_id=inst.id,
        item_index=0,
        title="Photo proof",
        verification_type="photo",
        min_photos=1,
        sort_order=0,
    )
    db.add(item)
    await db.commit()
    inst_id = inst.id
    yield inst_id
    # teardown: 이 instance 의 file_usages 삭제 → 고아 files 정리 → instance 삭제.
    item_ids = (
        await db.execute(
            select(ChecklistInstanceItem.id).where(ChecklistInstanceItem.instance_id == inst_id)
        )
    ).scalars().all()
    if item_ids:
        await db.execute(delete(FileUsage).where(FileUsage.owner_id.in_(item_ids)))
    await db.execute(
        delete(File).where(~exists(select(FileUsage.id).where(FileUsage.file_id == File.id)))
    )
    await db.execute(delete(ChecklistInstance).where(ChecklistInstance.id == inst_id))
    await db.commit()


async def _usages_for(db, instance_id: UUID) -> list[FileUsage]:
    """인스턴스의 file_usages (owner_type='cl_item'). u.file 은 selectin 로딩."""
    rows = await db.execute(
        select(FileUsage)
        .join(ChecklistInstanceItem, ChecklistInstanceItem.id == FileUsage.owner_id)
        .where(FileUsage.owner_type == "cl_item", ChecklistInstanceItem.instance_id == instance_id)
    )
    return list(rows.scalars().all())


async def _distinct_files_for(db, instance_id: UUID) -> list[File]:
    """인스턴스 usage 가 가리키는 **distinct** files (재사용 확인용)."""
    rows = await db.execute(
        select(File)
        .distinct()
        .join(FileUsage, FileUsage.file_id == File.id)
        .join(ChecklistInstanceItem, ChecklistInstanceItem.id == FileUsage.owner_id)
        .where(FileUsage.owner_type == "cl_item", ChecklistInstanceItem.instance_id == instance_id)
    )
    return list(rows.scalars().all())


# ── 쓰기: files 메타 + usage 생성 ──────────────────────────────────────


async def test_photos_meta_stored_in_files_metadata(db, photo_instance, test_user):
    """photos 메타 경로 — capture_time/source 가 files.metadata 에 저장된다."""
    await checklist_instance_service.complete_item(
        db, instance_id=photo_instance, item_index=0, user_id=test_user["id"],
        photos=[PhotoMeta(key="completions/2026/06/22/a.webp", capture_time=CT, capture_source="live")],
    )
    usages = await _usages_for(db, photo_instance)
    assert len(usages) == 1
    assert usages[0].file.file_metadata == {"captured_at": CT.isoformat(), "capture_source": "live"}


async def test_complete_item_creates_file_and_usage(db, photo_instance, test_user):
    """complete_item 은 files 행 + file_usages(owner_type='cl_item', context='submission')를 만든다."""
    await checklist_instance_service.complete_item(
        db, instance_id=photo_instance, item_index=0, user_id=test_user["id"],
        photos=[PhotoMeta(key="completions/2026/06/22/a.webp", capture_time=CT, capture_source="live")],
    )
    usages = await _usages_for(db, photo_instance)
    assert len(usages) == 1
    u = usages[0]
    assert u.owner_type == "cl_item"
    assert u.context == "submission"
    f = u.file
    assert f.path  # 상대경로 key
    assert f.status == "active"
    assert f.file_type == "photo"
    assert f.organization_id == test_user["organization_id"]


async def test_legacy_photo_urls_flagged_unknown(db, photo_instance, test_user):
    """legacy photo_urls 경로 — metadata={"captured_at":None,"capture_source":"unknown"}."""
    await checklist_instance_service.complete_item(
        db, instance_id=photo_instance, item_index=0, user_id=test_user["id"],
        photo_urls=["completions/2026/06/22/legacy.jpg"],
    )
    usages = await _usages_for(db, photo_instance)
    assert len(usages) == 1
    assert usages[0].file.file_metadata == {"captured_at": None, "capture_source": "unknown"}


async def test_chat_photo_metadata_null(db, photo_instance, test_user):
    """chat 사진 — 촬영메타 개념 없음 → files.metadata NULL, usage context='chat'."""
    await checklist_instance_service.add_review_content(
        db, instance_id=photo_instance, item_index=0, author_id=test_user["id"],
        content_type="photo", content="checklists/chat/x.jpg",
    )
    usages = await _usages_for(db, photo_instance)
    assert len(usages) == 1
    assert usages[0].context == "chat"
    assert usages[0].file.file_metadata is None


# ── 재사용: 같은 path = 1 files 행 + 여러 usage (복사 X) ─────────────────


async def test_reuse_same_path_one_file_two_usages(db, photo_instance, test_user):
    """같은 사진(path)을 재제출로 재사용 → files 1행, file_usages 2개 (blob 복사 없음)."""
    key = "completions/2026/06/22/reuse.webp"
    await checklist_instance_service.complete_item(
        db, instance_id=photo_instance, item_index=0, user_id=test_user["id"],
        photos=[PhotoMeta(key=key, capture_time=CT, capture_source="live")],
    )
    await checklist_instance_service.resubmit_completion(
        db, instance_id=photo_instance, item_index=0, user_id=test_user["id"],
        photos=[PhotoMeta(key=key, capture_time=CT, capture_source="live")],
    )
    usages = await _usages_for(db, photo_instance)
    files = await _distinct_files_for(db, photo_instance)
    assert len(usages) == 2   # 제출 v1 + v2 = usage 2개
    assert len(files) == 1    # 같은 blob = files 1행 (재사용)


async def test_resubmit_new_photo_two_files(db, photo_instance, test_user):
    """재제출에 다른 사진 → files 2행, usage 2개 (이전 증거 보존)."""
    await checklist_instance_service.complete_item(
        db, instance_id=photo_instance, item_index=0, user_id=test_user["id"],
        photos=[PhotoMeta(key="completions/2026/06/22/v1.webp", capture_time=CT, capture_source="live")],
    )
    await checklist_instance_service.resubmit_completion(
        db, instance_id=photo_instance, item_index=0, user_id=test_user["id"],
        photos=[PhotoMeta(key="completions/2026/06/22/v2.webp", capture_time=CT, capture_source="live")],
    )
    assert len(await _usages_for(db, photo_instance)) == 2
    assert len(await _distinct_files_for(db, photo_instance)) == 2


async def test_upsert_review_creates_review_usage(db, photo_instance, test_user):
    """리뷰 피드백 사진이 context='review' usage 로 저장된다."""
    await checklist_instance_service.complete_item(
        db, instance_id=photo_instance, item_index=0, user_id=test_user["id"],
        photos=[PhotoMeta(key="completions/2026/06/22/a.webp", capture_time=CT, capture_source="live")],
    )
    await checklist_instance_service.upsert_review(
        db, instance_id=photo_instance, item_index=0, reviewer_id=test_user["id"],
        result="fail", comment_text="redo", comment_photo_url="reviews/2026/06/22/r.webp",
    )
    review = [u for u in await _usages_for(db, photo_instance) if u.context == "review"]
    assert len(review) == 1
    assert review[0].file is not None


# ── 게이트 ─────────────────────────────────────────────────────────────


async def test_require_capture_time_rejects_missing(db, photo_instance, test_user, monkeypatch):
    """REQUIRE_CAPTURE_TIME=True 면 capture_time 없는 사진은 422 거부, usage 0."""
    monkeypatch.setattr(settings, "REQUIRE_CAPTURE_TIME", True)
    with pytest.raises(CaptureTimeRequiredError) as exc:
        await checklist_instance_service.complete_item(
            db, instance_id=photo_instance, item_index=0, user_id=test_user["id"],
            photo_urls=["completions/2026/06/22/legacy.jpg"],
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "CAPTURE_TIME_REQUIRED"
    assert await _usages_for(db, photo_instance) == []


async def test_require_capture_time_accepts_with_time(db, photo_instance, test_user, monkeypatch):
    """REQUIRE_CAPTURE_TIME=True 라도 capture_time 있으면 정상 저장."""
    monkeypatch.setattr(settings, "REQUIRE_CAPTURE_TIME", True)
    await checklist_instance_service.complete_item(
        db, instance_id=photo_instance, item_index=0, user_id=test_user["id"],
        photos=[PhotoMeta(key="completions/2026/06/22/ok.webp", capture_time=CT, capture_source="gallery")],
    )
    usages = await _usages_for(db, photo_instance)
    assert len(usages) == 1
    assert usages[0].file.file_metadata["capture_source"] == "gallery"


# ── 응답 계약 ───────────────────────────────────────────────────────────


async def test_detail_response_surfaces_capture_meta(db, photo_instance, test_user):
    """상세 응답 files 계약 유지 — 키 집합 + capture_time/source/received_at."""
    await checklist_instance_service.complete_item(
        db, instance_id=photo_instance, item_index=0, user_id=test_user["id"],
        photos=[PhotoMeta(key="completions/2026/06/22/a.webp", capture_time=CT, capture_source="live")],
    )
    inst = await checklist_instance_service.get_instance(db, photo_instance)
    detail = await checklist_instance_service.build_detail_response(db, inst)
    files = detail["items"][0]["files"]
    assert len(files) == 1
    f = files[0]
    assert set(f.keys()) == EXPECTED_FILE_KEYS
    assert f["capture_source"] == "live"
    assert f["capture_time"] == CT.isoformat()
    assert f["received_at"] is not None
    assert f["thumb_url"] == f["file_url"]  # thumb 파생 부재 → base 폴백


async def test_detail_response_includes_store_timezone(db, photo_instance):
    """상세/요약 응답에 store→org→default 로 해석한 timezone 노출."""
    inst = await checklist_instance_service.get_instance(db, photo_instance)
    detail = await checklist_instance_service.build_detail_response(db, inst)
    assert detail["timezone"] == "UTC"
    summary = await checklist_instance_service.build_response(db, inst)
    assert summary["timezone"] == "UTC"


# ── 삭제 = usage 한 줄, blob 은 GC ──────────────────────────────────────


async def test_delete_chat_usage_keeps_file_blob_untouched(db, photo_instance, test_user, monkeypatch):
    """채팅 삭제 = file_usages 행만 삭제. files 행/blob 은 그대로(GC 가 회수)."""
    from app.services import checklist_instance_service as svc_mod

    deleted: list[str] = []
    monkeypatch.setattr(svc_mod.storage_service, "delete_file", lambda k: deleted.append(k) or True)
    msg = await checklist_instance_service.add_review_content(
        db, instance_id=photo_instance, item_index=0, author_id=test_user["id"],
        content_type="photo", content="checklists/chat/del.jpg",
    )
    usages = await _usages_for(db, photo_instance)
    assert len(usages) == 1
    file_id = usages[0].file_id

    await checklist_instance_service.delete_review_content(db, content_id=msg.id)
    # usage 사라짐
    assert await _usages_for(db, photo_instance) == []
    # files 행은 그대로 (삭제 경로가 blob 을 안 건드림)
    assert (await db.get(File, file_id)) is not None
    assert deleted == []  # delete_file 호출 안 됨


async def test_gc_reclaims_orphan_files_only(db, test_user, test_store_id, monkeypatch):
    """GC: usage 없는 files 만 blob+행 회수, usage 있는 files 는 보존."""
    from app.services import file_service as fs_mod

    deleted: list[str] = []
    monkeypatch.setattr(fs_mod.storage_service, "delete_file", lambda k: deleted.append(k) or True)

    orphan = File(path="completions/2026/06/22/orphan.webp", file_type="photo", status="active",
                  organization_id=test_user["organization_id"], store_id=test_store_id)
    used = File(path="completions/2026/06/22/used.webp", file_type="photo", status="active",
                organization_id=test_user["organization_id"], store_id=test_store_id)
    db.add_all([orphan, used])
    await db.flush()
    db.add(FileUsage(file_id=used.id, owner_type="cl_item", owner_id=uuid4(), context="submission"))
    await db.flush()

    n = await file_service.gc_orphan_files(db)
    assert n >= 1
    assert (await db.get(File, orphan.id)) is None        # 고아 회수됨
    assert "completions/2026/06/22/orphan.webp" in deleted  # blob 삭제됨
    assert (await db.get(File, used.id)) is not None        # usage 있는 건 보존
    assert "completions/2026/06/22/used.webp" not in deleted
    await db.rollback()


async def test_replace_for_new_work_role_cleans_usages(db, photo_instance, test_user, test_store_id):
    """work_role 교체 시 instance 의 file_usages 가 정리된다(blob 은 GC)."""
    await checklist_instance_service.complete_item(
        db, instance_id=photo_instance, item_index=0, user_id=test_user["id"],
        photos=[PhotoMeta(key="completions/2026/06/22/swap.webp", capture_time=CT, capture_source="live")],
    )
    assert len(await _usages_for(db, photo_instance)) == 1
    inst = await db.get(ChecklistInstance, photo_instance)
    await checklist_instance_service.replace_for_new_work_role(
        db, instance=inst, schedule_id=uuid4(),
        organization_id=test_user["organization_id"], store_id=test_store_id,
        user_id=test_user["id"], work_date=date(2026, 6, 22), new_work_role_id=None,
    )
    await db.commit()
    # 이 instance 의 item 들이 삭제됐으니 usage 도 없어야 함
    assert await _usages_for(db, photo_instance) == []


async def test_purge_cl_item_file_usages_removes_usages_keeps_file(db, photo_instance, test_user):
    """purge 헬퍼(F3 수정): instance 의 file_usages 만 삭제, files 행은 남김(GC 회수 대상).

    instance/item 하드삭제 경로(bulk_assign_checklist remove/replace 등)가 이 헬퍼를 호출해
    orphan usage 를 방지한다. owner_id 폴리모픽이라 DB cascade 가 안 되므로 명시 정리가 맞다.
    """
    from app.services.checklist_instance_service import purge_cl_item_file_usages

    await checklist_instance_service.complete_item(
        db, instance_id=photo_instance, item_index=0, user_id=test_user["id"],
        photos=[PhotoMeta(key="completions/2026/06/22/purge.webp", capture_time=CT, capture_source="live")],
    )
    usages = await _usages_for(db, photo_instance)
    assert len(usages) == 1
    file_id = usages[0].file_id

    await purge_cl_item_file_usages(db, photo_instance)
    await db.flush()

    assert await _usages_for(db, photo_instance) == []        # usage 정리됨
    assert (await db.get(File, file_id)) is not None           # files 행은 남음(GC 대상)
