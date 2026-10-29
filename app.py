"""Local-only Sisavoca vocabulary illustration batch application."""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import threading
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from pipeline import APIError, OpenAIClient, load_api_key, normalize_object_keys, process_item
from report_xlsx import write_report


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
DB_PATH = OUTPUT / "jobs.db"
SHEET = ROOT / "다람쥐 캐릭터 시트3.jpg"
STATIC = ROOT / "static"
MAX_UPLOAD = 5 * 1024 * 1024
MAX_ROWS = 1000
FINAL_STATUSES = {"generated", "revised", "skipped", "discarded", "error"}
_lock = threading.Lock()
_worker: threading.Thread | None = None


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@contextmanager
def connect():
    OUTPUT.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, original_name TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS items (
            job_id TEXT NOT NULL, row_number INTEGER NOT NULL, status TEXT NOT NULL,
            payload TEXT NOT NULL, PRIMARY KEY(job_id, row_number))""")
        db.execute("UPDATE jobs SET status='stopped', updated_at=? WHERE status IN ('running','stop_requested')", (now(),))


def parse_csv(data: bytes) -> list[dict]:
    try:
        source = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV는 UTF-8 인코딩이어야 합니다.") from exc
    reader = csv.DictReader(io.StringIO(source, newline=""))
    required = ("word", "meaning_ko")
    if not reader.fieldnames or not set(required).issubset(reader.fieldnames):
        raise ValueError("CSV에 word, meaning_ko 열이 필요합니다.")
    rows = []
    for number, row in enumerate(reader, 2):
        if len(rows) >= MAX_ROWS:
            raise ValueError(f"한 작업은 최대 {MAX_ROWS}행까지 처리할 수 있습니다.")
        item = {key: (row.get(key) or "").strip() for key in required}
        problems = []
        if None in row:
            problems.append("열 수가 헤더보다 많음")
        for key in required:
            if not item[key]:
                problems.append(f"{key} 누락")
        if any(len(item[key]) > 2000 for key in item):
            problems.append("셀 길이 2000자 초과")
        item.update(row_number=number, status="error" if problems else "queued", decision_reason="", scene="",
                    memory_cue="", visual_signal="", object_keys=[], object_reference_files=[],
                    use_squirrel="", use_text="", review_notes="",
                    selected_version="",
                    revision_count=0, final_reason="; ".join(problems), image_file="",
                    api_calls=0, input_tokens=0, output_tokens=0, image_tokens=0)
        rows.append(item)
    if not rows:
        raise ValueError("CSV에 처리할 단어가 없습니다.")
    return rows


def create_job(data: bytes, filename: str) -> str:
    if not SHEET.exists():
        raise ValueError("다람쥐 캐릭터 시트를 찾을 수 없습니다.")
    rows = parse_csv(data)
    job_id = uuid.uuid4().hex[:12]
    stamp = now()
    job_dir = OUTPUT / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "source.csv").write_bytes(data)
    with connect() as db:
        db.execute("INSERT INTO jobs VALUES (?,?,?,?,?)", (job_id, "queued", stamp, stamp, filename))
        db.executemany("INSERT INTO items VALUES (?,?,?,?)",
                       [(job_id, row["row_number"], row["status"], json.dumps(row, ensure_ascii=False)) for row in rows])
    refresh_report(job_id)
    return job_id


def get_job(job_id: str) -> tuple[dict, list[dict]] | None:
    with connect() as db:
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            return None
        items = db.execute("SELECT payload FROM items WHERE job_id=? ORDER BY row_number", (job_id,)).fetchall()
    return dict(job), [json.loads(row["payload"]) for row in items]


def set_job_status(job_id: str, status: str) -> None:
    with connect() as db:
        db.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (status, now(), job_id))


def save_item(job_id: str, item: dict) -> None:
    with connect() as db:
        db.execute("UPDATE items SET status=?, payload=? WHERE job_id=? AND row_number=?",
                   (item["status"], json.dumps(item, ensure_ascii=False), job_id, item["row_number"]))
        db.execute("UPDATE jobs SET updated_at=? WHERE id=?", (now(), job_id))


def known_object_keys() -> list[str]:
    """Recent reusable prop names from completed jobs, for the planning prompt."""
    with connect() as db:
        rows = db.execute("SELECT payload FROM items WHERE status IN ('generated','revised') "
                          "ORDER BY rowid DESC").fetchall()
    keys = []
    for row in rows:
        for key in normalize_object_keys(json.loads(row["payload"]).get("object_keys")):
            if key not in keys:
                keys.append(key)
            if len(keys) == 100:
                return keys
    return keys


def find_object_references(keys: list[str]) -> list[tuple[str, str, bytes]]:
    """Use the earliest delivered PNG for each matching prop as its stable design reference."""
    wanted = normalize_object_keys(keys)
    if not wanted:
        return []
    with connect() as db:
        rows = db.execute("SELECT job_id, payload FROM items WHERE status IN ('generated','revised') "
                          "ORDER BY rowid ASC").fetchall()
    found = []
    used_files = set()
    matched_keys = set()
    for row in rows:
        item = json.loads(row["payload"])
        matching = [key for key in wanted if key not in matched_keys and
                    key in normalize_object_keys(item.get("object_keys"))]
        filename = item.get("image_file", "")
        if not matching or not filename or Path(filename).name != filename:
            continue
        path = OUTPUT / row["job_id"] / filename
        if not path.is_file():
            continue
        source = f"{row['job_id']}/{filename}"
        if source not in used_files:
            found.append((", ".join(matching), source, path.read_bytes()))
            used_files.add(source)
            matched_keys.update(matching)
        if len(found) == 2:
            break
    return found


def refresh_report(job_id: str) -> None:
    job, items = get_job(job_id) or (None, [])
    if job:
        for item in items:
            if item["status"] in ("generated", "revised") and not (OUTPUT / job_id / item["image_file"]).is_file():
                item.update(status="error", final_reason="납품 PNG 파일이 없어 보고서에서 제외됨", image_file="")
                save_item(job_id, item)
        job, items = get_job(job_id) or (None, [])
        write_report(OUTPUT / job_id / "work_report.xlsx", job, items)


def worker(job_id: str) -> None:
    global _worker
    try:
        key = load_api_key(ROOT / ".env")
        client = OpenAIClient(key)
        set_job_status(job_id, "running")
        _, items = get_job(job_id) or (None, [])
        for item in items:
            current, _ = get_job(job_id) or (None, [])
            if not current or current["status"] == "stop_requested":
                set_job_status(job_id, "stopped")
                break
            if item["status"] in FINAL_STATUSES:
                continue
            # An interrupted in-flight row is retried from planning; completed rows are never billed again.
            try:
                process_item(item, client, SHEET, OUTPUT / job_id,
                             lambda value: save_item(job_id, value),
                             find_object_references, known_object_keys())
            except Exception as exc:
                item.update(status="error", final_reason=str(exc)[:500])
                save_item(job_id, item)
            refresh_report(job_id)
        else:
            set_job_status(job_id, "completed")
        refresh_report(job_id)
    except Exception as exc:
        set_job_status(job_id, "error")
        _, items = get_job(job_id) or (None, [])
        for item in items:
            if item["status"] not in FINAL_STATUSES:
                item.update(status="error", final_reason=str(exc)[:500])
                save_item(job_id, item)
        refresh_report(job_id)
    finally:
        with _lock:
            _worker = None


def start_worker(job_id: str) -> bool:
    global _worker
    with _lock:
        if _worker and _worker.is_alive():
            return False
        _worker = threading.Thread(target=worker, args=(job_id,), daemon=True)
        _worker.start()
    return True


class Handler(BaseHTTPRequestHandler):
    server_version = "SisavocaLocal/1.0"

    def log_message(self, format: str, *args) -> None:
        # Do not log request bodies, prompts, API keys, or arbitrary error detail.
        print("[%s] %s" % (self.log_date_time_string(), format % args))

    def json_response(self, status: int, payload: dict | list) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def file_response(self, path: Path, mime: str, attachment: bool = False) -> None:
        if not path.is_file():
            return self.json_response(404, {"error": "파일이 없습니다."})
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if attachment:
            self.send_header("Content-Disposition",
                             f"attachment; filename=illustration.png; filename*=UTF-8''{quote(path.name)}")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parts = [unquote(x) for x in urlparse(self.path).path.strip("/").split("/")]
        if parts == [""]:
            return self.file_response(STATIC / "index.html", "text/html; charset=utf-8")
        if parts == ["api", "jobs"]:
            with connect() as db:
                rows = db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 30").fetchall()
            return self.json_response(200, [dict(row) for row in rows])
        if len(parts) >= 3 and parts[:2] == ["api", "jobs"] and len(parts[2]) == 12:
            found = get_job(parts[2])
            if not found:
                return self.json_response(404, {"error": "작업을 찾을 수 없습니다."})
            job, items = found
            if len(parts) == 3:
                return self.json_response(200, {"job": job, "items": items})
            if len(parts) == 4 and parts[3] == "report":
                return self.file_response(OUTPUT / job["id"] / "work_report.xlsx",
                                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", True)
            if len(parts) == 4 and parts[3] == "images.zip":
                memory = io.BytesIO()
                with zipfile.ZipFile(memory, "w", zipfile.ZIP_DEFLATED) as archive:
                    for item in items:
                        if item["status"] in ("generated", "revised") and item["image_file"]:
                            path = OUTPUT / job["id"] / item["image_file"]
                            if path.is_file():
                                archive.write(path, path.name)
                data = memory.getvalue()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{job["id"]}_images.zip"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            if len(parts) == 5 and parts[3] == "images":
                filename = parts[4]
                allowed = {i["image_file"] for i in items if i["status"] in ("generated", "revised")}
                if filename in allowed and filename == Path(filename).name:
                    return self.file_response(OUTPUT / job["id"] / filename, "image/png", True)
        return self.json_response(404, {"error": "경로를 찾을 수 없습니다."})

    def do_POST(self) -> None:
        parts = urlparse(self.path).path.strip("/").split("/")
        if parts == ["api", "jobs"]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length < 1 or length > MAX_UPLOAD:
                return self.json_response(400, {"error": "CSV는 5MB 이하여야 합니다."})
            filename = self.headers.get("X-Filename", "vocabulary.csv")[:150]
            try:
                key = load_api_key(ROOT / ".env")
                if not key:
                    raise APIError("API 키가 없습니다.")
                job_id = create_job(self.rfile.read(length), filename)
            except (ValueError, APIError) as exc:
                return self.json_response(400, {"error": str(exc)})
            if not start_worker(job_id):
                return self.json_response(409, {"error": "다른 작업이 실행 중입니다.", "job_id": job_id})
            return self.json_response(201, {"job_id": job_id})
        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and len(parts[2]) == 12:
            job_id = parts[2]
            found = get_job(job_id)
            if not found:
                return self.json_response(404, {"error": "작업을 찾을 수 없습니다."})
            job, _ = found
            if parts[3] == "stop" and job["status"] == "running":
                set_job_status(job_id, "stop_requested")
                return self.json_response(200, {"status": "stop_requested"})
            if parts[3] == "resume" and job["status"] in ("stopped", "queued"):
                if start_worker(job_id):
                    return self.json_response(200, {"status": "running"})
                return self.json_response(409, {"error": "다른 작업이 실행 중입니다."})
        return self.json_response(400, {"error": "사용할 수 없는 요청입니다."})


def main() -> None:
    init_db()
    port = int(os.environ.get("SISAVOCA_PORT", "8765"))
    print(f"시사보카 삽화 제작기: http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
