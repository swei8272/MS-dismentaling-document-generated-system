from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import platform
import socket
import sys
import time
import tracemalloc
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.client import HTTPConnection
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def generate_images(output: Path, count: int, width: int, height: int) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        noise = Image.effect_noise((width, height), 38 + (index % 11))
        tint = ImageOps.colorize(
            noise,
            black=(index * 17 % 80, index * 29 % 80, index * 43 % 80),
            white=(175 + index % 80, 165 + index * 3 % 90, 185 + index * 7 % 70),
        )
        draw = ImageDraw.Draw(tint)
        draw.rectangle((30, 30, 560, 150), fill=(245, 245, 235), outline=(30, 30, 30), width=4)
        draw.text((55, 65), f"SYNTHETIC PHASE 2 IMAGE {index:04d}", fill=(10, 10, 10))
        draw.line((0, index * 37 % height, width, index * 71 % height), fill=(255, 90, 40), width=8)
        tint.save(output / f"synthetic-{index:04d}.jpg", format="JPEG", quality=84)


def inspect_images(output: Path) -> dict:
    paths = sorted(output.glob("*.jpg"))
    sizes = [path.stat().st_size for path in paths]
    hashes = set()
    dimensions = set()
    for path in paths:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        hashes.add(digest.hexdigest())
        with Image.open(path) as image:
            dimensions.add(image.size)
    return {
        "count": len(paths),
        "unique_sha256": len(hashes),
        "dimensions": sorted([list(item) for item in dimensions]),
        "minimum_bytes": min(sizes) if sizes else 0,
        "maximum_bytes": max(sizes) if sizes else 0,
        "average_bytes": round(sum(sizes) / len(sizes)) if sizes else 0,
        "total_bytes": sum(sizes),
    }


def utc_timestamp(epoch: float | None = None) -> str:
    value = datetime.fromtimestamp(epoch, timezone.utc) if epoch is not None else datetime.now(timezone.utc)
    return value.isoformat(timespec="milliseconds")


def process_memory_counters() -> dict[str, int | None]:
    if os.name != "nt":
        return {
            "process_working_set_bytes": None,
            "process_peak_working_set_bytes": None,
        }

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    get_current_process = ctypes.windll.kernel32.GetCurrentProcess
    get_current_process.restype = ctypes.c_void_p
    get_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
    get_memory_info.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ProcessMemoryCounters),
        ctypes.c_ulong,
    )
    get_memory_info.restype = ctypes.c_int
    handle = get_current_process()
    if not get_memory_info(
        handle, ctypes.byref(counters), counters.cb
    ):
        return {
            "process_working_set_bytes": None,
            "process_peak_working_set_bytes": None,
        }
    return {
        "process_working_set_bytes": int(counters.WorkingSetSize),
        "process_peak_working_set_bytes": int(counters.PeakWorkingSetSize),
    }


def serve_validation(root: Path, port: int) -> None:
    from flask import jsonify, request
    from waitress import serve

    from app import create_app
    from database import database_connection

    root.mkdir(parents=True, exist_ok=True)
    tracemalloc.start()
    app = create_app(
        {
            "DATABASE_PATH": root / "data" / "validation.db",
            "UPLOAD_DIR": root / "uploads",
            "SECRET_KEY": "phase2-synthetic-validation-only",
        }
    )

    @app.get("/validation/metrics")
    def validation_metrics():
        current, peak = tracemalloc.get_traced_memory()
        return jsonify(
            **process_memory_counters(),
            traced_current_bytes=current,
            traced_peak_bytes=peak,
        )

    @app.get("/validation/storage-summary")
    def validation_storage_summary():
        batch_id = request.args.get("batch_id", type=int)
        with database_connection(root / "data" / "validation.db") as connection:
            evidence_count = connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
            unique_sha256_count = connection.execute(
                "SELECT COUNT(DISTINCT sha256) FROM evidence"
            ).fetchone()[0]
            batch_image_count = connection.execute(
                "SELECT COUNT(*) FROM batch_images WHERE batch_id = ?", (batch_id,)
            ).fetchone()[0]
            unresolved_failure_count = connection.execute(
                """
                SELECT COUNT(*) FROM upload_failures
                WHERE batch_id = ? AND status = 'failed'
                """,
                (batch_id,),
            ).fetchone()[0]
            receipt_count = connection.execute(
                "SELECT COUNT(*) FROM upload_receipts WHERE batch_id = ?", (batch_id,)
            ).fetchone()[0]
        upload_root = root / "uploads"
        disk_file_count = sum(
            1
            for path in upload_root.rglob("*")
            if path.is_file() and ".incoming" not in path.parts
        )
        return jsonify(
            evidence_count=evidence_count,
            unique_sha256_count=unique_sha256_count,
            batch_image_count=batch_image_count,
            upload_receipt_count=receipt_count,
            unresolved_failure_count=unresolved_failure_count,
            disk_file_count=disk_file_count,
        )

    serve(app, host="127.0.0.1", port=port, threads=8)


