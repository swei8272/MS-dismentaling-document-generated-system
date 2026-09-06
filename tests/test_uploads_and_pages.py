from __future__ import annotations

import io
import sys
import types
from pathlib import Path

from app import create_app
from database import (
    claim_next_queued_evidence,
    connect_database,
    create_batch,
    mark_evidence_completed,
)


def upload(client, batch_id: int, content: bytes, name: str = "车辆.png"):
    return client.post(
        f"/batches/{batch_id}/upload",
        data={"files": (io.BytesIO(content), name)},
        content_type="multipart/form-data",
        follow_redirects=False,
    )


def test_batch_pages_create_list_and_show_chinese(client, database_path: Path) -> None:
    response = client.post("/batches/new", data={"name": "上午车辆"})
    assert response.status_code == 303
    listing = client.get("/batches")
    assert listing.status_code == 200
    assert "批次管理".encode() in listing.data
    assert "上午车辆".encode() in listing.data
    assert client.get("/upload").status_code == 303


def test_new_image_is_queued_and_saved_by_hash(
    client, database_path: Path, upload_dir: Path, png_bytes: bytes
) -> None:
    batch = create_batch(database_path)
    response = upload(client, batch["id"], png_bytes)
    assert response.status_code == 303
    with connect_database(database_path) as connection:
        evidence = connection.execute("SELECT * FROM evidence").fetchone()
        link = connection.execute("SELECT * FROM batch_images").fetchone()
    assert evidence["processing_status"] == "queued"
    assert evidence["attempt_count"] == 0
    assert len(evidence["sha256"]) == 64
    assert (upload_dir / evidence["stored_path"]).is_file()
    assert link["original_name"] == "车辆.png"


def test_same_batch_duplicate_is_not_linked_twice(
    client, database_path: Path, png_bytes: bytes
) -> None:
    batch = create_batch(database_path)
    upload(client, batch["id"], png_bytes, "第一张.png")
    upload(client, batch["id"], png_bytes, "重复.png")
    with connect_database(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM batch_images").fetchone()[0] == 1


def test_cross_batch_duplicate_reuses_completed_evidence_without_requeue(
    client, database_path: Path, png_bytes: bytes
) -> None:
    first = create_batch(database_path)
    second = create_batch(database_path)
    upload(client, first["id"], png_bytes)
    claimed = claim_next_queued_evidence(database_path)
    mark_evidence_completed(database_path, claimed["id"], attempt_count=claimed["attempt_count"])
    upload(client, second["id"], png_bytes, "另一个文件名.png")
    with connect_database(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM batch_images").fetchone()[0] == 2
        status = connection.execute("SELECT processing_status FROM evidence").fetchone()[0]
    assert status == "completed"


def test_one_invalid_image_does_not_fail_other_files(
    client, database_path: Path, png_bytes: bytes
) -> None:
    batch = create_batch(database_path)
    response = client.post(
        f"/batches/{batch['id']}/upload",
        data={
            "files": [
                (io.BytesIO(b"not an image"), "损坏.png"),
                (io.BytesIO(png_bytes), "正常.png"),
            ]
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "文件内容不是有效图片".encode() in response.data
    with connect_database(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1


def test_upload_does_not_import_or_call_ocr_or_merge(
    monkeypatch, client, database_path: Path, png_bytes: bytes
) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("上传请求不得调用 OCR、字段提取或车辆合并")

    monkeypatch.setitem(sys.modules, "ocr_engine", types.SimpleNamespace(recognize=forbidden))
    monkeypatch.setitem(
        sys.modules, "parser", types.SimpleNamespace(extract_vehicle_fields=forbidden)
    )
    monkeypatch.setitem(sys.modules, "matcher", types.SimpleNamespace(merge_evidence=forbidden))
    batch = create_batch(database_path)
    assert upload(client, batch["id"], png_bytes).status_code == 303


def test_invalid_extension_is_rejected(client, database_path: Path, png_bytes: bytes) -> None:
    batch = create_batch(database_path)
    response = upload(client, batch["id"], png_bytes, "伪装.exe")
    assert response.status_code == 303
    with connect_database(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


def test_recreating_app_keeps_existing_batch(
    app, database_path: Path, upload_dir: Path
) -> None:
    batch = create_batch(database_path, "重启后仍存在")
    restarted = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-only",
            "DATABASE_PATH": database_path,
            "UPLOAD_DIR": upload_dir,
        }
    )
    response = restarted.test_client().get(f"/batches/{batch['id']}")
    assert response.status_code == 200
    assert "重启后仍存在".encode() in response.data
