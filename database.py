from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


TASK_STATUSES = ("queued", "processing", "completed", "pending", "failed")
MIGRATION_VERSION = 1
BUSY_TIMEOUT_MS = 5_000


class UnsupportedSchemaError(RuntimeError):
    """Raised before migration writes when an existing schema is unknown."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_database(database_path: str | Path) -> sqlite3.Connection:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


@contextmanager
def database_connection(database_path: str | Path) -> Iterator[sqlite3.Connection]:
    connection = connect_database(database_path)
    try:
        yield connection
    finally:
        connection.close()


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?",
        ("table", table),
    ).fetchone()
    return row is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _validate_existing_schema(connection: sqlite3.Connection) -> None:
    """Refuse an unknown legacy evidence layout before applying any migration."""
    if not _table_exists(connection, "evidence"):
        return
    columns = _columns(connection, "evidence")
    required = {"id", "sha256", "stored_path", "created_at"}
    missing = sorted(required - columns)
    if missing:
        raise UnsupportedSchemaError(
            "现有 evidence 表结构未知，缺少必要字段：" + ", ".join(missing)
        )
    invalid = connection.execute(
        """
        SELECT id
        FROM evidence
        WHERE sha256 IS NULL OR sha256 = ''
           OR stored_path IS NULL OR stored_path = ''
        LIMIT 1
        """
    ).fetchone()
    duplicate = connection.execute(
        "SELECT sha256 FROM evidence GROUP BY sha256 HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    if invalid is not None or duplicate is not None:
        raise UnsupportedSchemaError("现有 evidence 表包含空路径、空哈希或重复 SHA-256，已停止迁移")
    if _table_exists(connection, "vehicles"):
        vehicle_columns = _columns(connection, "vehicles")
        missing_vehicle = sorted({"id", "plate", "vin"} - vehicle_columns)
        if missing_vehicle:
            raise UnsupportedSchemaError(
                "现有 vehicles 表结构未知，缺少必要字段："
                + ", ".join(missing_vehicle)
            )


def migrate_database(database_path: str | Path) -> None:
    """Apply the current schema once; every statement is safe to retry."""
    with database_connection(database_path) as connection:
        _validate_existing_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?",
                (MIGRATION_VERSION,),
            ).fetchone()
            if applied is None:
                _apply_phase_one_migration(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (MIGRATION_VERSION, utc_now()),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _apply_phase_one_migration(connection: sqlite3.Connection) -> None:
    had_evidence = _table_exists(connection, "evidence")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS vehicles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            self_number TEXT,
            plate TEXT,
            vehicle_type TEXT,
            brand_model TEXT,
            owner_name TEXT,
            vin TEXT,
            engine_number TEXT,
            registration_date TEXT,
            curb_weight_kg INTEGER,
            business_owner TEXT,
            transport_method TEXT,
            arrival_time TEXT,
            deregistration_required TEXT,
            document_status TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_vehicles_plate ON vehicles(plate)"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS ix_vehicles_vin ON vehicles(vin)")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sha256 TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            media_type TEXT,
            size_bytes INTEGER,
            ocr_text TEXT,
            extracted_json TEXT,
            error_message TEXT,
            processing_status TEXT NOT NULL DEFAULT 'queued'
                CHECK(processing_status IN ('queued','processing','completed','pending','failed')),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
            started_at TEXT,
            finished_at TEXT,
            locked_at TEXT,
            created_at TEXT NOT NULL
        )
        """
    )

    evidence_columns = _columns(connection, "evidence")
    additions = {
        "media_type": "TEXT",
        "size_bytes": "INTEGER",
        "ocr_text": "TEXT",
        "extracted_json": "TEXT",
        "error_message": "TEXT",
        "processing_status": "TEXT NOT NULL DEFAULT 'queued' CHECK(processing_status IN ('queued','processing','completed','pending','failed'))",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0)",
        "started_at": "TEXT",
        "finished_at": "TEXT",
        "locked_at": "TEXT",
    }
    for column, definition in additions.items():
        if column not in evidence_columns:
            # Identifiers and definitions are fixed migration constants, not input.
            connection.execute(f'ALTER TABLE evidence ADD COLUMN "{column}" {definition}')
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_evidence_sha256 ON evidence(sha256)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_evidence_queue ON evidence(processing_status, id)"
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS conflicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_id INTEGER,
            evidence_id INTEGER,
            field_name TEXT NOT NULL,
            existing_value TEXT,
            incoming_value TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            FOREIGN KEY(vehicle_id) REFERENCES vehicles(id),
            FOREIGN KEY(evidence_id) REFERENCES evidence(id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_no TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'created'
                CHECK(status IN ('created','uploading','queued','processing','needs_review','partial_failed','completed')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_batches_created_at ON batches(created_at DESC)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS batch_images (
            batch_id INTEGER NOT NULL,
            evidence_id INTEGER NOT NULL,
            original_name TEXT NOT NULL,
            upload_order INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(batch_id, evidence_id),
            UNIQUE(batch_id, upload_order),
            FOREIGN KEY(batch_id) REFERENCES batches(id) ON DELETE RESTRICT,
            FOREIGN KEY(evidence_id) REFERENCES evidence(id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_batch_images_evidence ON batch_images(evidence_id)"
    )

    if had_evidence:
        _migrate_legacy_evidence(connection)


def _migrate_legacy_evidence(connection: sqlite3.Connection) -> None:
    """Place all pre-queue evidence in one completed, traceable import batch."""
    count = connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    if count == 0:
        return
    now = utc_now()
    connection.execute(
        """
        UPDATE evidence
        SET processing_status = 'completed',
            attempt_count = COALESCE(attempt_count, 0),
            finished_at = COALESCE(finished_at, created_at)
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO batches(
            batch_no, name, status, created_at, updated_at, completed_at
        ) VALUES (?, ?, 'completed', ?, ?, ?)
        """,
        ("LEGACY-IMPORT", "历史数据导入", now, now, now),
    )
    batch_id = connection.execute(
        "SELECT id FROM batches WHERE batch_no = ?", ("LEGACY-IMPORT",)
    ).fetchone()[0]
    evidence_columns = _columns(connection, "evidence")
    name_expression = "original_name" if "original_name" in evidence_columns else "stored_path"
    connection.execute(
        f"""
        INSERT OR IGNORE INTO batch_images(
            batch_id, evidence_id, original_name, upload_order, created_at
        )
        SELECT ?, id, COALESCE({name_expression}, stored_path),
               ROW_NUMBER() OVER (ORDER BY id), ?
        FROM evidence
        """,
        (batch_id, now),
    )


def create_batch(
    database_path: str | Path, name: str | None = None, *, batch_date: date | None = None
) -> sqlite3.Row:
    chosen_date = batch_date or date.today()
    prefix = chosen_date.strftime("%Y%m%d")
    now = utc_now()
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                """
                SELECT MAX(CAST(SUBSTR(batch_no, 10) AS INTEGER)) AS latest
                FROM batches
                WHERE batch_no LIKE ? AND LENGTH(batch_no) = 12
                """,
                (f"{prefix}-%",),
            ).fetchone()
            sequence = (row["latest"] or 0) + 1
            if sequence > 999:
                raise RuntimeError("当天批次编号已达到 999")
            batch_no = f"{prefix}-{sequence:03d}"
            batch_name = (name or "").strip() or batch_no
            cursor = connection.execute(
                """
                INSERT INTO batches(batch_no, name, status, created_at, updated_at)
                VALUES (?, ?, 'created', ?, ?)
                """,
                (batch_no, batch_name, now, now),
            )
            batch_id = cursor.lastrowid
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return connection.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()


def get_batch(database_path: str | Path, batch_id: int) -> sqlite3.Row | None:
    with database_connection(database_path) as connection:
        return connection.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()


def _batch_stats_sql(where: str = "") -> str:
    return f"""
        SELECT b.*,
               COUNT(bi.evidence_id) AS total,
               SUM(CASE WHEN e.processing_status = 'queued' THEN 1 ELSE 0 END) AS queued,
               SUM(CASE WHEN e.processing_status = 'processing' THEN 1 ELSE 0 END) AS processing,
               SUM(CASE WHEN e.processing_status = 'completed' THEN 1 ELSE 0 END) AS completed,
               SUM(CASE WHEN e.processing_status = 'pending' THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN e.processing_status = 'failed' THEN 1 ELSE 0 END) AS failed
        FROM batches b
        LEFT JOIN batch_images bi ON bi.batch_id = b.id
        LEFT JOIN evidence e ON e.id = bi.evidence_id
        {where}
        GROUP BY b.id
    """


def list_batches(database_path: str | Path) -> list[sqlite3.Row]:
    with database_connection(database_path) as connection:
        return list(
            connection.execute(_batch_stats_sql() + " ORDER BY b.created_at DESC, b.id DESC")
        )


def get_batch_with_stats(database_path: str | Path, batch_id: int) -> sqlite3.Row | None:
    with database_connection(database_path) as connection:
        return connection.execute(
            _batch_stats_sql("WHERE b.id = ?"), (batch_id,)
        ).fetchone()


def list_batch_images(database_path: str | Path, batch_id: int) -> list[sqlite3.Row]:
    with database_connection(database_path) as connection:
        return list(
            connection.execute(
                """
                SELECT bi.original_name, bi.upload_order, bi.created_at,
                       e.id AS evidence_id, e.processing_status, e.error_message,
                       e.sha256, e.stored_path
                FROM batch_images bi
                JOIN evidence e ON e.id = bi.evidence_id
                WHERE bi.batch_id = ?
                ORDER BY bi.upload_order, e.id
                """,
                (batch_id,),
            )
        )


def set_batch_uploading(database_path: str | Path, batch_id: int) -> None:
    with database_connection(database_path) as connection:
        connection.execute(
            "UPDATE batches SET status = 'uploading', updated_at = ? WHERE id = ?",
            (utc_now(), batch_id),
        )
        connection.commit()


def _refresh_batches(connection: sqlite3.Connection, batch_ids: list[int] | None = None) -> None:
    parameters: tuple[Any, ...] = ()
    where = ""
    if batch_ids is not None:
        unique_ids = sorted(set(batch_ids))
        if not unique_ids:
            return
        where = "WHERE b.id IN (" + ",".join("?" for _ in unique_ids) + ")"
        parameters = tuple(unique_ids)
    rows = connection.execute(
        f"""
        SELECT b.id, COUNT(e.id) AS total,
               SUM(CASE WHEN e.processing_status = 'queued' THEN 1 ELSE 0 END) AS queued,
               SUM(CASE WHEN e.processing_status = 'processing' THEN 1 ELSE 0 END) AS processing,
               SUM(CASE WHEN e.processing_status = 'pending' THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN e.processing_status = 'failed' THEN 1 ELSE 0 END) AS failed
        FROM batches b
        LEFT JOIN batch_images bi ON bi.batch_id = b.id
        LEFT JOIN evidence e ON e.id = bi.evidence_id
        {where}
        GROUP BY b.id
        """,
        parameters,
    ).fetchall()
    now = utc_now()
    for row in rows:
        if row["total"] == 0:
            status = "created"
        elif row["processing"]:
            status = "processing"
        elif row["queued"]:
            status = "queued"
        elif row["failed"]:
            status = "partial_failed"
        elif row["pending"]:
            status = "needs_review"
        else:
            status = "completed"
        completed_at = now if status == "completed" else None
        connection.execute(
            """
            UPDATE batches
            SET status = ?, updated_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (status, now, completed_at, row["id"]),
        )


def refresh_batch(database_path: str | Path, batch_id: int) -> None:
    with database_connection(database_path) as connection:
        _refresh_batches(connection, [batch_id])
        connection.commit()


def attach_uploaded_evidence(
    database_path: str | Path,
    *,
    batch_id: int,
    sha256: str,
    stored_path: str,
    media_type: str,
    size_bytes: int,
    original_name: str,
) -> tuple[int, bool, bool]:
    """Create/reuse evidence and attach it once to a batch.

    Returns (evidence_id, evidence_created, relationship_created).
    """
    now = utc_now()
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            batch = connection.execute(
                "SELECT id FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise LookupError("批次不存在")
            evidence = connection.execute(
                "SELECT id FROM evidence WHERE sha256 = ?", (sha256,)
            ).fetchone()
            created = evidence is None
            if created:
                cursor = connection.execute(
                    """
                    INSERT INTO evidence(
                        sha256, stored_path, media_type, size_bytes,
                        processing_status, attempt_count, created_at
                    ) VALUES (?, ?, ?, ?, 'queued', 0, ?)
                    """,
                    (sha256, stored_path, media_type, size_bytes, now),
                )
                evidence_id = cursor.lastrowid
            else:
                evidence_id = evidence["id"]
            next_order = connection.execute(
                "SELECT COALESCE(MAX(upload_order), 0) + 1 FROM batch_images WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()[0]
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO batch_images(
                    batch_id, evidence_id, original_name, upload_order, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (batch_id, evidence_id, original_name, next_order, now),
            )
            relationship_created = cursor.rowcount == 1
            _refresh_batches(connection, [batch_id])
            connection.commit()
            return evidence_id, created, relationship_created
        except Exception:
            connection.rollback()
            raise


def claim_next_queued_evidence(database_path: str | Path) -> dict[str, Any] | None:
    """Claim atomically; retain the returned attempt_count for terminal updates."""
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT * FROM evidence WHERE processing_status = ? ORDER BY id LIMIT 1",
                ("queued",),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            now = utc_now()
            cursor = connection.execute(
                """
                UPDATE evidence
                SET processing_status = 'processing', attempt_count = attempt_count + 1,
                    started_at = ?, locked_at = ?, finished_at = NULL,
                    error_message = NULL
                WHERE id = ? AND processing_status = 'queued'
                """,
                (now, now, row["id"]),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            batch_ids = [
                item[0]
                for item in connection.execute(
                    "SELECT batch_id FROM batch_images WHERE evidence_id = ?", (row["id"],)
                )
            ]
            _refresh_batches(connection, batch_ids)
            claimed = connection.execute(
                "SELECT * FROM evidence WHERE id = ?", (row["id"],)
            ).fetchone()
            connection.commit()
            return dict(claimed)
        except Exception:
            connection.rollback()
            raise


def _mark_evidence(
    database_path: str | Path, evidence_id: int, status: str, error_message: str | None,
    *, attempt_count: int,
) -> None:
    if status not in {"completed", "pending", "failed"}:
        raise ValueError("无效任务终态")
    if type(attempt_count) is not int or attempt_count < 1:
        raise ValueError("领取次数必须为正整数")
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = connection.execute(
                """
                UPDATE evidence
                SET processing_status = ?, error_message = ?, finished_at = ?, locked_at = NULL
                WHERE id = ? AND processing_status = 'processing' AND attempt_count = ?
                """,
                (status, error_message, utc_now(), evidence_id, attempt_count),
            )
            if cursor.rowcount != 1:
                # A timed-out worker cannot finish a newer claim, even when its
                # evidence is processing again. Keep this check in the UPDATE.
                raise ValueError("任务不存在、当前不在 processing 状态或领取已过期")
            batch_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT batch_id FROM batch_images WHERE evidence_id = ?", (evidence_id,)
                )
            ]
            _refresh_batches(connection, batch_ids)
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def mark_evidence_completed(
    database_path: str | Path, evidence_id: int, *, attempt_count: int
) -> None:
    _mark_evidence(database_path, evidence_id, "completed", None, attempt_count=attempt_count)


def mark_evidence_pending(
    database_path: str | Path, evidence_id: int, reason: str | None = None,
    *, attempt_count: int,
) -> None:
    _mark_evidence(database_path, evidence_id, "pending", reason, attempt_count=attempt_count)


def mark_evidence_failed(
    database_path: str | Path, evidence_id: int, error: str, *, attempt_count: int
) -> None:
    _mark_evidence(database_path, evidence_id, "failed", error, attempt_count=attempt_count)


def requeue_stale_processing_jobs(
    database_path: str | Path, *, timeout_seconds: int = 900
) -> int:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
    ).isoformat(timespec="seconds")
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            stale = connection.execute(
                """
                SELECT id FROM evidence
                WHERE processing_status = 'processing'
                  AND locked_at IS NOT NULL AND locked_at < ?
                """,
                (cutoff,),
            ).fetchall()
            evidence_ids = [row["id"] for row in stale]
            if evidence_ids:
                placeholders = ",".join("?" for _ in evidence_ids)
                connection.execute(
                    f"""
                    UPDATE evidence
                    SET processing_status = 'queued', started_at = NULL,
                        locked_at = NULL, finished_at = NULL,
                        error_message = '处理超时，已重新排队'
                    WHERE id IN ({placeholders})
                    """,
                    tuple(evidence_ids),
                )
                batch_ids = [
                    row[0]
                    for row in connection.execute(
                        f"SELECT DISTINCT batch_id FROM batch_images WHERE evidence_id IN ({placeholders})",
                        tuple(evidence_ids),
                    )
                ]
                _refresh_batches(connection, batch_ids)
            connection.commit()
            return len(evidence_ids)
        except Exception:
            connection.rollback()
            raise
