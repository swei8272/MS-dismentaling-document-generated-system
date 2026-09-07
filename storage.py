from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass, field
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


class UploadIdentityError(UploadValidationError):
    """Raised before canonical storage when an idempotency key names other content."""


@dataclass
class StoredUpload:
    sha256: str
    relative_path: str
    media_type: str
    size_bytes: int
    original_name: str
    temporary_path: Path = field(repr=False)
    upload_root: Path = field(repr=False)
    _finalized_path: Path | None = field(default=None, init=False, repr=False)
    _created_destination: bool = field(default=False, init=False, repr=False)

    def finalize(self) -> str:
        """Move verified bytes to canonical storage exactly once.

        The database layer calls this while it holds its write transaction, after
        revalidating the client id/failure receipt.  Keeping the file staged until
        that point prevents a losing concurrent request from leaving an orphan.
        """
        if self._finalized_path is not None:
            return self.relative_path
        destination = self.upload_root / Path(self.relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            self.temporary_path.unlink(missing_ok=True)
        else:
            os.replace(self.temporary_path, destination)
            self._created_destination = True
        self._finalized_path = destination
        return self.relative_path

    def rollback_finalized(self) -> None:
        """Remove only a canonical file created by this uncommitted request."""
        if self._created_destination and self._finalized_path is not None:
            self._finalized_path.unlink(missing_ok=True)
        self._created_destination = False
        self._finalized_path = None

    def discard(self) -> None:
        """Discard bytes that never became the committed canonical file."""
        self.temporary_path.unlink(missing_ok=True)


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
    expected_sha256: str | None = None,
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
        if expected_sha256 is not None and sha256 != expected_sha256:
            raise UploadIdentityError(
                "该上传标识已经确认过另一张图片，不能重新关联。"
            )
        canonical_extension = FORMAT_EXTENSIONS[image_format]
        relative = Path(sha256[:2]) / f"{sha256}{canonical_extension}"
        media_type = Image.MIME.get(image_format, f"image/{image_format.lower()}")
        return StoredUpload(
            sha256=sha256,
            relative_path=relative.as_posix(),
            media_type=media_type,
            size_bytes=size,
            original_name=original_name,
            temporary_path=temporary,
            upload_root=root,
        )
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