def fetch_json(url: str) -> tuple[dict, float, int, str | None]:
    started = time.perf_counter()
    error = None
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            status = response.status
    except urllib.error.HTTPError as exc:
        payload = {}
        status = exc.code
        error = f"{type(exc).__name__}: {exc}"
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        payload = {}
        status = 0
        error = f"{type(exc).__name__}: {exc}"
    elapsed_ms = (time.perf_counter() - started) * 1000
    return payload, elapsed_ms, status, error


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(math.ceil(len(ordered) * fraction) - 1, 0)]


def monitor(
    base_url: str,
    batch_id: int,
    stop_file: Path,
    output: Path,
    commit_sha: str | None = None,
) -> None:
    samples = []
    started = time.time()
    while not stop_file.exists():
        sample_started = time.time()
        status_payload, latency_ms, http_status, status_error = fetch_json(
            f"{base_url}/batches/{batch_id}/status"
        )
        metrics_payload, _, metrics_status, metrics_error = fetch_json(
            f"{base_url}/validation/metrics"
        )
        samples.append(
            {
                "at": sample_started,
                "latency_ms": round(latency_ms, 3),
                "http_status": http_status,
                "status_error": status_error,
                "metrics_status": metrics_status,
                "metrics_error": metrics_error,
                "saved_total": status_payload.get("batch", {}).get("total"),
                **metrics_payload,
            }
        )
        time.sleep(max(0, 1 - (time.time() - sample_started)))
    latencies = [item["latency_ms"] for item in samples if item["http_status"] == 200]
    ended = time.time()
    report = {
        "validation_commit_sha": commit_sha,
        "environment": {
            "platform": platform.platform(),
            "python": sys.version,
            "monitor_client": "Python urllib",
        },
        "sampling_window": {
            "started_at_utc": utc_timestamp(started),
            "ended_at_utc": utc_timestamp(ended),
            "duration_seconds": round(ended - started, 3),
        },
        "sample_count": len(samples),
        "http_failures": sum(item["http_status"] != 200 for item in samples),
        "metrics_http_failures": sum(
            item["metrics_status"] != 200 for item in samples
        ),
        "latency_p95_ms": percentile(latencies, 0.95),
        "latency_max_ms": max(latencies) if latencies else None,
        "server_sampled_working_set_max_bytes": max(
            (item.get("process_working_set_bytes") or 0 for item in samples), default=0
        ),
        "server_process_lifetime_peak_working_set_bytes": max(
            (item.get("process_peak_working_set_bytes") or 0 for item in samples),
            default=0,
        ),
        "server_process_lifetime_peak_source": (
            "Windows GetProcessMemoryInfo PROCESS_MEMORY_COUNTERS.PeakWorkingSetSize"
        ),
        "server_traced_peak_bytes": max(
            (item.get("traced_peak_bytes") or 0 for item in samples), default=0
        ),
        "samples": samples,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def multipart_body(
    paths: list[Path],
    *,
    client_ids: list[str] | None = None,
    failure_ids: list[str] | None = None,
) -> tuple[bytes, str]:
    boundary = f"----dgm-phase2-{uuid.uuid4().hex}"
    parts: list[bytes] = []
    client_ids = client_ids or [f"synthetic-{index}-{path.stem}" for index, path in enumerate(paths)]
    failure_ids = failure_ids or [""] * len(paths)
    for index, path in enumerate(paths):
        parts.extend(
            (
                f"--{boundary}\r\n".encode(),
                (
                    'Content-Disposition: form-data; name="files"; '
                    f'filename="{path.name}"\r\nContent-Type: image/jpeg\r\n\r\n'
                ).encode(),
                path.read_bytes(),
                b"\r\n",
                f"--{boundary}\r\n".encode(),
                (
                    'Content-Disposition: form-data; name="client_ids"\r\n\r\n'
                    f"{client_ids[index]}\r\n"
                ).encode(),
                f"--{boundary}\r\n".encode(),
                (
                    'Content-Disposition: form-data; name="failure_ids"\r\n\r\n'
                    f"{failure_ids[index]}\r\n"
                ).encode(),
                f"--{boundary}\r\n".encode(),
                (
                    'Content-Disposition: form-data; name="file_sizes"\r\n\r\n'
                    f"{path.stat().st_size}\r\n"
                ).encode(),
            )
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def http_target(base_url: str) -> tuple[str, int]:
    parsed = urllib.parse.urlsplit(base_url)
    return parsed.hostname or "127.0.0.1", parsed.port or 80


def create_validation_batch(base_url: str) -> int:
    host, port = http_target(base_url)
    connection = HTTPConnection(host, port, timeout=10)
    body = urllib.parse.urlencode({"name": "第二阶段 500 张合成图片验证"})
    connection.request(
        "POST",
        "/batches/new",
        body=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    response = connection.getresponse()
    response.read()
    location = response.getheader("Location") or ""
    connection.close()
    if response.status != 303 or not location:
        raise RuntimeError(f"创建批次失败：HTTP {response.status}")
    return int(location.rstrip("/").split("/")[-1])


def send_multipart(base_url: str, batch_id: int, body: bytes, boundary: str) -> dict:
    host, port = http_target(base_url)
    connection = HTTPConnection(host, port, timeout=180)
    connection.request(
        "POST",
        f"/batches/{batch_id}/upload",
        body=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
            "Accept": "application/json",
            "X-Requested-With": "BatchUploader",
        },
    )
    response = connection.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    connection.close()
    if response.status != 200:
        raise RuntimeError(f"上传失败：HTTP {response.status}: {payload}")
    return payload


def send_partial_then_disconnect(base_url: str, batch_id: int, body: bytes, boundary: str) -> None:
    host, port = http_target(base_url)
    with socket.create_connection((host, port), timeout=10) as connection:
        headers = (
            f"POST /batches/{batch_id}/upload HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Content-Type: multipart/form-data; boundary={boundary}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Accept: application/json\r\n"
            "X-Requested-With: BatchUploader\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        connection.sendall(headers)
        connection.sendall(body[: len(body) // 2])


def send_complete_without_reading_response(
    base_url: str, batch_id: int, body: bytes, boundary: str
) -> None:
    host, port = http_target(base_url)
    connection = HTTPConnection(host, port, timeout=180)
    connection.request(
        "POST",
        f"/batches/{batch_id}/upload",
        body=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
            "Accept": "application/json",
            "X-Requested-With": "BatchUploader",
        },
    )
    connection.close()


def upload_images(
    base_url: str,
    image_dir: Path,
    group_size: int,
    output: Path,
    batch_id: int | None = None,
    commit_sha: str | None = None,
) -> None:
    paths = sorted(image_dir.glob("*.jpg"))
    if not paths:
        raise RuntimeError("没有找到合成 JPG 图片")
    batch_id = batch_id or create_validation_batch(base_url)
    groups = [paths[index : index + group_size] for index in range(0, len(paths), group_size)]
    counts = {"added": 0, "reused": 0, "already_in_batch": 0, "failed": 0}
    group_results = []
    tracemalloc.start()
    started_epoch = time.time()
    started = time.perf_counter()
    for group_index, group in enumerate(groups, start=1):
        body, boundary = multipart_body(group)
        simulated = None
        if group_index == 2:
            send_partial_then_disconnect(base_url, batch_id, body, boundary)
            simulated = "mid_request_disconnect_then_retry"
            time.sleep(1)
        if group_index == 3:
            send_complete_without_reading_response(base_url, batch_id, body, boundary)
            simulated = "response_lost_then_retry"
            time.sleep(2)
        group_started = time.perf_counter()
        payload = send_multipart(base_url, batch_id, body, boundary)
        statuses = [result["status"] for result in payload["results"]]
        for status in statuses:
            counts[status] = counts.get(status, 0) + 1
        group_results.append(
            {
                "group": group_index,
                "files": len(group),
                "payload_bytes": len(body),
                "elapsed_seconds": round(time.perf_counter() - group_started, 3),
                "simulated": simulated,
                "statuses": {status: statuses.count(status) for status in sorted(set(statuses))},
            }
        )
    elapsed = time.perf_counter() - started
    ended_epoch = time.time()
    _, client_traced_peak = tracemalloc.get_traced_memory()
    status_payload, _, _, _ = fetch_json(f"{base_url}/batches/{batch_id}/status")
    storage_payload, _, storage_status, _ = fetch_json(
        f"{base_url}/validation/storage-summary?batch_id={batch_id}"
    )
    report = {
        "validation_commit_sha": commit_sha,
        "environment": {
            "platform": platform.platform(),
            "python": sys.version,
            "server": "Waitress",
            "uploader": "Python HTTP validation client (not browser memory)",
        },
        "batch_id": batch_id,
        "upload_window": {
            "started_at_utc": utc_timestamp(started_epoch),
            "ended_at_utc": utc_timestamp(ended_epoch),
            "elapsed_seconds": round(elapsed, 3),
        },
        "image_inventory": inspect_images(image_dir),
        "grouping": {
            "group_max_files": group_size,
            "group_max_bytes": 64 * 1024 * 1024,
            "file_max_bytes": 16 * 1024 * 1024,
            "group_count": len(groups),
            "concurrent_requests": 1,
        },
        "observed_result_counts": counts,
        "server_batch": status_payload.get("batch"),
        "upload_failure_count": status_payload.get("upload_failure_count"),
        "storage_summary_http_status": storage_status,
        "storage_summary": storage_payload,
        "python_uploader_traced_peak_bytes": client_traced_peak,
        "browser_memory": None,
        "group_results": group_results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


def validate_mixed_failure(base_url: str, image_dir: Path, output: Path) -> None:
    valid = sorted(image_dir.glob("*.jpg"))[:3]
    if len(valid) != 3:
        raise RuntimeError("混合验证至少需要 3 张合成图片")
    corrupt = output.parent / "damaged-synthetic.jpg"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_bytes(b"this is deliberately not a valid image")
    batch_id = create_validation_batch(base_url)
    initial_ids = ["mixed-good-a", "mixed-damaged", "mixed-good-b"]
    body, boundary = multipart_body(
        [valid[0], corrupt, valid[1]], client_ids=initial_ids
    )
    send_partial_then_disconnect(base_url, batch_id, body, boundary)
    time.sleep(1)
    initial = send_multipart(base_url, batch_id, body, boundary)
    failed = next(item for item in initial["results"] if item["client_id"] == "mixed-damaged")
    retry_body, retry_boundary = multipart_body(
        [valid[2]],
        client_ids=["mixed-damaged"],
        failure_ids=[str(failed["failure_id"])],
    )
    retried = send_multipart(base_url, batch_id, retry_body, retry_boundary)
    status_payload, _, _, _ = fetch_json(f"{base_url}/batches/{batch_id}/status")
    report = {
        "batch_id": batch_id,
        "simulated": "mid_request_disconnect_then_retry",
        "initial_results": initial["results"],
        "retry_results": retried["results"],
        "final_batch": status_payload.get("batch"),
        "final_upload_failure_count": status_payload.get("upload_failure_count"),
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="第二阶段合成图片和 Waitress 验证辅助工具")
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate")
    generate.add_argument("output", type=Path)
    generate.add_argument("--count", type=int, default=500)
    generate.add_argument("--width", type=int, default=1600)
    generate.add_argument("--height", type=int, default=1200)
    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("output", type=Path)
    server = subparsers.add_parser("serve")
    server.add_argument("root", type=Path)
    server.add_argument("--port", type=int, default=5054)
    watch = subparsers.add_parser("monitor")
    watch.add_argument("base_url")
    watch.add_argument("batch_id", type=int)
    watch.add_argument("stop_file", type=Path)
    watch.add_argument("output", type=Path)
    watch.add_argument("--commit-sha")
    upload = subparsers.add_parser("upload")
    upload.add_argument("base_url")
    upload.add_argument("image_dir", type=Path)
    upload.add_argument("output", type=Path)
    upload.add_argument("--group-size", type=int, default=25)
    upload.add_argument("--batch-id", type=int)
    upload.add_argument("--commit-sha")
    create = subparsers.add_parser("create-batch")
    create.add_argument("base_url")
    mixed = subparsers.add_parser("mixed")
    mixed.add_argument("base_url")
    mixed.add_argument("image_dir", type=Path)
    mixed.add_argument("output", type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "generate":
        generate_images(args.output, args.count, args.width, args.height)
    elif args.command == "inspect":
        print(json.dumps(inspect_images(args.output), ensure_ascii=False, indent=2))
    elif args.command == "serve":
        serve_validation(args.root, args.port)
    elif args.command == "monitor":
        monitor(
            args.base_url,
            args.batch_id,
            args.stop_file,
            args.output,
            args.commit_sha,
        )
    elif args.command == "upload":
        upload_images(
            args.base_url,
            args.image_dir,
            args.group_size,
            args.output,
            args.batch_id,
            args.commit_sha,
        )
    elif args.command == "create-batch":
        print(create_validation_batch(args.base_url))
    elif args.command == "mixed":
        validate_mixed_failure(args.base_url, args.image_dir, args.output)


if __name__ == "__main__":
    main()
