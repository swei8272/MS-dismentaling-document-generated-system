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
    MAX_CONTENT_LENGTH = 512 * 1024 * 1024
    SECRET_KEY = os.environ.get("DGM_SECRET_KEY") or secrets.token_hex(32)
