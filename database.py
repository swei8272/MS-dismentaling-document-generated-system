from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


TASK_STATUSES = ("queued", "processing", "completed", "pending", "failed")
MIGRATION_VERSION = 3
BUSY_TIMEOUT_MS = 5_000


class UnsupportedSchemaError(RuntimeError):
    """Raised before migration writes when an existing schema is unknown."""


class UploadAssociationError(ValueError):
    """Raised when a retry identifier cannot safely be associated with an upload."""


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
    """Apply every unapplied migration in order; each migration is retry-safe."""
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
            migrations = (
                (1, _apply_phase_one_migration),
                (2, _apply_phase_two_migration),
                (3, _apply_phase_two_recovery_migration),
            )
            applied = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            unknown = sorted(version for version in applied if version > MIGRATION_VERSION)
            if unknown:
                raise UnsupportedSchemaError(
                    f"数据库版本 {unknown[-1]} 高于程序支持的版本 {MIGRATION_VERSION}，已停止迁移"
                )
            for version, migration in migrations:
                if version in applied:
                    continue
                migration(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, utc_now()),
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


def _apply_phase_two_migration(connection: sqlite3.Connection) -> None:
    """Keep upload transport failures separate from OCR evidence task state."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS upload_failures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            client_id TEXT NOT NULL,
            original_name TEXT NOT NULL,
            size_bytes INTEGER,
            reason TEXT NOT NULL,
            retryable INTEGER NOT NULL DEFAULT 0 CHECK(retryable IN (0, 1)),
            status TEXT NOT NULL DEFAULT 'failed'
                CHECK(status IN ('failed', 'resolved')),
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            UNIQUE(batch_id, client_id),
            FOREIGN KEY(batch_id) REFERENCES batches(id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_upload_failures_batch_status
        ON upload_failures(batch_id, status, id DESC)
        """
    )


