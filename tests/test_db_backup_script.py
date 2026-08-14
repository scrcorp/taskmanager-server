"""scripts/db-backup.sh 의 가드 경로 회귀 테스트.

DB/S3 접속이 필요 없는 분기만 검증한다. 덤프·업로드 본체는 실제 DB/버킷이
있어야 하므로 여기서 다루지 않는다(수동 검증: `./scripts/db-backup.sh x --no-upload`).

이 가드들이 중요한 이유: 이 스크립트는 배포 워크플로에서 fail-closed 로 호출된다.
설정이 빠졌을 때 조용히 성공하면 백업 없이 마이그레이션이 돌아버린다.
"""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "db-backup.sh"

# .env 를 읽어 들여 테스트가 로컬 설정에 좌우되지 않도록 존재하지 않는 경로를 준다
NO_ENV = ["--env-file", "/nonexistent/.env"]


def run(args, env=None):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **(env or {})},
    )


def test_script_exists_and_is_executable():
    assert SCRIPT.exists()
    assert SCRIPT.stat().st_mode & 0o111, "실행 권한이 없으면 배포 스크립트에서 못 부른다"


def test_syntax_is_valid():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_skip_flag_exits_success_without_touching_db():
    """긴급 탈출구. 배포를 막지 않도록 0 으로 끝나야 한다."""
    r = run(["t", *NO_ENV], env={"SKIP_DB_BACKUP": "1"})
    assert r.returncode == 0
    assert "SKIP_DB_BACKUP" in r.stdout


def test_missing_database_url_fails():
    r = run(["t", *NO_ENV])
    assert r.returncode != 0
    assert "DATABASE_URL" in r.stderr


def test_missing_bucket_fails_before_dumping():
    """버킷이 없으면 덤프를 뜨기 전에 멈춰야 한다 (뜨고 나서 못 올리면 낭비)."""
    r = run(["t", *NO_ENV], env={"DATABASE_URL": "postgresql://u:p@h:5432/d"})
    assert r.returncode != 0
    assert "DB_BACKUP_S3_BUCKET" in r.stderr
    assert "덤프 시작" not in r.stdout


def test_no_upload_does_not_require_bucket():
    """--no-upload 는 버킷 없이도 통과해야 한다(로컬 검증용).

    버킷 검사를 지나 DB 접속 단계까지 갔는지로 확인한다.
    """
    r = run(["t", "--no-upload", *NO_ENV], env={"DATABASE_URL": "postgresql://u:p@127.0.0.1:1/d"})
    assert "DB_BACKUP_S3_BUCKET" not in r.stderr


@pytest.mark.parametrize("url", ["mysql://a/b", "sqlite:///x.db", "not-a-url"])
def test_non_postgres_url_rejected(url):
    r = run(["t", *NO_ENV], env={"DATABASE_URL": url, "DB_BACKUP_S3_BUCKET": "b"})
    assert r.returncode != 0
    assert "형식" in r.stderr


def test_asyncpg_driver_suffix_is_accepted():
    """SQLAlchemy URL(postgresql+asyncpg://)을 그대로 넣어도 통과해야 한다."""
    r = run(
        ["t", *NO_ENV],
        env={
            "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/d",
            "DB_BACKUP_S3_BUCKET": "b",
        },
    )
    # 형식 오류로 죽으면 안 된다 — 접속 단계까지는 가야 한다
    assert "형식을 인식할 수 없다" not in r.stderr


def test_unreachable_db_fails_with_actionable_message():
    r = run(
        ["t", *NO_ENV],
        env={
            "DATABASE_URL": "postgresql://u:p@127.0.0.1:1/d",
            "DB_BACKUP_S3_BUCKET": "b",
        },
    )
    assert r.returncode != 0
    # pg 클라이언트가 아예 없는 환경이면 그 사유로, 있으면 접속 실패로 죽는다
    assert ("DB 접속 실패" in r.stderr) or ("pg_dump 도 docker 도 없다" in r.stderr)


