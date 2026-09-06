from __future__ import annotations

import uuid
from pathlib import Path

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

from config import Config
from database import (
    attach_uploaded_evidence,
    count_batch_images,
    count_upload_failures,
    create_batch,
    database_connection,
    get_batch,
    get_batch_with_stats,
    list_batch_images_page,
    list_batches,
    list_upload_failures,
    migrate_database,
    record_upload_failure,
    refresh_batch,
    set_batch_uploading,
)
from storage import UploadValidationError, display_filename, save_image_stream


STATUS_LABELS = {
    "created": "已创建",
    "uploading": "上传中",
    "queued": "排队中",
    "processing": "处理中",
    "completed": "已完成",
    "pending": "待确认",
    "failed": "失败",
    "needs_review": "需要复核",
    "partial_failed": "部分失败",
}


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)

    app.config["DATABASE_PATH"] = Path(app.config["DATABASE_PATH"])
    app.config["UPLOAD_DIR"] = Path(app.config["UPLOAD_DIR"])
    migrate_database(app.config["DATABASE_PATH"])
    app.config["UPLOAD_DIR"].mkdir(parents=True, exist_ok=True)
    app.jinja_env.globals["status_label"] = STATUS_LABELS

    @app.get("/")
    def index():
        return redirect(url_for("batch_list"))

    @app.get("/batches")
    def batch_list():
        return render_template("batches.html", batches=list_batches(_database_path(app)))

    @app.route("/batches/new", methods=["GET", "POST"])
    def batch_new():
        if request.method == "POST":
            batch = create_batch(_database_path(app), request.form.get("name"))
            flash(f"已创建批次 {batch['batch_no']}。", "success")
            return redirect(url_for("batch_detail", batch_id=batch["id"]), code=303)
        return render_template("batch_new.html")

    @app.get("/batches/<int:batch_id>")
    def batch_detail(batch_id: int):
        batch = get_batch_with_stats(_database_path(app), batch_id)
        if batch is None:
            abort(404)
        page_size = int(app.config["BATCH_IMAGE_PAGE_SIZE"])
        page = max(request.args.get("page", 1, type=int), 1)
        image_total = count_batch_images(_database_path(app), batch_id)
        page_count = max((image_total + page_size - 1) // page_size, 1)
        page = min(page, page_count)
        images = list_batch_images_page(
            _database_path(app), batch_id, limit=page_size, offset=(page - 1) * page_size
        )
        upload_failures = list_upload_failures(_database_path(app), batch_id)
        return render_template(
            "batch_detail.html",
            batch=batch,
            images=images,
            upload_failures=upload_failures,
            upload_failure_count=count_upload_failures(_database_path(app), batch_id),
            page=page,
            page_count=page_count,
            uploader_config={
                "group_max_files": app.config["UPLOAD_GROUP_MAX_FILES"],
                "group_max_bytes": app.config["UPLOAD_GROUP_MAX_BYTES"],
                "file_max_bytes": app.config["UPLOAD_FILE_MAX_BYTES"],
                "max_auto_retries": app.config["UPLOAD_MAX_AUTO_RETRIES"],
                "poll_ms": app.config["BATCH_STATUS_POLL_MS"],
            },
        )

    @app.get("/batches/<int:batch_id>/status")
    def batch_status(batch_id: int):
        batch = get_batch_with_stats(_database_path(app), batch_id)
        if batch is None:
            abort(404)
        return jsonify(
            batch=dict(batch),
            upload_failure_count=count_upload_failures(_database_path(app), batch_id),
            upload_failures=[
                dict(row) for row in list_upload_failures(_database_path(app), batch_id, limit=25)
            ],
        )

    @app.get("/batches/<int:batch_id>/images")
    def batch_images(batch_id: int):
        if get_batch(_database_path(app), batch_id) is None:
            abort(404)
        page = max(request.args.get("page", 1, type=int), 1)
        per_page = min(max(request.args.get("per_page", 50, type=int), 1), 100)
        total = count_batch_images(_database_path(app), batch_id)
        return jsonify(
            items=[
                dict(row)
                for row in list_batch_images_page(
                    _database_path(app),
                    batch_id,
                    limit=per_page,
                    offset=(page - 1) * per_page,
                )
            ],
            page=page,
            per_page=per_page,
            total=total,
        )

    @app.post("/batches/<int:batch_id>/upload-failures")
    def batch_upload_failures(batch_id: int):
        if get_batch(_database_path(app), batch_id) is None:
            abort(404)
        payload = request.get_json(silent=True) or {}
        items = payload.get("items")
        if not isinstance(items, list) or len(items) > 1_000:
            return jsonify(error="失败记录格式无效。"), 400
        seen: set[str] = set()
        results = []
        for item in items:
            if not isinstance(item, dict):
                return jsonify(error="失败记录格式无效。"), 400
            client_id = str(item.get("client_id") or "").strip()
            if not client_id or len(client_id) > 128 or client_id in seen:
                return jsonify(error="每个文件必须使用请求内唯一标识。"), 400
            seen.add(client_id)
            size_bytes = item.get("size_bytes")
            if type(size_bytes) is not int or size_bytes < 0:
                return jsonify(error="文件大小无效。"), 400
            if size_bytes > int(app.config["UPLOAD_FILE_MAX_BYTES"]):
                reason = _file_limit_message(int(app.config["UPLOAD_FILE_MAX_BYTES"]))
                retryable = False
            elif item.get("kind") == "transport_unknown":
                reason = "网络连接中断，服务端结果未确认，请安全重传。"
                retryable = True
            else:
                reason = "浏览器未上传该文件，请重新选择后重试。"
                retryable = False
            failure_id = record_upload_failure(
                _database_path(app),
                batch_id=batch_id,
                client_id=client_id,
                original_name=display_filename(str(item.get("name") or "")),
                size_bytes=size_bytes,
                reason=reason,
                retryable=retryable,
            )
            results.append({"client_id": client_id, "failure_id": failure_id, "reason": reason})
        return jsonify(results=results)

    @app.post("/batches/<int:batch_id>/upload")
    def batch_upload(batch_id: int):
        if get_batch(_database_path(app), batch_id) is None:
            abort(404)
        items = request.files.getlist("files") or request.files.getlist("files[]")
        if not items:
            single = request.files.get("file")
            items = [single] if single else []
        if not items:
            if _json_requested():
                return jsonify(error="请选择至少一张图片。"), 400
            flash("请选择至少一张图片。", "error")
            return redirect(url_for("batch_detail", batch_id=batch_id), code=303)

        client_ids = request.form.getlist("client_ids")
        failure_ids = request.form.getlist("failure_ids")
        declared_sizes = request.form.getlist("file_sizes")
        if _json_requested():
            client_ids = [value.strip() for value in client_ids]
            if (
                len(client_ids) != len(items)
                or any(not value or len(value) > 128 for value in client_ids)
                or len(set(client_ids)) != len(client_ids)
            ):
                return jsonify(error="每个文件必须使用请求内唯一标识。"), 400
        else:
            client_ids = [f"legacy-{uuid.uuid4().hex}" for _item in items]
        if len(failure_ids) != len(items):
            failure_ids = [""] * len(items)
        if len(declared_sizes) != len(items):
            declared_sizes = [""] * len(items)

        set_batch_uploading(_database_path(app), batch_id)
        added = reused = skipped = 0
        group_bytes = 0
        results = []
        try:
            for index, item in enumerate(items):
                if item is None:
                    continue
                client_id = client_ids[index]
                original_name = display_filename(item.filename)
                reported_size = _nonnegative_int_or_none(declared_sizes[index])
                try:
                    if index >= int(app.config["UPLOAD_GROUP_MAX_FILES"]):
                        raise UploadValidationError(
                            f"每组最多上传 {app.config['UPLOAD_GROUP_MAX_FILES']} 张图片"
                        )
                    stored = save_image_stream(
                        item,
                        app.config["UPLOAD_DIR"],
                        max_file_bytes=int(app.config["UPLOAD_FILE_MAX_BYTES"]),
                        remaining_group_bytes=int(app.config["UPLOAD_GROUP_MAX_BYTES"])
                        - group_bytes,
                    )
                    group_bytes += stored.size_bytes
                    failure_id = _positive_int_or_none(failure_ids[index])
                    evidence_id, evidence_created, relation_created = attach_uploaded_evidence(
                        _database_path(app),
                        batch_id=batch_id,
                        sha256=stored.sha256,
                        stored_path=stored.relative_path,
                        media_type=stored.media_type,
                        size_bytes=stored.size_bytes,
                        original_name=stored.original_name,
                        upload_failure_id=failure_id,
                        upload_failure_client_id=client_id,
                    )
                    if not relation_created:
                        status = "already_in_batch"
                        skipped += 1
                        message = "服务端已保存，本批次无需重复添加。"
                    elif evidence_created:
                        status = "added"
                        added += 1
                        message = "已保存并进入 OCR 等待队列。"
                    else:
                        status = "reused"
                        reused += 1
                        message = "已复用全局图片记录。"
                    results.append(
                        {
                            "client_id": client_id,
                            "name": stored.original_name,
                            "status": status,
                            "message": message,
                            "retryable": False,
                            "evidence_id": evidence_id,
                            "processing_status": _evidence_status(
                                _database_path(app), evidence_id
                            ),
                        }
                    )
                except UploadValidationError as exc:
                    skipped += 1
                    reason = str(exc)
                    failure_id = record_upload_failure(
                        _database_path(app),
                        batch_id=batch_id,
                        client_id=client_id,
                        original_name=original_name,
                        size_bytes=reported_size if reported_size is not None else _known_size(item),
                        reason=reason,
                        retryable=False,
                    )
                    results.append(
                        _failed_result(client_id, original_name, reason, False, failure_id)
                    )
                except Exception:
                    skipped += 1
                    app.logger.exception("保存上传图片失败")
                    reason = "保存失败，请稍后重试。"
                    failure_id = record_upload_failure(
                        _database_path(app),
                        batch_id=batch_id,
                        client_id=client_id,
                        original_name=original_name,
                        size_bytes=reported_size if reported_size is not None else _known_size(item),
                        reason=reason,
                        retryable=True,
                    )
                    results.append(
                        _failed_result(client_id, original_name, reason, True, failure_id)
                    )
        finally:
            refresh_batch(_database_path(app), batch_id)

        if _json_requested():
            return jsonify(
                results=results,
                summary={"added": added, "reused": reused, "failed_or_existing": skipped},
                batch=dict(get_batch_with_stats(_database_path(app), batch_id)),
                upload_failure_count=count_upload_failures(_database_path(app), batch_id),
            )
        flash(
            f"上传完成：新增 {added} 张，复用 {reused} 张，跳过 {skipped} 张。",
            "success" if skipped == 0 else "warning",
        )
        return redirect(url_for("batch_detail", batch_id=batch_id), code=303)

    @app.route("/upload", methods=["GET", "POST"])
    def legacy_upload():
        flash("请先创建批次，再上传图片。", "warning")
        return redirect(url_for("batch_new"), code=303)

    @app.errorhandler(413)
    def request_too_large(_error):
        message = "本组请求过大，请减小分组后重试。"
        if _json_requested():
            return jsonify(error=message, retryable=False), 413
        flash(message, "error")
        batch_id = (request.view_args or {}).get("batch_id")
        target = url_for("batch_detail", batch_id=batch_id) if batch_id else url_for("batch_list")
        return redirect(target, code=303)

    return app


def _database_path(app: Flask) -> Path:
    return Path(app.config["DATABASE_PATH"])


def _json_requested() -> bool:
    return (
        request.headers.get("X-Requested-With") == "BatchUploader"
        or request.accept_mimetypes.best == "application/json"
    )


def _known_size(item) -> int | None:
    size = getattr(item, "content_length", None)
    return size if type(size) is int and size >= 0 else None


def _positive_int_or_none(value: str) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _nonnegative_int_or_none(value: str) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _file_limit_message(limit: int) -> str:
    return f"单个文件不能超过 {limit / (1024 * 1024):g} MiB"


def _failed_result(
    client_id: str, name: str, reason: str, retryable: bool, failure_id: int
) -> dict:
    return {
        "client_id": client_id,
        "name": name,
        "status": "failed",
        "message": reason,
        "retryable": retryable,
        "failure_id": failure_id,
    }


def _evidence_status(database_path: Path, evidence_id: int) -> str:
    with database_connection(database_path) as connection:
        return connection.execute(
            "SELECT processing_status FROM evidence WHERE id = ?", (evidence_id,)
        ).fetchone()[0]


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=False)
