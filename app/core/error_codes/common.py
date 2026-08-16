"""여러 도메인이 **같은 뜻으로** 공유하는 코드.

전역 중복 검사(G4)가 처음 돌았을 때 걸린 것들이다 — `store_not_found` 가 가입 플로우와
매장 사진 업로드에서, `file_too_large`/`invalid_file_type` 이 지원서 첨부와 매장 사진에서
각각 리터럴로 쓰이고 있었다. 뜻이 같으므로 **여기 한 번만** 선언하고 각 도메인이 import 한다.

뜻이 다른데 이름만 같다면 여기 두면 안 된다 — 도메인별로 다른 이름을 붙여야 한다.
"""

from __future__ import annotations

from app.core.error_codes._registry import domain

COMMON = domain("common")

STORE_NOT_FOUND = COMMON.legacy(
    "store_not_found",
    404,
    "This store is not available.",
    frozen=True,
    clients=("console/src/types/signup.ts", "console/src/components/signup/InvalidLinkScreen.tsx"),
)

FORBIDDEN = COMMON.legacy(
    "forbidden",
    403,
    "You do not have permission to do this.",
)

INVALID_FILE_TYPE = COMMON.legacy(
    "invalid_file_type",
    400,
    "This file type is not supported.",
    hint="Use a JPG, PNG, or WebP image.",
)

FILE_TOO_LARGE = COMMON.legacy(
    "file_too_large",
    400,
    "This file is too large.",
    hint="Choose a smaller file and try again.",
)

FILE_NOT_FOUND = COMMON.legacy("file_not_found", 404, "This file is no longer available.")

INVALID_FOLDER = COMMON.legacy("invalid_folder", 400, "This upload location is not allowed.")

INVALID_KEY = COMMON.legacy("invalid_key", 400, "This file reference is not valid.")

GROUP_NOT_FOUND = COMMON.code(
    "GROUP_NOT_FOUND",
    404,
    "This store group no longer exists.",
)

INVALID_DATE_RANGE = COMMON.code(
    "INVALID_DATE_RANGE",
    400,
    "date_from must be on or before date_to.",
)

INVALID_STORE_IDS = COMMON.code(
    "INVALID_STORE_IDS",
    400,
    "store_ids must be comma-separated UUIDs.",
)

INVALID_STORE_OVERRIDES = COMMON.code(
    "INVALID_STORE_OVERRIDES",
    400,
    "store_overrides must be a JSON object of {label: store id}.",
)
