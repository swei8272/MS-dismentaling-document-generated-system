import os
import secrets
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
DATABASE_PATH = DATA_DIR / "vehicles.db"
UPLOAD_DIR = DATA_DIR / "uploads"


class Config:
    DATABASE_PATH = DATABASE_PATH
    UPLOAD_DIR = UPLOAD_DIR
    UPLOAD_GROUP_MAX_FILES = 25
    UPLOAD_GROUP_MAX_BYTES = 64 * 1024 * 1024
    UPLOAD_FILE_MAX_BYTES = 16 * 1024 * 1024
    # Leave multipart headers and boundaries above the 64 MiB file-content cap.
    MAX_CONTENT_LENGTH = 72 * 1024 * 1024
    BATCH_IMAGE_PAGE_SIZE = 50
    UPLOAD_MAX_AUTO_RETRIES = 2
    BATCH_STATUS_POLL_MS = 2_000
    SECRET_KEY = os.environ.get("DGM_SECRET_KEY") or secrets.token_hex(32)
