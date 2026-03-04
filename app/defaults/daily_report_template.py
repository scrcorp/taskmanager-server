"""기본 일일 리포트 템플릿 정의.

조직 생성 시 이 정의를 기반으로 해당 조직의 기본 템플릿을 DB에 생성.
내용 수정 시 이 파일만 변경하면 신규 조직에 반영됨.
기존 조직의 템플릿은 Admin에서 개별 수정.
"""

DEFAULT_TEMPLATE_NAME = "Daily Report"

DEFAULT_SECTIONS = [
    {
        "title": "Sales & Revenue",
        "description": "Today's sales figures, transaction count, average ticket size, comparison to target",
        "sort_order": 1,
        "is_required": True,
    },
    {
        "title": "Staff & Operations",
        "description": "Staffing levels, attendance issues, notable performance, shift handoff notes",
        "sort_order": 2,
        "is_required": True,
    },
    {
        "title": "Customer Feedback",
        "description": "Customer complaints, compliments, special requests, service quality observations",
        "sort_order": 3,
        "is_required": False,
    },
    {
        "title": "Issues & Actions",
        "description": "Problems encountered, actions taken, unresolved issues requiring follow-up",
        "sort_order": 4,
        "is_required": True,
    },
    {
        "title": "Notes",
        "description": "Any other observations, reminders for next shift, upcoming events",
        "sort_order": 5,
        "is_required": False,
    },
]
