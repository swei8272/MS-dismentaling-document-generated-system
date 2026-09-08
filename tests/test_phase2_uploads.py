from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import app as app_module
import database as database_module
import pytest
from app import create_app
from database import (
    attach_uploaded_evidence,
    claim_next_queued_evidence,
    connect_database,
    create_batch,
    mark_evidence_completed,
)
from storage import save_image_stream as real_save_image_stream
from tests.conftest import image_bytes
from werkzeug.datastructures import FileStorage


JSON_HEADERS = {
    "Accept": "application/json",
    "X-Requested-With": "BatchUploader",
}


def json_upload(client, batch_id: int, files, client_ids, failure_ids=None):
    data = {
        "files": [(io.BytesIO(content), name) for content, name in files],
        "client_ids": client_ids,
        "file_sizes": [str(len(content)) for content, _name in files],
    }
    if failure_ids is not None:
        data["failure_ids"] = failure_ids
    return client.post(
        f"/batches/{batch_id}/upload",
        data=data,
        content_type="multipart/form-data",
        headers=JSON_HEADERS,
    )


def persist_failures(client, batch_id: int, items):
    return client.post(
        f"/batches/{batch_id}/upload-failures",
        json={"items": items},
        headers={"Accept": "application/json"},
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


@pytest.mark.parametrize("failure_total", [26, 61], ids=["twenty-six", "sixty-one"])
def test_paginated_failures_all_resolve_by_exact_identifier(
    client, database_path: Path, failure_total: int
) -> None:
    batch = create_batch(database_path)
    metadata = [
        {
            "client_id": f"failed-{index:03d}",
            "name": f"原损坏-{index:03d}.jpg",
            "size_bytes": 900 + index,
            "kind": "transport_unknown",
        }
        for index in range(failure_total)
    ]
    persisted = persist_failures(client, batch["id"], metadata)
    assert persisted.status_code == 200
    assert {item["sync_status"] for item in persisted.get_json()["results"]} == {"saved"}

    status = client.get(f"/batches/{batch['id']}/status").get_json()
    assert status["upload_failure_count"] == failure_total
    assert "upload_failures" not in status

    all_failures = []
    page = 1
    while True:
        payload = client.get(
            f"/batches/{batch['id']}/upload-failures?page={page}&per_page=25"
        ).get_json()
        assert payload["total"] == failure_total
        assert len(payload["items"]) <= 25
        all_failures.extend(payload["items"])
        if page >= payload["page_count"]:
            break
        page += 1
    assert len(all_failures) == failure_total
    assert len({row["id"] for row in all_failures}) == failure_total
    assert len({row["client_id"] for row in all_failures}) == failure_total

    for group_start in range(0, failure_total, 25):
        failures = all_failures[group_start : group_start + 25]
        files = []
        for index, _failure in enumerate(failures, start=group_start):
            files.append(
                (
                    image_bytes((index % 256, index * 3 % 256, index * 7 % 256)),
                    f"修复后-{index:03d}.png",
                )
            )
        retried = json_upload(
            client,
            batch["id"],
            files,
            [failure["client_id"] for failure in failures],
            [str(failure["id"]) for failure in failures],
        )
        assert retried.status_code == 200
        assert all(
            result["status"] == "added" for result in retried.get_json()["results"]
        )
        remaining_count = failure_total - group_start - len(failures)
        refreshed_first_page = client.get(
            f"/batches/{batch['id']}/upload-failures?page=1&per_page=25"
        ).get_json()
        assert refreshed_first_page["total"] == remaining_count
        assert len(refreshed_first_page["items"]) == min(remaining_count, 25)

    final_status = client.get(f"/batches/{batch['id']}/status").get_json()
    assert final_status["upload_failure_count"] == 0
    final_page = client.get(
        f"/batches/{batch['id']}/upload-failures?page=1&per_page=25"
    ).get_json()
    assert final_page["total"] == 0
    assert final_page["items"] == []
    with connect_database(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM upload_failures WHERE status = 'resolved'"
        ).fetchone()[0] == failure_total
        assert connection.execute("SELECT COUNT(*) FROM upload_receipts").fetchone()[0] == failure_total
        assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == failure_total
        assert connection.execute("SELECT COUNT(*) FROM batch_images").fetchone()[0] == failure_total


def test_explicit_replacement_resolves_only_selected_same_name_failure(
    client, database_path: Path
) -> None:
    batch = create_batch(database_path)
    persisted = persist_failures(
        client,
        batch["id"],
        [
            {
                "client_id": "same-name-a",
                "name": "同名损坏.jpg",
                "size_bytes": 777,
                "kind": "transport_unknown",
            },
            {
                "client_id": "same-name-b",
                "name": "同名损坏.jpg",
                "size_bytes": 777,
                "kind": "transport_unknown",
            },
        ],
    ).get_json()["results"]
    by_client = {row["client_id"]: row for row in persisted}

    replacement_a = image_bytes((11, 22, 33))
    assert len(replacement_a) != 777
    first_retry = json_upload(
        client,
        batch["id"],
        [(replacement_a, "同名损坏.jpg")],
        ["same-name-a"],
        [str(by_client["same-name-a"]["failure_id"])],
    ).get_json()["results"][0]
    assert first_retry["status"] == "added"

    remaining = client.get(
        f"/batches/{batch['id']}/upload-failures?page=1&per_page=25"
    ).get_json()
    assert remaining["total"] == 1
    assert remaining["items"][0]["client_id"] == "same-name-b"

    replacement_b = image_bytes((44, 55, 66))
    second_retry = json_upload(
        client,
        batch["id"],
        [(replacement_b, "修复并压缩后.png")],
        ["same-name-b"],
        [str(by_client["same-name-b"]["failure_id"])],
    ).get_json()["results"][0]
    assert second_retry["status"] == "added"
    assert client.get(f"/batches/{batch['id']}/status").get_json()[
        "upload_failure_count"
    ] == 0
    with connect_database(database_path) as connection:
        rows = list(
            connection.execute(
                "SELECT client_id, status, resolved_evidence_id FROM upload_failures ORDER BY id"
            )
        )
        assert [row["status"] for row in rows] == ["resolved", "resolved"]
        assert rows[0]["resolved_evidence_id"] != rows[1]["resolved_evidence_id"]


def test_retry_reference_rejects_wrong_client_and_cross_batch_without_writes(
    client, database_path: Path
) -> None:
    source = create_batch(database_path)
    other = create_batch(database_path)
    failure = persist_failures(
        client,
        source["id"],
        [
            {
                "client_id": "source-client",
                "name": "损坏.jpg",
                "size_bytes": 9,
                "kind": "transport_unknown",
            }
        ],
    ).get_json()["results"][0]
    content = image_bytes((80, 81, 82))

    wrong_client = json_upload(
        client,
        source["id"],
        [(content, "替换.png")],
        ["different-client"],
        [str(failure["failure_id"])],
    ).get_json()["results"][0]
    assert wrong_client["status"] == "failed"
    assert wrong_client["retryable"] is False
    assert "失败记录无效" in wrong_client["message"]

    cross_batch = json_upload(
        client,
        other["id"],
        [(content, "替换.png")],
        ["source-client"],
        [str(failure["failure_id"])],
    ).get_json()["results"][0]
    assert cross_batch["status"] == "failed"
    assert cross_batch["retryable"] is False
    assert "当前批次" in cross_batch["message"]

    with connect_database(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM batch_images").fetchone()[0] == 0
        assert connection.execute(
            "SELECT status FROM upload_failures WHERE id = ?", (failure["failure_id"],)
        ).fetchone()[0] == "failed"


def test_resolved_failure_retry_is_idempotent_only_for_same_content(
    client, database_path: Path, upload_dir: Path
) -> None:
    batch = create_batch(database_path)
    failure = persist_failures(
        client,
        batch["id"],
        [
            {
                "client_id": "stable-client",
                "name": "待修复.jpg",
                "size_bytes": 14,
                "kind": "transport_unknown",
            }
        ],
    ).get_json()["results"][0]
    content = image_bytes((91, 92, 93))
    first = json_upload(
        client,
        batch["id"],
        [(content, "修复.png")],
        ["stable-client"],
        [str(failure["failure_id"])],
    ).get_json()["results"][0]
    assert first["status"] == "added"

    duplicate = json_upload(
        client,
        batch["id"],
        [(content, "响应丢失后重传.png")],
        ["stable-client"],
        [str(failure["failure_id"])],
    ).get_json()["results"][0]
    assert duplicate["status"] == "already_in_batch"

    conflicting = json_upload(
        client,
        batch["id"],
        [(image_bytes((101, 102, 103)), "另一张图.png")],
        ["stable-client"],
        [str(failure["failure_id"])],
    ).get_json()["results"][0]
    assert conflicting["status"] == "failed"
    assert conflicting["retryable"] is False
    assert "另一张图片" in conflicting["message"]
    with connect_database(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM batch_images").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM upload_receipts").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM upload_failures WHERE status = 'failed'"
        ).fetchone()[0] == 0
    assert len(
        [
            path
            for path in upload_dir.rglob("*")
            if path.is_file() and ".incoming" not in path.parts
        ]
    ) == 1


def test_json_retry_rejects_failure_id_count_mismatch(
    client, database_path: Path, png_bytes: bytes
) -> None:
    batch = create_batch(database_path)
    response = json_upload(
        client,
        batch["id"],
        [(png_bytes, "一.png"), (image_bytes((2, 3, 4)), "二.png")],
        ["one", "two"],
        ["123"],
    )
    assert response.status_code == 400
    assert "数量必须与文件数量一致" in response.get_json()["error"]
    with connect_database(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM upload_receipts").fetchone()[0] == 0


def test_failure_metadata_sync_is_idempotent_and_late_writes_cannot_reopen(
    client, database_path: Path
) -> None:
    success_first = create_batch(database_path)
    successful = json_upload(
        client,
        success_first["id"],
        [(image_bytes((120, 121, 122)), "先成功.png")],
        ["success-first"],
    ).get_json()["results"][0]
    assert successful["status"] == "added"
    late_payload = [
        {
            "client_id": "success-first",
            "name": "先成功.png",
            "size_bytes": 1234,
            "kind": "transport_unknown",
        }
    ]
    for _ in range(2):
        late = persist_failures(client, success_first["id"], late_payload).get_json()[
            "results"
        ][0]
        assert late["sync_status"] == "already_confirmed"
        assert late["failure_id"] is None
    assert client.get(f"/batches/{success_first['id']}/status").get_json()[
        "upload_failure_count"
    ] == 0

    failure_first = create_batch(database_path)
    pending_payload = [
        {
            "client_id": "failure-first",
            "name": "断网文件.png",
            "size_bytes": 4321,
            "kind": "transport_unknown",
        }
    ]
    first_write = persist_failures(client, failure_first["id"], pending_payload).get_json()[
        "results"
    ][0]
    duplicate_write = persist_failures(
        client, failure_first["id"], pending_payload
    ).get_json()["results"][0]
    assert first_write["sync_status"] == "saved"
    assert duplicate_write["sync_status"] == "saved"
    assert duplicate_write["failure_id"] == first_write["failure_id"]

    recovered = json_upload(
        client,
        failure_first["id"],
        [(image_bytes((130, 131, 132)), "恢复后重新选择.png")],
        ["failure-first"],
    ).get_json()["results"][0]
    assert recovered["status"] == "added"
    delayed_again = persist_failures(
        client, failure_first["id"], pending_payload
    ).get_json()["results"][0]
    assert delayed_again["sync_status"] == "already_confirmed"
    assert client.get(f"/batches/{failure_first['id']}/status").get_json()[
        "upload_failure_count"
    ] == 0
    with connect_database(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM upload_failures WHERE batch_id = ?",
            (failure_first["id"],),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT status FROM upload_failures WHERE batch_id = ?",
            (failure_first["id"],),
        ).fetchone()[0] == "resolved"


def test_failure_sync_and_successful_upload_race_ends_confirmed(
    app, database_path: Path
) -> None:
    batch = create_batch(database_path)
    barrier = threading.Barrier(2)
    content = image_bytes((140, 141, 142))

    def upload_now():
        with app.test_client() as thread_client:
            barrier.wait(timeout=5)
            return json_upload(
                thread_client,
                batch["id"],
                [(content, "并发恢复.png")],
                ["raced-client"],
            ).get_json()

    def persist_now():
        with app.test_client() as thread_client:
            barrier.wait(timeout=5)
            return persist_failures(
                thread_client,
                batch["id"],
                [
                    {
                        "client_id": "raced-client",
                        "name": "并发恢复.png",
                        "size_bytes": len(content),
                        "kind": "transport_unknown",
                    }
                ],
            ).get_json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        upload_future = executor.submit(upload_now)
        failure_future = executor.submit(persist_now)
        upload_payload = upload_future.result(timeout=15)
        failure_payload = failure_future.result(timeout=15)

    assert upload_payload["results"][0]["status"] in {
        "added",
        "already_in_batch",
    }
    assert failure_payload["results"][0]["sync_status"] in {
        "saved",
        "already_confirmed",
    }
    with connect_database(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM batch_images").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM upload_receipts").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM upload_failures WHERE status = 'failed'"
        ).fetchone()[0] == 0


def test_concurrent_different_uploads_for_one_client_leave_no_orphan(
    app,
    database_path: Path,
    upload_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = create_batch(database_path)
    staged_barrier = threading.Barrier(2)
    first_content = image_bytes((150, 151, 152))
    second_content = image_bytes((160, 161, 162))

    def stage_together(*args, **kwargs):
        staged = real_save_image_stream(*args, **kwargs)
        staged_barrier.wait(timeout=5)
        return staged

    monkeypatch.setattr(app_module, "save_image_stream", stage_together)

    def upload_now(content: bytes, name: str):
        with app.test_client() as thread_client:
            return json_upload(
                thread_client,
                batch["id"],
                [(content, name)],
                ["same-tab-client"],
            ).get_json()["results"][0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(upload_now, first_content, "标签页一.png")
        second_future = executor.submit(upload_now, second_content, "标签页二.png")
        results = [first_future.result(timeout=15), second_future.result(timeout=15)]

    assert sorted(result["status"] for result in results) == ["added", "failed"]
    failed = next(result for result in results if result["status"] == "failed")
    assert failed["retryable"] is False
    assert "另一张图片" in failed["message"]
    with connect_database(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM batch_images").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM upload_receipts").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM upload_failures WHERE status = 'failed'"
        ).fetchone()[0] == 0
    canonical = [
        path
        for path in upload_dir.rglob("*")
        if path.is_file() and ".incoming" not in path.parts
    ]
    incoming = list((upload_dir / ".incoming").glob("*.part"))
    assert len(canonical) == 1
    assert incoming == []


def test_database_error_rolls_back_new_canonical_file_before_unlock(
    app,
    database_path: Path,
    upload_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = create_batch(database_path)
    content = image_bytes((170, 171, 172))
    staged = real_save_image_stream(
        FileStorage(stream=io.BytesIO(content), filename="事务回滚.png"),
        upload_dir,
    )

    def fail_refresh(_connection, _batch_ids):
        raise RuntimeError("injected refresh failure")

    monkeypatch.setattr(database_module, "_refresh_batches", fail_refresh)
    try:
        with pytest.raises(RuntimeError, match="injected refresh failure"):
            attach_uploaded_evidence(
                database_path,
                batch_id=batch["id"],
                sha256=staged.sha256,
                stored_path=staged.relative_path,
                media_type=staged.media_type,
                size_bytes=staged.size_bytes,
                original_name=staged.original_name,
                upload_failure_client_id="rollback-client",
                finalize_storage=staged.finalize,
                rollback_storage=staged.rollback_finalized,
            )
    finally:
        staged.discard()

    with connect_database(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM batch_images").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM upload_receipts").fetchone()[0] == 0
    assert [
        path
        for path in upload_dir.rglob("*")
        if path.is_file() and ".incoming" not in path.parts
    ] == []
    assert list((upload_dir / ".incoming").glob("*.part")) == []


def test_failure_metadata_payload_is_validated_before_any_write(
    client, database_path: Path
) -> None:
    batch = create_batch(database_path)
    response = persist_failures(
        client,
        batch["id"],
        [
            {
                "client_id": "valid-first",
                "name": "一.png",
                "size_bytes": 10,
                "kind": "transport_unknown",
            },
            {
                "client_id": "invalid-second",
                "name": "二.png",
                "size_bytes": -1,
                "kind": "transport_unknown",
            },
        ],
    )
    assert response.status_code == 400
    with connect_database(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM upload_failures").fetchone()[0] == 0


def test_batch_page_exposes_explicit_retry_and_metadata_only_recovery_text(
    client, database_path: Path
) -> None:
    batch = create_batch(database_path)
    persist_failures(
        client,
        batch["id"],
        [
            {
                "client_id": "page-failure",
                "name": "页面失败.png",
                "size_bytes": 12,
                "kind": "transport_unknown",
            }
        ],
    )
    page = client.get(f"/batches/{batch['id']}")
    assert page.status_code == 200
    assert b"batch_upload_state.js" in page.data
    assert "不会根据文件名或大小自动认定关联".encode() in page.data
    assert "不保存图片或 File 对象".encode() in page.data
