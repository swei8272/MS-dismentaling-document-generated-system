from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from database import (
    attach_uploaded_evidence,
    claim_next_queued_evidence,
    connect_database,
    create_batch,
    get_batch_with_stats,
    list_batches,
    mark_evidence_completed,
    mark_evidence_failed,
    mark_evidence_pending,
    migrate_database,
    requeue_stale_processing_jobs,
)


def add_evidence(database_path: Path, batch_id: int, suffix: str) -> int:
    evidence_id, _, _ = attach_uploaded_evidence(
        database_path,
        batch_id=batch_id,
        sha256=suffix * 64,
        stored_path=f"{suffix}/{suffix}.png",
        media_type="image/png",
        size_bytes=10,
        original_name=f"{suffix}.png",
    )
    return evidence_id


def test_daily_batch_numbers_increment_and_blank_name_uses_number(database_path: Path) -> None:
    migrate_database(database_path)
    day = date(2026, 9, 5)
    first = create_batch(database_path, "", batch_date=day)
    second = create_batch(database_path, "下午车辆", batch_date=day)
    assert first["batch_no"] == "20260905-001"
    assert first["name"] == first["batch_no"]
    assert second["batch_no"] == "20260905-002"
    with connect_database(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(DISTINCT batch_no) FROM batches"
        ).fetchone()[0] == 2


def test_claim_is_atomic_and_same_task_cannot_be_claimed_twice(database_path: Path) -> None:
    migrate_database(database_path)
    batch = create_batch(database_path)
    evidence_id = add_evidence(database_path, batch["id"], "a")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda _: claim_next_queued_evidence(database_path), range(2))
        )

    claimed = [item for item in results if item is not None]
    assert len(claimed) == 1
    assert claimed[0]["id"] == evidence_id
    assert claimed[0]["attempt_count"] == 1


def test_completed_pending_and_failed_states_update_batch_counts(database_path: Path) -> None:
    migrate_database(database_path)
    batch = create_batch(database_path)
    ids = [add_evidence(database_path, batch["id"], suffix) for suffix in "abc"]

    first = claim_next_queued_evidence(database_path)
    assert first["id"] == ids[0]
    mark_evidence_completed(database_path, ids[0], attempt_count=first["attempt_count"])
    second = claim_next_queued_evidence(database_path)
    assert second["id"] == ids[1]
    mark_evidence_pending(database_path, ids[1], "等待人工归档", attempt_count=second["attempt_count"])
    third = claim_next_queued_evidence(database_path)
    assert third["id"] == ids[2]
    mark_evidence_failed(database_path, ids[2], "虚构的处理错误", attempt_count=third["attempt_count"])

    stats = get_batch_with_stats(database_path, batch["id"])
    assert (stats["completed"], stats["pending"], stats["failed"]) == (1, 1, 1)
    assert stats["status"] == "partial_failed"
    with connect_database(database_path) as connection:
        failed = connection.execute(
            "SELECT error_message FROM evidence WHERE id = ?", (ids[2],)
        ).fetchone()[0]
    assert failed == "虚构的处理错误"


def test_stale_processing_job_is_requeued(database_path: Path) -> None:
    migrate_database(database_path)
    batch = create_batch(database_path)
    evidence_id = add_evidence(database_path, batch["id"], "d")
    claim_next_queued_evidence(database_path)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    with connect_database(database_path) as connection:
        connection.execute(
            "UPDATE evidence SET locked_at = ? WHERE id = ?", (old, evidence_id)
        )
        connection.commit()

    assert requeue_stale_processing_jobs(database_path, timeout_seconds=60) == 1
    assert requeue_stale_processing_jobs(database_path, timeout_seconds=60) == 0
    with connect_database(database_path) as connection:
        row = connection.execute(
            "SELECT processing_status, attempt_count FROM evidence WHERE id = ?",
            (evidence_id,),
        ).fetchone()
    assert tuple(row) == ("queued", 1)


def test_batches_and_tasks_persist_across_connections(database_path: Path) -> None:
    migrate_database(database_path)
    batch = create_batch(database_path, "持久化测试")
    add_evidence(database_path, batch["id"], "e")

    migrate_database(database_path)
    batches = list_batches(database_path)
    assert len(batches) == 1
    assert batches[0]["name"] == "持久化测试"
    assert batches[0]["queued"] == 1


@pytest.mark.parametrize("finish,args,expected_status", [
    (mark_evidence_completed, (), "completed"),
    (mark_evidence_pending, ("等待人工确认",), "pending"),
    (mark_evidence_failed, ("处理失败",), "failed"),
])
def test_expired_claim_cannot_finish_reassigned_task(
    database_path: Path, finish, args, expected_status: str
) -> None:
    migrate_database(database_path)
    first_batch = create_batch(database_path)
    second_batch = create_batch(database_path)
    evidence_id = add_evidence(database_path, first_batch["id"], "a")
    assert add_evidence(database_path, second_batch["id"], "a") == evidence_id
    worker_a = claim_next_queued_evidence(database_path)
    with connect_database(database_path) as connection:
        connection.execute(
            "UPDATE evidence SET locked_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", evidence_id),
        )
        connection.commit()
    assert requeue_stale_processing_jobs(database_path) == 1

    # Reject late reports both before and after another worker claims the job.
    with pytest.raises(ValueError, match="领取已过期"):
        finish(database_path, evidence_id, *args, attempt_count=worker_a["attempt_count"])
    worker_b = claim_next_queued_evidence(database_path)
    assert worker_b["attempt_count"] == worker_a["attempt_count"] + 1
    migrate_database(database_path)  # Restart/migration must not reset the fence.
    before = [dict(get_batch_with_stats(database_path, b["id"])) for b in (first_batch, second_batch)]
    with pytest.raises(ValueError, match="领取已过期"):
        finish(database_path, evidence_id, *args, attempt_count=worker_a["attempt_count"])
    with connect_database(database_path) as connection:
        row = connection.execute("SELECT * FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
        assert dict(row) == worker_b
    assert [dict(get_batch_with_stats(database_path, b["id"])) for b in (first_batch, second_batch)] == before

    finish(database_path, evidence_id, *args, attempt_count=worker_b["attempt_count"])
    for batch in (first_batch, second_batch):
        assert get_batch_with_stats(database_path, batch["id"])[expected_status] == 1
    with pytest.raises(ValueError):
        finish(database_path, evidence_id, *args, attempt_count=worker_b["attempt_count"])


@pytest.mark.parametrize("attempt_count", [0, -1, None, True, "1", 1.0])
def test_invalid_claim_identity_is_rejected(database_path: Path, attempt_count) -> None:
    migrate_database(database_path)
    batch = create_batch(database_path)
    evidence_id = add_evidence(database_path, batch["id"], "b")
    claim = claim_next_queued_evidence(database_path)
    with pytest.raises(ValueError, match="正整数"):
        mark_evidence_completed(database_path, evidence_id, attempt_count=attempt_count)
    mark_evidence_completed(database_path, evidence_id, attempt_count=claim["attempt_count"])
