from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import create_app


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "test.db"


@pytest.fixture
def upload_dir(tmp_path: Path) -> Path:
    return tmp_path / "uploads"


@pytest.fixture
def app(database_path: Path, upload_dir: Path):
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-only",
            "DATABASE_PATH": database_path,
            "UPLOAD_DIR": upload_dir,
        }
    )


@pytest.fixture
def client(app):
    return app.test_client()


def image_bytes(color: tuple[int, int, int] = (20, 90, 160)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (12, 8), color=color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def png_bytes() -> bytes:
    return image_bytes()
