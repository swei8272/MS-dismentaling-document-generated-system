from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from database import UnsupportedSchemaError, connect_database, migrate_database


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
    } <= table_names(database_path)
    with connect_database(database_path) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert [
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations")
        ] == [1, 2]


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
    migrate_database(database_path)
    with connect_database(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM batches WHERE batch_no = ?", ("LEGACY-IMPORT",)
        ).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM batch_images").fetchone()[0] == 1


def test_phase_one_database_upgrades_to_phase_two_without_changing_queue_rows(
    database_path: Path,
) -> None:
    _create_known_legacy_database(database_path)
    migrate_database(database_path)
    with connect_database(database_path) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version = ?", (2,))
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
        ] == [1, 2]
        assert [dict(row) for row in connection.execute("SELECT * FROM evidence")] == before_evidence
        assert [dict(row) for row in connection.execute("SELECT * FROM batch_images")] == before_links
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("upload_failures",),
        ).fetchone()[0] == 1


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
