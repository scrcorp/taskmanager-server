"""알림 카테고리 정의 + 사용자 선호도 헬퍼.

Alert category definitions + user preference helpers.

알림 type 값들을 사용자 친화적인 카테고리(6개)로 그룹화하여 admin/app
설정 UI에서 카테고리×채널(in-app/email) 격자로 토글할 수 있게 한다.

저장 형태 (users.alert_preferences JSONB):

    {
        "schedule":     {"in_app": true,  "email": false},
        "reply":        {"in_app": false, "email": true}
    }

미명시 카테고리/채널은 default = True. 사용자가 명시적으로 끈 것만 저장.
"""
from typing import Any, Optional


# 카테고리 정의 — id/label/email 가능 여부/포함 type 목록
# admin/app 클라이언트는 GET 응답으로 이 목록을 받아 그대로 렌더한다.
CATEGORIES: list[dict[str, Any]] = [
    {
        "code": "schedule",
        "label": "Schedule",
        "description": "Schedule submission, approval, and substitution",
        "types": ["schedule_pending", "schedule_approved", "schedule_assigned", "schedule_substitute"],
        "email_available": False,
    },
    {
        "code": "checklist",
        "label": "Checklist",
        "description": "Checklist completion and re-review",
        "types": ["checklist_submitted", "checklist_re_review"],
        "email_available": True,
    },
    {
        "code": "reply",
        "label": "Reply",
        "description": "Manager replies on your checklist or daily report",
        "types": ["reply"],
        "email_available": True,
    },
    {
        "code": "task",
        "label": "Additional Task",
        "description": "Tasks assigned to you",
        "types": ["additional_task"],
        "email_available": False,
    },
    {
        "code": "notice",
        "label": "Notice",
        "description": "Notices targeted at you",
        "types": ["notice"],
        "email_available": False,
    },
    {
        "code": "attendance",
        "label": "Attendance",
        "description": "Clock-in/out corrections",
        "types": ["attendance_corrected"],
        "email_available": False,
    },
    {
        "code": "warning",
        "label": "Warning",
        "description": "Disciplinary warnings issued to you",
        "types": ["warning"],
        "email_available": False,
    },
]

# 카테고리 코드 set — 빠른 검증용
CATEGORY_CODES: set[str] = {c["code"] for c in CATEGORIES}

# alert.type → category code 역인덱스
_TYPE_TO_CATEGORY: dict[str, str] = {
    t: c["code"] for c in CATEGORIES for t in c["types"]
}

# 카테고리별 email 가능 여부
_EMAIL_AVAILABLE: dict[str, bool] = {
    c["code"]: c["email_available"] for c in CATEGORIES
}


def category_for_type(alert_type: str) -> Optional[str]:
    """alert.type 으로 카테고리 코드 조회. 매핑 없으면 None."""
    return _TYPE_TO_CATEGORY.get(alert_type)


def email_available_for_category(category_code: str) -> bool:
    """해당 카테고리에 이메일 발송이 구현되어 있는지."""
    return _EMAIL_AVAILABLE.get(category_code, False)


def is_in_app_enabled(prefs: Optional[dict], category_code: str) -> bool:
    """preferences 에서 in-app 알림 활성화 여부. 미명시 = True."""
    if not prefs or not isinstance(prefs, dict):
        return True
    cat = prefs.get(category_code)
    if not isinstance(cat, dict):
        return True
    val = cat.get("in_app")
    return True if val is None else bool(val)


def is_email_enabled(prefs: Optional[dict], category_code: str) -> bool:
    """preferences 에서 이메일 활성화 여부. 미명시 = True (이메일 가능 카테고리 기준)."""
    if not prefs or not isinstance(prefs, dict):
        return True
    cat = prefs.get(category_code)
    if not isinstance(cat, dict):
        return True
    val = cat.get("email")
    return True if val is None else bool(val)


def is_in_app_enabled_for_type(prefs: Optional[dict], alert_type: str) -> bool:
    """type 으로 직접 in-app 활성화 여부 체크. 카테고리 매핑 없으면 True."""
    cat = category_for_type(alert_type)
    return True if cat is None else is_in_app_enabled(prefs, cat)


def is_email_enabled_for_type(prefs: Optional[dict], alert_type: str) -> bool:
    """type 으로 직접 email 활성화 여부 체크."""
    cat = category_for_type(alert_type)
    return True if cat is None else is_email_enabled(prefs, cat)


def normalize_preferences(raw: Any) -> dict[str, dict[str, bool]]:
    """클라이언트 입력 정규화 — 알 수 없는 카테고리/필드 제거.

    - CATEGORY_CODES 에 없는 키 제거
    - in_app/email 외 필드 제거
    - email_available=False 카테고리에 email=False 명시는 무의미하지만 허용
      (UI 일관성 위해 저장은 함)
    - True 인 값(default)도 명시 저장은 허용 (지우지 않음)
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, bool]] = {}
    for code, val in raw.items():
        if code not in CATEGORY_CODES or not isinstance(val, dict):
            continue
        cleaned: dict[str, bool] = {}
        if "in_app" in val:
            cleaned["in_app"] = bool(val["in_app"])
        if "email" in val:
            cleaned["email"] = bool(val["email"])
        if cleaned:
            out[code] = cleaned
    return out
