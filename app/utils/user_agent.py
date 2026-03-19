"""User-Agent 파싱 유틸리티 — 기기명과 프로그램명을 추출합니다.

User-Agent parsing utility — Extracts device name and program name
from User-Agent header for session management display.
"""

from user_agents import parse as ua_parse


def parse_user_agent(ua_string: str | None) -> tuple[str, str]:
    """User-Agent 문자열을 파싱하여 (device_name, program) 튜플을 반환합니다.

    Parse User-Agent string into (device_name, program) tuple.

    Args:
        ua_string: User-Agent 헤더 값 (User-Agent header value, may be None)

    Returns:
        tuple[str, str]: (device_name, program) — e.g. ("Mac OS X 10.15", "Chrome 131")
    """
    if not ua_string:
        return ("Unknown", "Unknown")

    # Flutter/Dart detection
    if ua_string.startswith("Dart/"):
        dart_version = ua_string.split("/")[1].split(" ")[0] if "/" in ua_string else ""
        return ("Mobile", f"Flutter ({dart_version})" if dart_version else "Flutter")

    ua = ua_parse(ua_string)

    # Device name: OS family + version
    os_family = ua.os.family
    os_version = ua.os.version_string
    if os_family and os_family != "Other":
        device = f"{os_family} {os_version}".strip() if os_version else os_family
    else:
        device = "Unknown"

    # Program: browser family + major version
    browser_family = ua.browser.family
    browser_version = ua.browser.version_string
    if browser_family and browser_family != "Other":
        # Use only major.minor version for cleaner display
        version_parts = browser_version.split(".")
        short_version = ".".join(version_parts[:2]) if version_parts else ""
        program = f"{browser_family} {short_version}".strip() if short_version else browser_family
    else:
        program = "Unknown"

    return (device, program)
