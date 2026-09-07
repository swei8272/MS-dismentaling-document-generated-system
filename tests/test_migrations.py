from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from database import (
    UnsupportedSchemaError,
    UploadAssociationError,
    _apply_phase_one_migration,
    _apply_phase_two_migration,
    connect_database,
    create_batch,
    get_expected_upload_sha256,
    migrate_database,
    utc_now,
)


def table_names(path: Path) -> set[str]:
    with connect_database(path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


def test_fresh_database_initialization(database_path: Path) -> None:
    migrate_database(database_path)
    assert {
        "schema_migrations",
        "vehicles",
        "evidence",
        "conflicts",
        "batches",
        "batch_images",
        "upload_failures",
        "upload_receipts",
    } <= table_names(database_path)
    with connect_database(database_path) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert [
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations")
        ] == [1, 2, 3]
        assert "resolved_evidence_id" in {
            row["name"] for row in connection.execute("PRAGMA table_info(upload_failures)")
        }


def _create_known_legacy_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE vehicles (
            id INTEGER PRIMARY KEY,
            plate TEXT,
            vin TEXT,
            legacy_note TEXT
        );
        CREATE TABLE evidence (
            id INTEGER PRIMARY KEY,
            sha256 TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            original_name TEXT,
            vehicle_id INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE TABLE legacy_exports (
            id INTEGER PRIMARY KEY,
            filename TEXT NOT NULL
        );
        INSERT INTO vehicles VALUES (7, '测试A123', 'TESTVIN00000000001', '保留车辆');
        INSERT INTO evidence VALUES (
            9,
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'legacy/car.png',
            '旧图片.png',
            7,
            '2025-01-02T03:04:05+00:00'
        );
        INSERT INTO legacy_exports VALUES (3, '历史表.xlsx');
        """
    )
    connection.commit()
    connection.close()


def test_known_legacy_database_is_migrated_without_losing_rows(database_path: Path) -> None:
    _create_known_legacy_database(database_path)
    migrate_database(database_path)
    with connect_database(database_path) as connection:
        vehicle = connection.execute("SELECT * FROM vehicles WHERE id = ?", (7,)).fetchone()
        evidence = connection.execute("SELECT * FROM evidence WHERE id = ?", (9,)).fetchone()
        legacy_export = connection.execute(
            "SELECT * FROM legacy_exports WHERE id = ?", (3,)
        ).fetchone()
        batch = connection.execute(
            "SELECT * FROM batches WHERE batch_no = ?", ("LEGACY-IMPORT",)
        ).fetchone()
        link = connection.execute(
            "SELECT * FROM batch_images WHERE batch_id = ? AND evidence_id = ?",
            (batch["id"], 9),
        ).fetchone()
    assert vehicle["legacy_note"] == "保留车辆"
    assert evidence["vehicle_id"] == 7
    assert evidence["processing_status"] == "completed"
    assert legacy_export["filename"] == "历史表.xlsx"
    assert batch["status"] == "completed"
    assert link["original_name"] == "旧图片.png"


def test_migration_is_idempotent(database_path: Path) -> None:
    _create_known_legacy_database(database_path)
    migrate_database(database_path)
    with connect_database(database_path) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version = ?", (3,))
        connection.commit()
    migrate_database(database_path)
    migrate_database(database_path)
    with connect_database(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM batches WHERE batch_no = ?", ("LEGACY-IMPORT",)
        ).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM batch_images").fetchone()[0] == 1
        assert list(connection.execute("PRAGMA foreign_key_check")) == []


def test_phase_one_database_upgrades_through_phase_two_fixes_without_changing_queue_rows(
    database_path: Path,
) -> None:
    _create_known_legacy_database(database_path)
    migrate_database(database_path)
    with connect_database(database_path) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version IN (?, ?)", (2, 3))
        connection.execute("DROP TABLE upload_receipts")
        connection.execute("DROP TABLE upload_failures")
        before_evidence = [dict(row) for row in connection.execute("SELECT * FROM evidence")]
        before_links = [dict(row) for row in connection.execute("SELECT * FROM batch_images")]
        connection.commit()

    migrate_database(database_path)
    migrate_database(database_path)

    with connect_database(database_path) as connection:
        assert [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ] == [1, 2, 3]
        assert [dict(row) for row in connection.execute("SELECT * FROM evidence")] == before_evidence
        assert [dict(row) for row in connection.execute("SELECT * FROM batch_images")] == before_links
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("upload_failures",),
        ).fetchone()[0] == 1


def test_phase_two_database_upgrades_to_recovery_receipts_without_data_loss(
    database_path: Path,
) -> None:
    with connect_database(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        _apply_phase_one_migration(connection)
        _apply_phase_two_migration(connection)
        now = utc_now()
        connection.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            ((1, now), (2, now)),
        )
        connection.commit()

    batch = create_batch(database_path, "版本二保留测试")
    with connect_database(database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO upload_failures(
                batch_id, client_id, original_name, size_bytes, reason,
                retryable, status, created_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, 1, 'failed', ?, NULL)
            """,
            (
                batch["id"],
                "legacy-v2-client",
                "旧失败.jpg",
                123,
                "旧失败原因",
                utc_now(),
            ),
        )
        failure_id = cursor.lastrowid
        connection.execute(
            """
            INSERT INTO upload_failures(
                batch_id, client_id, original_name, size_bytes, reason,
                retryable, status, created_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, 1, 'resolved', ?, ?)
            """,
            (
                batch["id"],
                "legacy-v2-resolved",
                "旧已解决.jpg",
                456,
                "旧失败已解决",
                utc_now(),
                utc_now(),
            ),
        )
        connection.commit()

    migrate_database(database_path)
    migrate_database(database_path)

    with connect_database(database_path) as connection:
        assert [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ] == [1, 2, 3]
        failure = connection.execute(
            "SELECT * FROM upload_failures WHERE id = ?", (failure_id,)
        ).fetchone()
        assert failure["client_id"] == "legacy-v2-client"
        assert failure["reason"] == "旧失败原因"
        assert failure["status"] == "failed"
        assert failure["resolved_evidence_id"] is None
        resolved_failure = connection.execute(
            "SELECT * FROM upload_failures WHERE client_id = ?",
            ("legacy-v2-resolved",),
        ).fetchone()
        assert resolved_failure["status"] == "resolved"
        assert resolved_failure["resolved_evidence_id"] is None
        assert connection.execute("SELECT COUNT(*) FROM upload_receipts").fetchone()[0] == 0

    with pytest.raises(UploadAssociationError, match="升级前已经解决"):
        get_expected_upload_sha256(
            database_path,
            batch_id=batch["id"],
            client_id="legacy-v2-resolved",
            failure_id=resolved_failure["id"],
        )


def test_unknown_legacy_evidence_schema_stops_before_writing(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY, mystery TEXT)")
    connection.execute("INSERT INTO evidence(mystery) VALUES (?)", ("必须保留",))
    connection.commit()
    connection.close()

    with pytest.raises(UnsupportedSchemaError):
        migrate_database(database_path)

    connection = sqlite3.connect(database_path)
    assert connection.execute("SELECT mystery FROM evidence").fetchone()[0] == "必须保留"
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()[0] == 0
    connection.close()