def prune_select(lines, cutoff="2026-08-01", keep_min=7):
    """스크립트의 삭제 대상 선별 로직을 직접 호출한다."""
    r = subprocess.run(
        ["bash", str(SCRIPT), "--prune-select", cutoff, str(keep_min)],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    return [k for k in r.stdout.splitlines() if k]


def make(n, day_start=1, prefix="k"):
    return [f"2026-07-{day_start + i:02d}\t{prefix}{i}.dump" for i in range(n)]


def test_prune_selects_only_objects_older_than_cutoff():
    lines = ["2026-07-01\told.dump", "2026-08-01\tsame.dump", "2026-08-05\tnew.dump"]
    # keep_min=0 이어야 나이 조건만 본다
    assert prune_select(lines, keep_min=0) == ["old.dump"]


def test_prune_keeps_minimum_count_even_when_all_are_old():
    """전부 오래됐어도 keep_min 개는 남아야 한다."""
    got = prune_select(make(10), cutoff="2026-12-31", keep_min=7)
    assert len(got) == 3
    # 오래된 것부터 지운다
    assert got == ["k0.dump", "k1.dump", "k2.dump"]


def test_prune_deletes_nothing_when_at_or_below_keep_min():
    assert prune_select(make(7), cutoff="2026-12-31", keep_min=7) == []
    assert prune_select(make(3), cutoff="2026-12-31", keep_min=7) == []


def test_prune_handles_empty_and_malformed_input():
    assert prune_select([""], keep_min=0) == []
    # 탭이 없는 줄, aws 가 결과 없을 때 뱉는 "None" 등은 무시돼야 한다
    assert prune_select(["None", "garbage"], cutoff="2026-12-31", keep_min=0) == []


def test_prune_is_skipped_when_retention_not_configured():
    """보존일수를 안 정했으면 아무것도 지우지 않는다(기본값이 안전한 쪽)."""
    script = SCRIPT.read_text()
    assert 'if [ -z "$RETENTION_DAYS" ]; then' in script
    # 정리 블록은 업로드 검증(크기 대조) 뒤에 있어야 한다
    assert script.index("업로드 크기 불일치") < script.index("prune_old_backups")


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"


def test_deploy_workflow_calls_backup_before_starting_containers():
    """배포 워크플로에서 백업이 컨테이너 기동(=alembic 실행)보다 앞서야 의미가 있다."""
    wf = (WORKFLOWS / "deploy-prod.yml").read_text()
    assert "db-backup.sh" in wf
    assert "script_stop: true" in wf, "이게 없으면 백업이 실패해도 배포가 계속된다"
    assert wf.index("db-backup.sh") < wf.index("docker compose up")


def test_docker_prune_runs_before_the_dump():
    """공간을 비운 뒤에 덤프를 떠야 디스크 부족으로 실패하지 않는다."""
    wf = (WORKFLOWS / "deploy-prod.yml").read_text()
    assert wf.index("docker image prune") < wf.index("db-backup.sh")
    assert wf.index("docker builder prune") < wf.index("db-backup.sh")


@pytest.mark.parametrize("name", ["deploy-prod.yml", "deploy-staging.yml"])
def test_prune_never_touches_volumes_or_all_images(name):
    """`--volumes` 는 compose 의 uploads 볼륨을 지운다 = 업로드 파일 유실.

    `-a` 는 사용 중이 아닌 이미지까지 날려 매 배포 재다운로드를 유발한다.
    둘 다 실수로 붙기 쉬운 플래그라 테스트로 막아둔다.
    """
    wf = (WORKFLOWS / name).read_text()
    for line in wf.splitlines():
        if "prune" in line and not line.strip().startswith("#"):
            assert "--volumes" not in line, f"위험: {line.strip()}"
            assert " -a" not in line, f"과도한 정리: {line.strip()}"
