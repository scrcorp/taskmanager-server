"""시프트 프리셋 서비스 — Shift Preset CRUD 비즈니스 로직.

Shift Preset Service — Business logic for shift preset CRUD.
"""

from datetime import time
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import ShiftPreset
from app.repositories.shift_preset_repository import shift_preset_repository
from app.schemas.shift_preset import ShiftPresetCreate, ShiftPresetResponse, ShiftPresetUpdate
from app.utils.exceptions import NotFoundError


class ShiftPresetService:

    def _to_response(self, preset: ShiftPreset) -> ShiftPresetResponse:
        return ShiftPresetResponse(
            id=str(preset.id),
            store_id=str(preset.store_id),
            shift_id=str(preset.shift_id),
            name=preset.name,
            start_time=preset.start_time.strftime("%H:%M"),
            end_time=preset.end_time.strftime("%H:%M"),
            is_active=preset.is_active,
            sort_order=preset.sort_order,
            created_at=preset.created_at,
        )

    async def list_presets(
        self, db: AsyncSession, store_id: UUID
    ) -> list[ShiftPresetResponse]:
        presets = await shift_preset_repository.get_by_store(db, store_id)
        return [self._to_response(p) for p in presets]

    async def create_preset(
        self, db: AsyncSession, organization_id: UUID, store_id: UUID, data: ShiftPresetCreate
    ) -> ShiftPresetResponse:
        h1, m1 = map(int, data.start_time.split(":"))
        h2, m2 = map(int, data.end_time.split(":"))
        preset = await shift_preset_repository.create(db, {
            "organization_id": organization_id,
            "store_id": store_id,
            "shift_id": UUID(data.shift_id),
            "name": data.name,
            "start_time": time(h1, m1),
            "end_time": time(h2, m2),
            "sort_order": data.sort_order,
        })
        return self._to_response(preset)

    async def update_preset(
        self, db: AsyncSession, preset_id: UUID, organization_id: UUID, data: ShiftPresetUpdate
    ) -> ShiftPresetResponse:
        update_data = data.model_dump(exclude_unset=True)
        if "start_time" in update_data and update_data["start_time"]:
            h, m = map(int, update_data["start_time"].split(":"))
            update_data["start_time"] = time(h, m)
        if "end_time" in update_data and update_data["end_time"]:
            h, m = map(int, update_data["end_time"].split(":"))
            update_data["end_time"] = time(h, m)
        preset = await shift_preset_repository.update(db, preset_id, update_data, organization_id)
        if preset is None:
            raise NotFoundError("Shift preset not found")
        return self._to_response(preset)

    async def delete_preset(
        self, db: AsyncSession, preset_id: UUID, organization_id: UUID
    ) -> None:
        deleted = await shift_preset_repository.delete(db, preset_id, organization_id)
        if not deleted:
            raise NotFoundError("Shift preset not found")


shift_preset_service: ShiftPresetService = ShiftPresetService()