def _apply_phase_two_recovery_migration(connection: sqlite3.Connection) -> None:
    """Add durable upload receipts without rewriting the already released migration 2."""
    if "resolved_evidence_id" not in _columns(connection, "upload_failures"):
        connection.execute(
            """
            ALTER TABLE upload_failures
            ADD COLUMN resolved_evidence_id INTEGER REFERENCES evidence(id)
            """
        )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS upload_receipts (
            batch_id INTEGER NOT NULL,
            client_id TEXT NOT NULL,
            evidence_id INTEGER NOT NULL,
            confirmed_at TEXT NOT NULL,
            PRIMARY KEY(batch_id, client_id),
            FOREIGN KEY(batch_id) REFERENCES batches(id) ON DELETE RESTRICT,
            FOREIGN KEY(evidence_id) REFERENCES evidence(id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_upload_receipts_evidence
        ON upload_receipts(evidence_id)
        """
    )


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
    return list_batch_images_page(database_path, batch_id)


def list_batch_images_page(
    database_path: str | Path,
    batch_id: int,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[sqlite3.Row]:
    if limit is not None and limit < 1:
        raise ValueError("分页数量必须为正整数")
    if offset < 0:
        raise ValueError("分页偏移不能为负数")
    pagination = ""
    parameters: list[Any] = [batch_id]
    if limit is not None:
        pagination = " LIMIT ? OFFSET ?"
        parameters.extend((limit, offset))
    with database_connection(database_path) as connection:
        return list(
            connection.execute(
                f"""
                SELECT bi.original_name, bi.upload_order, bi.created_at,
                       e.id AS evidence_id, e.processing_status, e.error_message,
                       e.sha256, e.stored_path
                FROM batch_images bi
                JOIN evidence e ON e.id = bi.evidence_id
                WHERE bi.batch_id = ?
                ORDER BY bi.upload_order, e.id
                {pagination}
                """,
                tuple(parameters),
            )
        )


def count_batch_images(database_path: str | Path, batch_id: int) -> int:
    with database_connection(database_path) as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM batch_images WHERE batch_id = ?", (batch_id,)
        ).fetchone()[0]


def record_upload_failure(
    database_path: str | Path,
    *,
    batch_id: int,
    client_id: str,
    original_name: str,
    size_bytes: int | None,
    reason: str,
    retryable: bool,
) -> int | None:
    """Persist an idempotent transport failure unless this client was confirmed.

    ``None`` means the same batch/client identifier already has a durable success
    receipt (or an already-resolved failure), so a late browser write must not
    recreate an unresolved failure.
    """
    now = utc_now()
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            receipt = connection.execute(
                """
                SELECT evidence_id FROM upload_receipts
                WHERE batch_id = ? AND client_id = ?
                """,
                (batch_id, client_id),
            ).fetchone()
            if receipt is not None:
                connection.commit()
                return None

            existing = connection.execute(
                """
                SELECT id, status FROM upload_failures
                WHERE batch_id = ? AND client_id = ?
                """,
                (batch_id, client_id),
            ).fetchone()
            if existing is not None and existing["status"] == "resolved":
                connection.commit()
                return None
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO upload_failures(
                        batch_id, client_id, original_name, size_bytes, reason,
                        retryable, status, created_at, resolved_at,
                        resolved_evidence_id
                    ) VALUES (?, ?, ?, ?, ?, ?, 'failed', ?, NULL, NULL)
                    """,
                    (
                        batch_id,
                        client_id,
                        original_name,
                        size_bytes,
                        reason,
                        int(retryable),
                        now,
                    ),
                )
                failure_id = int(cursor.lastrowid)
            else:
                failure_id = int(existing["id"])
                connection.execute(
                    """
                    UPDATE upload_failures
                    SET original_name = ?, size_bytes = ?, reason = ?, retryable = ?
                    WHERE id = ? AND status = 'failed'
                    """,
                    (
                        original_name,
                        size_bytes,
                        reason,
                        int(retryable),
                        failure_id,
                    ),
                )
            connection.commit()
            return failure_id
        except Exception:
            connection.rollback()
            raise


def list_upload_failures(
    database_path: str | Path,
    batch_id: int,
    *,
    limit: int = 25,
    offset: int = 0,
) -> list[sqlite3.Row]:
    if limit < 1:
        raise ValueError("失败记录数量必须为正整数")
    if offset < 0:
        raise ValueError("失败记录分页偏移不能为负数")
    with database_connection(database_path) as connection:
        return list(
            connection.execute(
                """
                SELECT id, client_id, original_name, size_bytes, reason,
                       retryable, status, created_at, resolved_at,
                       resolved_evidence_id
                FROM upload_failures
                WHERE batch_id = ? AND status = 'failed'
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (batch_id, limit, offset),
            )
        )


def get_upload_failures_page(
    database_path: str | Path,
    batch_id: int,
    *,
    page: int,
    per_page: int,
) -> tuple[list[sqlite3.Row], int, int, int]:
    """Read failure count and one bounded page from the same SQLite snapshot."""
    if page < 1 or per_page < 1:
        raise ValueError("失败记录分页参数必须为正整数")
    with database_connection(database_path) as connection:
        connection.execute("BEGIN")
        try:
            total = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM upload_failures
                    WHERE batch_id = ? AND status = 'failed'
                    """,
                    (batch_id,),
                ).fetchone()[0]
            )
            page_count = max((total + per_page - 1) // per_page, 1)
            normalized_page = min(page, page_count)
            rows = list(
                connection.execute(
                    """
                    SELECT id, client_id, original_name, size_bytes, reason,
                           retryable, status, created_at, resolved_at,
                           resolved_evidence_id
                    FROM upload_failures
                    WHERE batch_id = ? AND status = 'failed'
                    ORDER BY id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (
                        batch_id,
                        per_page,
                        (normalized_page - 1) * per_page,
                    ),
                )
            )
            connection.commit()
            return rows, total, normalized_page, page_count
        except Exception:
            connection.rollback()
            raise


def count_upload_failures(database_path: str | Path, batch_id: int) -> int:
    with database_connection(database_path) as connection:
        return connection.execute(
            """
            SELECT COUNT(*) FROM upload_failures
            WHERE batch_id = ? AND status = 'failed'
            """,
            (batch_id,),
        ).fetchone()[0]


def get_upload_failure_summary(
    database_path: str | Path, batch_id: int
) -> tuple[int, int | None]:
    """Return only the unresolved count/latest id for lightweight polling."""
    with database_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS total, MAX(id) AS latest_id
            FROM upload_failures
            WHERE batch_id = ? AND status = 'failed'
            """,
            (batch_id,),
        ).fetchone()
        return int(row["total"]), row["latest_id"]


def get_expected_upload_sha256(
    database_path: str | Path,
    *,
    batch_id: int,
    client_id: str,
    failure_id: int | None = None,
) -> str | None:
    """Validate a retry key and return content already confirmed for that key.

    The caller supplies this SHA to storage so a conflicting retry is rejected
    while its bytes are still in the temporary incoming file.
    """
    with database_connection(database_path) as connection:
        if failure_id is not None:
            failure = connection.execute(
                """
                SELECT batch_id, client_id, status, resolved_evidence_id
                FROM upload_failures
                WHERE id = ?
                """,
                (failure_id,),
            ).fetchone()
            if (
                failure is None
                or failure["batch_id"] != batch_id
                or failure["client_id"] != client_id
            ):
                raise UploadAssociationError("失败记录无效或不属于当前批次。")
        else:
            failure = connection.execute(
                """
                SELECT batch_id, client_id, status, resolved_evidence_id
                FROM upload_failures
                WHERE batch_id = ? AND client_id = ?
                """,
                (batch_id, client_id),
            ).fetchone()

        receipt = connection.execute(
            """
            SELECT e.sha256
            FROM upload_receipts ur
            JOIN evidence e ON e.id = ur.evidence_id
            WHERE ur.batch_id = ? AND ur.client_id = ?
            """,
            (batch_id, client_id),
        ).fetchone()
        receipt_sha = receipt["sha256"] if receipt is not None else None

        resolved_sha = None
        if failure is not None and failure["status"] == "resolved":
            if failure["resolved_evidence_id"] is None:
                raise UploadAssociationError(
                    "该失败记录在升级前已经解决，无法安全验证重复请求；请刷新批次确认服务端结果。"
                )
            resolved = connection.execute(
                "SELECT sha256 FROM evidence WHERE id = ?",
                (failure["resolved_evidence_id"],),
            ).fetchone()
            if resolved is None:
                raise UploadAssociationError("失败记录关联的图片不存在，已停止重试。")
            resolved_sha = resolved["sha256"]

    if receipt_sha is not None and resolved_sha is not None and receipt_sha != resolved_sha:
        raise UploadAssociationError("上传回执与失败记录不一致，已停止重试。")
    return receipt_sha or resolved_sha


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
    upload_failure_id: int | None = None,
    upload_failure_client_id: str | None = None,
    finalize_storage: Callable[[], str] | None = None,
    rollback_storage: Callable[[], None] | None = None,
) -> tuple[int, bool, bool]:
    """Create/reuse evidence and attach it once to a batch.

    If supplied, ``finalize_storage`` runs under ``BEGIN IMMEDIATE`` only after
    the exact retry/receipt identity has been revalidated.  This closes the gap
    where two requests could both write canonical bytes before one lost the
    client-id race in SQLite.

    Returns (evidence_id, evidence_created, relationship_created).
    """
    now = utc_now()
    with database_connection(database_path) as connection:
        storage_finalized = False
        connection.execute("BEGIN IMMEDIATE")
        try:
            batch = connection.execute(
                "SELECT id FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise LookupError("批次不存在")
            failure = None
            if upload_failure_id is not None:
                failure = connection.execute(
                    """
                    SELECT id, batch_id, client_id, status, resolved_evidence_id
                    FROM upload_failures
                    WHERE id = ?
                    """,
                    (upload_failure_id,),
                ).fetchone()
                if (
                    failure is None
                    or failure["batch_id"] != batch_id
                    or failure["client_id"] != upload_failure_client_id
                ):
                    raise UploadAssociationError("失败记录无效或不属于当前批次。")
            elif upload_failure_client_id:
                # An offline outbox write and its successful retry may cross.
                # The exact client id is safe to associate; names/sizes are not.
                failure = connection.execute(
                    """
                    SELECT id, batch_id, client_id, status, resolved_evidence_id
                    FROM upload_failures
                    WHERE batch_id = ? AND client_id = ?
                    """,
                    (batch_id, upload_failure_client_id),
                ).fetchone()

            evidence = connection.execute(
                "SELECT id, stored_path FROM evidence WHERE sha256 = ?", (sha256,)
            ).fetchone()
            receipt = None
            if upload_failure_client_id:
                receipt = connection.execute(
                    """
                    SELECT ur.evidence_id, e.sha256
                    FROM upload_receipts ur
                    JOIN evidence e ON e.id = ur.evidence_id
                    WHERE batch_id = ? AND client_id = ?
                    """,
                    (batch_id, upload_failure_client_id),
                ).fetchone()
                if receipt is not None and receipt["sha256"] != sha256:
                    raise UploadAssociationError(
                        "该上传标识已经确认过另一张图片，不能重新关联。"
                    )
            if failure is not None and failure["status"] == "resolved":
                if failure["resolved_evidence_id"] is None:
                    raise UploadAssociationError(
                        "该失败记录在升级前已经解决，无法安全验证重复请求；请刷新批次确认服务端结果。"
                    )
                resolved = connection.execute(
                    "SELECT sha256 FROM evidence WHERE id = ?",
                    (failure["resolved_evidence_id"],),
                ).fetchone()
                if resolved is None:
                    raise UploadAssociationError("失败记录关联的图片不存在，已停止重试。")
                if resolved["sha256"] != sha256:
                    raise UploadAssociationError(
                        "该失败记录已经由另一张图片解决，不能重复关联。"
                    )

            created = evidence is None
            if created:
                committed_path = finalize_storage() if finalize_storage else stored_path
                storage_finalized = finalize_storage is not None
                cursor = connection.execute(
                    """
                    INSERT INTO evidence(
                        sha256, stored_path, media_type, size_bytes,
                        processing_status, attempt_count, created_at
                    ) VALUES (?, ?, ?, ?, 'queued', 0, ?)
                    """,
                    (sha256, committed_path, media_type, size_bytes, now),
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
            if failure is not None and failure["status"] == "failed":
                cursor = connection.execute(
                    """
                    UPDATE upload_failures
                    SET status = 'resolved', resolved_at = ?,
                        resolved_evidence_id = ?
                    WHERE id = ? AND batch_id = ? AND client_id = ?
                      AND status = 'failed'
                    """,
                    (
                        now,
                        evidence_id,
                        failure["id"],
                        batch_id,
                        upload_failure_client_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise UploadAssociationError("失败记录状态已变化，请刷新后重试。")
            if upload_failure_client_id and receipt is None:
                connection.execute(
                    """
                    INSERT INTO upload_receipts(
                        batch_id, client_id, evidence_id, confirmed_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (batch_id, upload_failure_client_id, evidence_id, now),
                )
            _refresh_batches(connection, [batch_id])
            connection.commit()
            return evidence_id, created, relationship_created
        except Exception:
            # If SQL after os.replace fails, remove only the file created by this
            # request while the SQLite write lock is still held.  Releasing the
            # lock first could let a waiting request adopt that path before it is
            # removed.  If commit outcome is already final, leave the file alone.
            try:
                if (
                    storage_finalized
                    and rollback_storage is not None
                    and connection.in_transaction
                ):
                    rollback_storage()
            finally:
                if connection.in_transaction:
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
