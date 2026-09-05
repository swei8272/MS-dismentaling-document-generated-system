from __future__ import annotations

import argparse
from pathlib import Path

from config import DATABASE_PATH
from database import claim_next_queued_evidence, migrate_database, requeue_stale_processing_jobs


def self_check(database_path: str | Path = DATABASE_PATH) -> int:
    """Validate queue access without claiming or processing business images."""
    migrate_database(database_path)
    recovered = requeue_stale_processing_jobs(database_path)
    if not callable(claim_next_queued_evidence):
        raise RuntimeError("任务领取接口不可用")
    print(f"任务队列接口正常；恢复超时任务 {recovered} 个。")
    print("第一阶段未启用 OCR，Worker 已安全退出。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="铭深文件生成系统 Worker 骨架")
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    args = parser.parse_args()
    return self_check(args.database)


if __name__ == "__main__":
    raise SystemExit(main())
