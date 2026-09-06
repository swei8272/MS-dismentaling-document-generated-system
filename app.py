from __future__ import annotations

from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, url_for

from config import Config
from database import (
    attach_uploaded_evidence,
    create_batch,
    get_batch,
    get_batch_with_stats,
    list_batch_images,
    list_batches,
    migrate_database,
    refresh_batch,
    set_batch_uploading,
)
from storage import UploadValidationError, save_image_stream


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
        images = list_batch_images(_database_path(app), batch_id)
        return render_template("batch_detail.html", batch=batch, images=images)

    @app.post("/batches/<int:batch_id>/upload")
    def batch_upload(batch_id: int):
        if get_batch(_database_path(app), batch_id) is None:
            abort(404)
        items = request.files.getlist("files") or request.files.getlist("files[]")
        if not items:
            single = request.files.get("file")
            items = [single] if single else []
        if not items:
            flash("请选择至少一张图片。", "error")
            return redirect(url_for("batch_detail", batch_id=batch_id), code=303)

        set_batch_uploading(_database_path(app), batch_id)
        added = reused = skipped = 0
        for item in items:
            if item is None:
                continue
            try:
                stored = save_image_stream(item, app.config["UPLOAD_DIR"])
                _, evidence_created, relation_created = attach_uploaded_evidence(
                    _database_path(app),
                    batch_id=batch_id,
                    sha256=stored.sha256,
                    stored_path=stored.relative_path,
                    media_type=stored.media_type,
                    size_bytes=stored.size_bytes,
                    original_name=stored.original_name,
                )
                if not relation_created:
                    skipped += 1
                elif evidence_created:
                    added += 1
                else:
                    reused += 1
            except (UploadValidationError, OSError, ValueError) as exc:
                skipped += 1
                flash(f"{item.filename or '未命名文件'}：{exc}", "error")
            except Exception:
                skipped += 1
                app.logger.exception("保存上传图片失败")
                flash(f"{item.filename or '未命名文件'}：保存失败，请重试。", "error")
        refresh_batch(_database_path(app), batch_id)
        flash(
            f"上传完成：新增 {added} 张，复用 {reused} 张，跳过 {skipped} 张。",
            "success" if skipped == 0 else "warning",
        )
        return redirect(url_for("batch_detail", batch_id=batch_id), code=303)

    @app.route("/upload", methods=["GET", "POST"])
    def legacy_upload():
        flash("请先创建批次，再上传图片。", "warning")
        return redirect(url_for("batch_new"), code=303)

    return app


def _database_path(app: Flask) -> Path:
    return Path(app.config["DATABASE_PATH"])


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=False)
