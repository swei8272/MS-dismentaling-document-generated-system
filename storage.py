from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePath

from PIL import Image, UnidentifiedImageError
from werkzeug.datastructures import FileStorage


CHUNK_SIZE = 1024 * 1024
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
FORMAT_EXTENSIONS = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "BMP": ".bmp",
    "TIFF": ".tiff",
}


class UploadValidationError(ValueError):
    pass


@dataclass(frozen=True)
class StoredUpload:
    sha256: str
    relative_path: str
    media_type: str
    size_bytes: int
    original_name: str


def display_filename(filename: str | None) -> str:
    name = (filename or "").replace("\\", "/")
    clean = PurePath(name).name.strip()
    return clean[:255] or "未命名图片"


def _mib_text(size: int) -> str:
    return f"{size / (1024 * 1024):g} MiB"


def save_image_stream(
    upload: FileStorage,
    upload_root: str | Path,
    *,
    max_file_bytes: int | None = None,
    remaining_group_bytes: int | None = None,
) -> StoredUpload:
    original_name = display_filename(upload.filename)
    extension = Path(original_name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise UploadValidationError(f"不支持的图片扩展名：{extension or '无'}")

    root = Path(upload_root)
    incoming = root / ".incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    temporary = incoming / f"{uuid.uuid4().hex}.part"
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("xb") as output:
            while True:
                chunk = upload.stream.read(CHUNK_SIZE)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
                if max_file_bytes is not None and size > max_file_bytes:
                    raise UploadValidationError(
                        f"单个文件不能超过 {_mib_text(max_file_bytes)}"
                    )
                if remaining_group_bytes is not None and size > remaining_group_bytes:
                    raise UploadValidationError("本组图片总大小超过服务端限制，请减小分组")
        if size == 0:
            raise UploadValidationError("图片文件为空")
        try:
            with Image.open(temporary) as image:
                image_format = (image.format or "").upper()
                image.verify()
        except (UnidentifiedImageError, OSError) as exc:
            raise UploadValidationError("文件内容不是有效图片") from exc
        if image_format not in FORMAT_EXTENSIONS:
            raise UploadValidationError(f"不支持的图片格式：{image_format or '未知'}")

        sha256 = digest.hexdigest()
        canonical_extension = FORMAT_EXTENSIONS[image_format]
        relative = Path(sha256[:2]) / f"{sha256}{canonical_extension}"
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            temporary.unlink()
        else:
            os.replace(temporary, destination)
        media_type = Image.MIME.get(image_format, f"image/{image_format.lower()}")
        return StoredUpload(
            sha256=sha256,
            relative_path=relative.as_posix(),
            media_type=media_type,
            size_bytes=size,
            original_name=original_name,
        )
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
