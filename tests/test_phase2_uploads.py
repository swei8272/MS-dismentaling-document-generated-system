from __future__ import annotations

import io
from pathlib import Path

import app as app_module
from app import create_app
from database import (
    claim_next_queued_evidence,
    connect_database,
    create_batch,
    mark_evidence_completed,
)
from storage import save_image_stream as real_save_image_stream
from tests.conftest import image_bytes


JSON_HEADERS = {
    "Accept": "application/json",
    "X-Requested-With": "BatchUploader",
}


def json_upload(client, batch_id: int, files, client_ids, failure_ids=None):
    data = {
        "files": [(io.BytesIO(content), name) for content, name in files],
        "client_ids": client_ids,
    }
    if failure_ids is not None:
        data["failure_ids"] = failure_ids
    return client.post(
        f"/batches/{batch_id}/upload",
        data=data,
        content_type="multipart/form-data",
        headers=JSON_HEADERS,
    )


def test_json_results_distinguish_same_name_and_same_content(
    client, database_path: Path
) -> None:
    batch = create_batch(database_path)
    first = image_bytes((20, 30, 40))
    second = image_bytes((50, 60, 70))
    response = json_upload(
        client,
        batch["id"],
        [(first, "同名.png"), (second, "同名.png"), (first, "另一个名字.png")],
        ["file-a", "file-b", "file-c"],
    )
    assert response.status_code == 200
    results = response.get_json()["results"]
    assert [result["client_id"] for result in results] == ["file-a", "file-b", "file-c"]
    assert [result["status"] for result in results] == [
        "added",
        "added",
        "already_in_batch",
    ]
    with connect_database(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM batch_images").fetchone()[0] == 2


def test_json_upload_requires_request_unique_client_ids(
    client, database_path: Path, png_bytes: bytes
) -> None:
    batch = create_batch(database_path)
    response = json_upload(
        client,
        batch["id"],
        [(png_bytes, "一.png"), (image_bytes((1, 2, 3)), "二.png")],
        ["duplicate", "duplicate"],
    )
    assert response.status_code == 400
    assert "唯一标识" in response.get_json()["error"]
    with connect_database(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


def test_legacy_form_failures_from_separate_requests_do_not_overwrite(
    client, database_path: Path
) -> None:
    batch = create_batch(database_path)
    for name in ("损坏一.png", "损坏二.png"):
        response = client.post(
            f"/batches/{batch['id']}/upload",
            data={"files": (io.BytesIO(b"damaged"), name)},
            content_type="multipart/form-data",
        )
        assert response.status_code == 303
    with connect_database(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM upload_failures").fetchone()[0] == 2


def test_mixed_corrupt_upload_records_transport_failure_not_ocr_failure(
    client, database_path: Path, png_bytes: bytes
) -> None:
    batch = create_batch(database_path)
    response = json_upload(
        client,
        batch["id"],
        [(b"damaged", "损坏.png"), (png_bytes, "正常.png")],
        ["bad", "good"],
    )
    assert response.status_code == 200
    results = {item["client_id"]: item for item in response.get_json()["results"]}
    assert results["bad"]["status"] == "failed"
    assert results["bad"]["retryable"] is False
    assert results["good"]["status"] == "added"
    status = client.get(f"/batches/{batch['id']}/status").get_json()
    assert status["batch"]["total"] == 1
    assert status["batch"]["queued"] == 1
    assert status["batch"]["failed"] == 0
    assert status["upload_failure_count"] == 1
    with connect_database(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM upload_failures").fetchone()[0] == 1


def test_lost_response_retransmit_is_idempotent_and_does_not_requeue_completed(
    client, database_path: Path, png_bytes: bytes
) -> None:
    batch = create_batch(database_path)
    first = json_upload(client, batch["id"], [(png_bytes, "原图.png")], ["unknown-a"])
    assert first.get_json()["results"][0]["status"] == "added"
    claim = claim_next_queued_evidence(database_path)
    mark_evidence_completed(
        database_path, claim["id"], attempt_count=claim["attempt_count"]
    )

    retry = json_upload(client, batch["id"], [(png_bytes, "重传.png")], ["unknown-b"])
    result = retry.get_json()["results"][0]
    assert result["status"] == "already_in_batch"
    assert result["processing_status"] == "completed"
    with connect_database(database_path) as connection:
        evidence = connection.execute("SELECT * FROM evidence").fetchone()
        assert evidence["processing_status"] == "completed"
        assert evidence["attempt_count"] == 1
        assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM batch_images").fetchone()[0] == 1


def test_cross_batch_json_upload_reuses_global_evidence(
    client, database_path: Path, png_bytes: bytes
) -> None:
    first = create_batch(database_path)
    second = create_batch(database_path)
    assert json_upload(client, first["id"], [(png_bytes, "一.png")], ["a"]).get_json()[
        "results"
    ][0]["status"] == "added"
    result = json_upload(client, second["id"], [(png_bytes, "二.png")], ["b"]).get_json()[
        "results"
    ][0]
    assert result["status"] == "reused"
    with connect_database(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM batch_images").fetchone()[0] == 2


def test_file_and_group_limits_fail_individual_files_and_continue(
    database_path: Path, upload_dir: Path, png_bytes: bytes
) -> None:
    larger = image_bytes((100, 110, 120)) + b"padding-that-keeps-png-valid"
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-only",
            "DATABASE_PATH": database_path,
            "UPLOAD_DIR": upload_dir,
            "UPLOAD_FILE_MAX_BYTES": len(png_bytes) + 2,
            "UPLOAD_GROUP_MAX_BYTES": len(png_bytes) + 3,
        }
    )
    batch = create_batch(database_path)
    response = json_upload(
        app.test_client(),
        batch["id"],
        [(larger, "超限.png"), (png_bytes, "正常.png")],
        ["large", "normal"],
    )
    results = {item["client_id"]: item for item in response.get_json()["results"]}
    assert results["large"]["status"] == "failed"
    assert "单个文件不能超过" in results["large"]["message"]
    assert results["normal"]["status"] == "added"

    another = image_bytes((200, 10, 20))
    batch_two = create_batch(database_path)
    response = json_upload(
        app.test_client(),
        batch_two["id"],
        [(png_bytes, "第一.png"), (another, "组超限.png")],
        ["first", "overflow"],
    )
    results = {item["client_id"]: item for item in response.get_json()["results"]}
    assert results["first"]["status"] == "reused"
    assert results["overflow"]["status"] == "failed"
    assert "本组图片总大小" in results["overflow"]["message"]


def test_server_group_count_limit_only_rejects_excess_files(
    database_path: Path, upload_dir: Path
) -> None:
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-only",
            "DATABASE_PATH": database_path,
            "UPLOAD_DIR": upload_dir,
            "UPLOAD_GROUP_MAX_FILES": 1,
        }
    )
    batch = create_batch(database_path)
    response = json_upload(
        app.test_client(),
        batch["id"],
        [(image_bytes((1, 1, 1)), "一.png"), (image_bytes((2, 2, 2)), "二.png")],
        ["one", "two"],
    )
    assert [item["status"] for item in response.get_json()["results"]] == ["added", "failed"]


def test_temporary_failure_can_be_manually_retried_and_resolved(
    monkeypatch, client, database_path: Path, png_bytes: bytes
) -> None:
    batch = create_batch(database_path)
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("private local path must not leak")
        return real_save_image_stream(*args, **kwargs)

    monkeypatch.setattr(app_module, "save_image_stream", fail_once)
    failed = json_upload(client, batch["id"], [(png_bytes, "重试.png")], ["retry-me"])
    first_result = failed.get_json()["results"][0]
    assert first_result["status"] == "failed"
    assert first_result["retryable"] is True
    assert "private local path" not in first_result["message"]

    retried = json_upload(
        client,
        batch["id"],
        [(png_bytes, "重试.png")],
        ["retry-me"],
        [str(first_result["failure_id"])],
    )
    assert retried.get_json()["results"][0]["status"] == "added"
    assert client.get(f"/batches/{batch['id']}/status").get_json()[
        "upload_failure_count"
    ] == 0


def test_client_side_oversize_record_survives_refresh_without_evidence(
    client, database_path: Path, app
) -> None:
    batch = create_batch(database_path)
    response = client.post(
        f"/batches/{batch['id']}/upload-failures",
        json={
            "items": [
                {
                    "client_id": "too-large",
                    "name": "大图.png",
                    "size_bytes": app.config["UPLOAD_FILE_MAX_BYTES"] + 1,
                }
            ]
        },
    )
    assert response.status_code == 200
    page = client.get(f"/batches/{batch['id']}")
    assert "大图.png".encode() in page.data
    assert "单个文件不能超过".encode() in page.data
    with connect_database(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM upload_failures").fetchone()[0] == 1


def test_batch_image_page_is_bounded(client, database_path: Path) -> None:
    batch = create_batch(database_path)
    for index in range(55):
        with connect_database(database_path) as connection:
            now = f"2026-09-06T00:00:{index:02d}+00:00"
            cursor = connection.execute(
                """
                INSERT INTO evidence(
                    sha256, stored_path, processing_status, attempt_count, created_at
                ) VALUES (?, ?, 'queued', 0, ?)
                """,
                (f"{index:064x}", f"synthetic/{index}.png", now),
            )
            connection.execute(
                """
                INSERT INTO batch_images(
                    batch_id, evidence_id, original_name, upload_order, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (batch["id"], cursor.lastrowid, f"合成-{index}.png", index + 1, now),
            )
            connection.commit()
    page = client.get(f"/batches/{batch['id']}")
    assert page.data.count(b"<tbody>") == 1
    assert "第 1 / 2 页".encode() in page.data
    payload = client.get(f"/batches/{batch['id']}/images?page=2&per_page=50").get_json()
    assert payload["total"] == 55
    assert len(payload["items"]) == 5
