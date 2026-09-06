from pathlib import Path

from database import attach_uploaded_evidence, connect_database, create_batch, migrate_database
from worker import self_check


def test_worker_self_check_does_not_claim_queued_work(database_path: Path) -> None:
    migrate_database(database_path)
    batch = create_batch(database_path)
    attach_uploaded_evidence(
        database_path,
        batch_id=batch["id"],
        sha256="f" * 64,
        stored_path="ff/f.png",
        media_type="image/png",
        size_bytes=10,
        original_name="待处理.png",
    )
    assert self_check(database_path) == 0
    with connect_database(database_path) as connection:
        status = connection.execute("SELECT processing_status FROM evidence").fetchone()[0]
    assert status == "queued"
