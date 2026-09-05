from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import html
import hmac
import json
import logging
import msvcrt
from logging.handlers import RotatingFileHandler
import mimetypes
import os
import secrets
import socket
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from http.client import HTTPConnection
from http import HTTPStatus
from http.cookies import SimpleCookie
from pathlib import Path
from socketserver import ThreadingMixIn
from threading import BoundedSemaphore, Lock
from urllib.parse import parse_qs, urlparse
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer


# Deployment knobs. The deadline is intentionally easy to find and change.
DEADLINE = datetime(2026, 9, 29, 12, 0, 0)
BASE_DIR = Path(__file__).resolve().parent
CSV_PATH = BASE_DIR / "users.csv"
COLLEGE_MAJORS_PATH = BASE_DIR / "college_majors.json"
DATABASE_PATH = BASE_DIR / "data" / "survey.sqlite3"
LOG_PATH = BASE_DIR / "logs" / "app.log"
CSV_HAS_HEADER = True
USE_LOCAL_USERS_CSV =True
REMOTE_USERS_CSV_SERVER = "45.125.44.183:10090"
REMOTE_USERS_CSV_SHARED_SECRET = "REMOTE_USERS_CSV_SHARED_SECRET"
REMOTE_AUTH_TIMEOUT_SECONDS = 5
REMOTE_AUTH_CLOCK_SKEW_SECONDS = 30
SESSION_TTL = timedelta(minutes=30)
RATE_WINDOW_SECONDS = 60
RATE_LIMIT = 20
REPEAT_WINDOW_SECONDS = 10
SUBMISSION_WINDOW_SECONDS = 60
SUBMISSION_LIMIT = 3
REQUEST_TIMEOUT_SECONDS = 30
MAX_REQUEST_THREADS = 32

DEPARTMENTS = [
    "设备服务站 - 硬件部",
    "网络技术站 - 网络部",
    "基础事务站 - 办公室",
    "基础事务站 - 美工部",
    "基础事务站 - 推广部",
]

CAMPUS_BY_COLLEGE = {
    "南湖校区": {
        "外国语学院", "艺术学院", "理学院", "资源与土木工程学院", "冶金学院",
        "材料科学与工程学院", "机械工程与自动化学院", "信息科学与工程学院",
        "体育部", "国家卓越工程师学院",
    },
    "浑南校区": {
        "文法学院", "马克思主义学院", "工商管理学院", "计算机科学与工程学院", "软件学院",
        "医学与生物信息工程学院", "生命科学与健康学院", "江河建筑学院",
        "机器人科学与工程学院", "未来技术学院",
    },
}
LEGACY_MEDICAL_COLLEGES = {
    "医学与生物信息工程学院（原中荷生物医学与信息工程学院）",
    "医学与生物信息工程学院（中荷生物医学与信息工程学院）",
}
MEDICAL_COLLEGE = "医学与生物信息工程学院"
SESSIONS: dict[str, dict] = {}
RATE_BUCKETS: dict[str, list[float]] = {}
STATE_LOCK = Lock()


def load_college_majors() -> tuple[dict[str, tuple[str, ...]], object]:
    """Load the major mapping once and hold the source file open for this process."""
    mapping_file = COLLEGE_MAJORS_PATH.open("r", encoding="utf-8")
    try:
        msvcrt.locking(mapping_file.fileno(), msvcrt.LK_NBLCK, 1)
        raw_mapping = json.load(mapping_file)
    except Exception:
        mapping_file.close()
        raise
    if not isinstance(raw_mapping, dict):
        mapping_file.close()
        raise ValueError("college_majors.json must contain an object")
    mapping: dict[str, tuple[str, ...]] = {}
    for college, majors in raw_mapping.items():
        if not isinstance(college, str) or not isinstance(majors, list) or not all(isinstance(major, str) and major for major in majors):
            mapping_file.close()
            raise ValueError("college_majors.json contains invalid entries")
        mapping[college] = tuple(majors)
    if mapping.get("未知") != ("未知",):
        mapping_file.close()
        raise ValueError("college_majors.json must define 未知: [未知]")
    return mapping, mapping_file


COLLEGE_MAJORS, COLLEGE_MAJORS_FILE = load_college_majors()


def configure_logging() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("survey")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


LOGGER = configure_logging()


class QuietWSGIRequestHandler(WSGIRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)

    def handle(self):
        try:
            super().handle()
        except socket.timeout:
            LOGGER.warning("request_timeout peer=%s", self.client_address[0])
        except (ConnectionError, OSError) as error:
            LOGGER.warning("request_connection_error peer=%s error=%s", self.client_address[0], error)

    def log_message(self, format, *args):
        status = str(args[1]) if len(args) > 1 else ""
        request_path = urlparse(getattr(self, "path", "/")).path
        if status in {"200", "302"} or (status == "404" and request_path == "/favicon.ico"):
            return
        super().log_message(format, *args)


class StableWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, server_address, application, handler_class):
        super().__init__(server_address, handler_class)
        self.set_app(application)
        self._request_slots = BoundedSemaphore(MAX_REQUEST_THREADS)

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            LOGGER.warning("request_rejected peer=%s reason=server_busy", client_address[0])
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


def now() -> datetime:
    return datetime.now()


def deadline_passed() -> bool:
    return now() >= DEADLINE


def iso_now() -> str:
    return now().isoformat(timespec="seconds")


def client_ip(environ: dict) -> str:
    # Keep the direct peer address in logs; forwarded headers are client-controlled.
    return environ.get("REMOTE_ADDR", "unknown")[:64]


def user_agent(environ: dict) -> str:
    return environ.get("HTTP_USER_AGENT", "")[:512]


def campus_for_college(college: str) -> str:
    for campus, colleges in CAMPUS_BY_COLLEGE.items():
        if college in colleges:
            return campus
    return "未知"


def normalize_college(college: str) -> str:
    return MEDICAL_COLLEGE if college in LEGACY_MEDICAL_COLLEGES else college


def majors_for_college(college: str) -> tuple[str, ...]:
    return COLLEGE_MAJORS.get(college, ("其他",))


def db_connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS submissions (
                student_id TEXT PRIMARY KEY,
                qq TEXT NOT NULL,
                name TEXT NOT NULL,
                college TEXT NOT NULL DEFAULT '',
                group_card TEXT NOT NULL DEFAULT '',
                campus TEXT NOT NULL DEFAULT '未知',
                major TEXT NOT NULL DEFAULT '',
                gender TEXT NOT NULL CHECK (gender IN ('男', '女')),
                departments_json TEXT NOT NULL,
                transfer TEXT NOT NULL CHECK (transfer IN ('是', '否')),
                strengths TEXT NOT NULL,
                other_talents TEXT NOT NULL DEFAULT '',
                submitted_at TEXT NOT NULL,
                request_id TEXT NOT NULL,
                content_hash TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS login_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                qq TEXT NOT NULL,
                student_id TEXT NOT NULL,
                name TEXT,
                logged_at TEXT NOT NULL,
                ip TEXT NOT NULL,
                user_agent TEXT NOT NULL,
                result TEXT NOT NULL,
                reason TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS change_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id TEXT NOT NULL,
                operator TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS request_dedup (
                request_id TEXT PRIMARY KEY,
                student_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_annotations (
                student_id TEXT PRIMARY KEY,
                stars_json TEXT NOT NULL DEFAULT '[]',
                comments_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_request_dedup_student_time
                ON request_dedup(student_id, created_at);
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(submissions)")}
        if "college" not in columns:
            conn.execute("ALTER TABLE submissions ADD COLUMN college TEXT NOT NULL DEFAULT ''")
        if "group_card" not in columns:
            conn.execute("ALTER TABLE submissions ADD COLUMN group_card TEXT NOT NULL DEFAULT ''")
        if "campus" not in columns:
            conn.execute("ALTER TABLE submissions ADD COLUMN campus TEXT NOT NULL DEFAULT '未知'")
        if "major" not in columns:
            conn.execute("ALTER TABLE submissions ADD COLUMN major TEXT NOT NULL DEFAULT ''")
        placeholders = ", ".join("?" for _ in LEGACY_MEDICAL_COLLEGES)
        conn.execute(
            f"UPDATE submissions SET college = ? WHERE college IN ({placeholders})",
            (MEDICAL_COLLEGE, *LEGACY_MEDICAL_COLLEGES),
        )
        for campus, colleges in CAMPUS_BY_COLLEGE.items():
            placeholders = ", ".join("?" for _ in colleges)
            conn.execute(
                f"UPDATE submissions SET campus = ? WHERE college IN ({placeholders})",
                (campus, *colleges),
            )


def load_users() -> dict[str, dict[str, str]]:
    users: dict[str, dict[str, str]] = {}
    if not CSV_PATH.is_file():
        return users
    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as file:
        rows = csv.DictReader(file)
        for row in rows:
            qq = (row.get("QQ号") or "").strip()
            student_id = (row.get("学号") or "").strip()
            name = (row.get("姓名") or "").strip()
            college = normalize_college((row.get("学院") or "").strip())
            # The source file currently uses 群名片; accept the earlier QQ群名片 spelling too.
            group_card = (row.get("QQ群名片") or row.get("群名片") or "").strip()
            if qq:
                users[qq] = {"student_id": student_id, "name": name, "college": college, "group_card": group_card}
    return users


def load_users_by_student_id() -> dict[str, dict[str, str]]:
    return {
        user["student_id"]: user
        for user in load_users().values()
        if user.get("student_id")
    }


def is_admin_user(qq: str, authenticated_name: str = "") -> bool:
    """Determine admin access from the name stored in users.csv."""
    local_user = load_users().get(qq)
    if local_user:
        return local_user["name"].startswith("管理员")
    return USE_LOCAL_USERS_CSV and authenticated_name.startswith("管理员")


def admin_submission(row: sqlite3.Row, identity: dict[str, str] | None = None) -> dict:
    identity = identity or {}
    college = identity.get("college", "")
    return {
        "qq": row["qq"],
        "name": row["name"],
        "student_id": row["student_id"],
        "college": college,
        "group_card": identity.get("group_card", ""),
        "campus": campus_for_college(college),
        "major": row["major"],
        "gender": row["gender"],
        "departments": json.loads(row["departments_json"]),
        "transfer": row["transfer"],
        "strengths": row["strengths"],
        "other_talents": row["other_talents"],
        "submitted_at": row["submitted_at"],
        "star_count": len(parse_annotation_list(row["stars_json"])),
        "comment_count": len(parse_annotation_list(row["comments_json"])),
    }


def parse_annotation_list(value: str) -> list[dict]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def get_admin_annotations(conn: sqlite3.Connection, student_id: str) -> dict:
    row = conn.execute(
        "SELECT stars_json, comments_json FROM admin_annotations WHERE student_id = ?",
        (student_id,),
    ).fetchone()
    stars = parse_annotation_list(row["stars_json"]) if row else []
    comments = parse_annotation_list(row["comments_json"]) if row else []
    return {"stars": stars, "comments": comments, "star_count": len(stars), "comment_count": len(comments)}


def normalized_annotation(body: dict, annotation_type: str) -> tuple[str | None, str | None]:
    field = "reason" if annotation_type == "star" else "content"
    value = body.get(field)
    if not isinstance(value, str):
        return None, "内容格式无效。"
    value = value.strip()
    if not value:
        return None, "内容不能为空。"
    if len(value) > 1000 or "\x00" in value:
        return None, "内容不能超过 1000 个字符。"
    return value, None


def auth_signature(secret: str, method: str, path: str, timestamp: str, nonce: str, body: bytes, status: int | None = None) -> str:
    status_part = str(status) if status is not None else "request"
    material = "\n".join((status_part, method.upper(), path, timestamp, nonce, hashlib.sha256(body).hexdigest())).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), material, hashlib.sha256).hexdigest()


def verify_remote_user(qq: str, student_id: str) -> dict:
    """Ask the remote CSV service without receiving the users.csv contents."""
    if not REMOTE_USERS_CSV_SHARED_SECRET:
        return {"error": "remote_auth_not_configured"}
    try:
        host, separator, port_text = REMOTE_USERS_CSV_SERVER.rpartition(":")
        if not separator or not host or not port_text:
            return {"error": "remote_auth_configuration_error"}
        port = int(port_text)
        if not 1 <= port <= 65535:
            return {"error": "remote_auth_configuration_error"}

        request_body = json.dumps(
            {"qq": qq, "student_id": student_id},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        timestamp = str(int(time.time()))
        nonce = secrets.token_urlsafe(24)
        signature = auth_signature(REMOTE_USERS_CSV_SHARED_SECRET, "POST", "/verify", timestamp, nonce, request_body)
        connection = HTTPConnection(host, port, timeout=REMOTE_AUTH_TIMEOUT_SECONDS)
        try:
            connection.request(
                "POST",
                "/verify",
                body=request_body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(request_body)),
                    "X-Auth-Timestamp": timestamp,
                    "X-Auth-Nonce": nonce,
                    "X-Auth-Signature": signature,
                },
            )
            response = connection.getresponse()
            response_body = response.read(64 * 1024 + 1)
            response_status = response.status
            response_timestamp = response.getheader("X-Auth-Timestamp", "")
            response_nonce = response.getheader("X-Auth-Nonce", "")
            response_signature = response.getheader("X-Auth-Signature", "")
        finally:
            connection.close()

        if len(response_body) > 64 * 1024 or not response_timestamp or response_nonce != nonce or not response_signature:
            return {"error": "remote_auth_bad_response"}
        try:
            response_age = abs(int(time.time()) - int(response_timestamp))
        except ValueError:
            return {"error": "remote_auth_bad_response"}
        if response_age > REMOTE_AUTH_CLOCK_SKEW_SECONDS:
            return {"error": "remote_auth_bad_response"}
        expected_signature = auth_signature(
            REMOTE_USERS_CSV_SHARED_SECRET,
            "POST",
            "/verify",
            response_timestamp,
            response_nonce,
            response_body,
            response_status,
        )
        if not hmac.compare_digest(response_signature, expected_signature):
            return {"error": "remote_auth_bad_response"}
        if response_status != 200:
            return {"error": "remote_auth_unavailable"}
        payload = json.loads(response_body.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            return {"error": "remote_auth_bad_response"}
        reason = payload.get("reason")
        if reason not in {"ok", "qq_not_found", "student_missing", "student_mismatch"}:
            return {"error": "remote_auth_bad_response"}
        return {
            "known": bool(payload.get("known")),
            "expected_student": str(payload.get("expected_student", ""))[:64],
            "name": str(payload.get("name", ""))[:128],
            "college": str(payload.get("college", ""))[:128],
            "group_card": str(payload.get("group_card", ""))[:128],
            "reason": reason,
        }
    except (ConnectionError, OSError, TimeoutError, ValueError, json.JSONDecodeError):
        return {"error": "remote_auth_unavailable"}


def cleanup_state() -> None:
    cutoff = time.time() - RATE_WINDOW_SECONDS
    session_cutoff = now() - SESSION_TTL
    with STATE_LOCK:
        for key in list(RATE_BUCKETS):
            RATE_BUCKETS[key] = [stamp for stamp in RATE_BUCKETS[key] if stamp >= cutoff]
            if not RATE_BUCKETS[key]:
                del RATE_BUCKETS[key]
        for token, session in list(SESSIONS.items()):
            if session["created_at"] < session_cutoff:
                del SESSIONS[token]


def rate_limited(key: str) -> bool:
    stamp = time.time()
    with STATE_LOCK:
        bucket = [item for item in RATE_BUCKETS.get(key, []) if stamp - item < RATE_WINDOW_SECONDS]
        limited = len(bucket) >= RATE_LIMIT
        if not limited:
            bucket.append(stamp)
        RATE_BUCKETS[key] = bucket
        return limited


def json_response(payload: dict, status: int = 200, extra_headers: list[tuple[str, str]] | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(body)))]
    if extra_headers:
        headers.extend(extra_headers)
    return status, headers, body


def html_response(body: str, status: int = 200, extra_headers: list[tuple[str, str]] | None = None):
    encoded = body.encode("utf-8")
    headers = [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(encoded)))]
    if extra_headers:
        headers.extend(extra_headers)
    return status, headers, encoded


def redirect(location: str, status: int = 302, extra_headers: list[tuple[str, str]] | None = None):
    headers = [("Location", location), ("Content-Length", "0")]
    if extra_headers:
        headers.extend(extra_headers)
    return status, headers, b""


def render_page(filename: str, **values: str) -> str:
    template = (BASE_DIR / "templates" / filename).read_text(encoding="utf-8")
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", html.escape(str(value)))
    return template


def parse_body(environ: dict) -> dict:
    length = min(int(environ.get("CONTENT_LENGTH") or 0), 64 * 1024)
    raw = environ["wsgi.input"].read(length)
    if not raw:
        return {}
    content_type = environ.get("CONTENT_TYPE", "")
    if "application/json" in content_type:
        try:
            parsed = json.loads(raw.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
    parsed_qs = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed_qs.items()}


def cookies(environ: dict) -> dict[str, str]:
    cookie = SimpleCookie()
    cookie.load(environ.get("HTTP_COOKIE", ""))
    return {key: morsel.value for key, morsel in cookie.items()}


def session_from_request(environ: dict) -> tuple[str | None, dict | None]:
    token = cookies(environ).get("survey_session")
    if not token:
        return None, None
    with STATE_LOCK:
        session = SESSIONS.get(token)
        if not session:
            return token, None
        if now() - session["created_at"] > SESSION_TTL:
            del SESSIONS[token]
            return token, None
        return token, session


def require_session(environ: dict):
    token, session = session_from_request(environ)
    if not session:
        return None, json_response({"ok": False, "error": "登录已失效，请重新登录。"}, 401)
    if session["csrf"] != environ.get("HTTP_X_CSRF_TOKEN", ""):
        return None, json_response({"ok": False, "error": "请求校验失败，请刷新页面后重试。"}, 403)
    return session, None


def log_login(environ: dict, qq: str, student_id: str, name: str | None, result: str, reason: str) -> None:
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO login_logs(qq, student_id, name, logged_at, ip, user_agent, result, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (qq[:64], student_id[:64], name[:128] if name else None, iso_now(), client_ip(environ), user_agent(environ), result[:32], reason[:255]),
        )


def valid_departments(value) -> bool:
    return isinstance(value, list) and 1 <= len(value) <= len(DEPARTMENTS) and len(set(value)) == len(value) and all(item in DEPARTMENTS for item in value)


def decode_strengths(value) -> str | None:
    if not isinstance(value, str) or len(value) > 16000:
        return None
    try:
        raw = base64.b64decode(value, validate=True)
        text = raw.decode("utf-8").strip()
    except (binascii.Error, UnicodeDecodeError):
        return None
    if len(text) > 4000 or "\x00" in text:
        return None
    return text


def normalized_submission(body: dict, session: dict) -> tuple[dict | None, str | None]:
    major = body.get("major")
    gender = body.get("gender")
    transfer = body.get("transfer")
    departments = body.get("departments")
    strengths = decode_strengths(body.get("strengths_b64", ""))
    other_talents = decode_strengths(body.get("other_talents_b64", ""))
    if not isinstance(major, str) or major not in majors_for_college(session["college"]):
        return None, "请选择与学院相符的专业。"
    if gender not in ("男", "女"):
        return None, "请选择性别。"
    if transfer not in ("是", "否"):
        return None, "请选择是否服从部门间调剂。"
    if not valid_departments(departments):
        return None, "请至少选择一个部门，并确认部门顺序有效。"
    if strengths is None:
        return None, "请填写个人优势及未来工作设想，内容不能超过 4000 字。"
    if other_talents is None:
        return None, "其它特长内容不能超过 4000 字。"
    return {
        "name": session["name"],
        "student_id": session["student_id"],
        "qq": session["qq"],
        "college": session["college"],
        "group_card": session["group_card"],
        "campus": session["campus"],
        "major": major,
        "gender": gender,
        "departments": departments,
        "transfer": transfer,
        "strengths": strengths,
        "other_talents": other_talents,
    }, None


def content_hash(record: dict) -> str:
    material = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def public_submission(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    return {
        "major": row["major"],
        "gender": row["gender"],
        "departments": json.loads(row["departments_json"]),
        "transfer": row["transfer"],
        "strengths": row["strengths"],
        "other_talents": row["other_talents"],
        "submitted_at": row["submitted_at"],
    }


def save_submission(record: dict, request_id: str):
    if not isinstance(request_id, str) or not (1 <= len(request_id) <= 80):
        return None, "请求编号无效。", 400
    fingerprint = content_hash(record)
    current_time = time.time()
    with db_connect() as conn:
        dedup_retention = max(RATE_WINDOW_SECONDS, REPEAT_WINDOW_SECONDS, SUBMISSION_WINDOW_SECONDS)
        conn.execute("DELETE FROM request_dedup WHERE created_at < ?", (current_time - dedup_retention,))
        recent_submissions = conn.execute(
            "SELECT COUNT(*) FROM request_dedup WHERE student_id = ? AND created_at >= ?",
            (record["student_id"], current_time - SUBMISSION_WINDOW_SECONDS),
        ).fetchone()[0]
        if recent_submissions >= SUBMISSION_LIMIT:
            return None, "提交过于频繁，请稍后再试。", 429
        duplicate = conn.execute(
            "SELECT request_id FROM request_dedup WHERE student_id = ? AND content_hash = ? AND created_at >= ? LIMIT 1",
            (record["student_id"], fingerprint, current_time - REPEAT_WINDOW_SECONDS),
        ).fetchone()
        if duplicate:
            return None, "相同内容刚刚已经提交，请稍后再试。", 409
        if conn.execute("SELECT 1 FROM request_dedup WHERE request_id = ?", (request_id,)).fetchone():
            return None, "请求已处理，请勿重复提交。", 409

        before_row = conn.execute("SELECT * FROM submissions WHERE student_id = ?", (record["student_id"],)).fetchone()
        before = public_submission(before_row)
        submitted_at = iso_now()
        conn.execute(
            "INSERT INTO request_dedup(request_id, student_id, content_hash, created_at) VALUES (?, ?, ?, ?)",
            (request_id, record["student_id"], fingerprint, current_time),
        )
        conn.execute(
            """INSERT INTO submissions(student_id, qq, name, college, group_card, campus, major, gender, departments_json, transfer, strengths, other_talents, submitted_at, request_id, content_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(student_id) DO UPDATE SET qq=excluded.qq, name=excluded.name, college=excluded.college, group_card=excluded.group_card, campus=excluded.campus, major=excluded.major, gender=excluded.gender,
               departments_json=excluded.departments_json, transfer=excluded.transfer, strengths=excluded.strengths,
               other_talents=excluded.other_talents,
               submitted_at=excluded.submitted_at, request_id=excluded.request_id, content_hash=excluded.content_hash""",
            (record["student_id"], record["qq"], record["name"], record["college"], record["group_card"], record["campus"], record["major"], record["gender"], json.dumps(record["departments"], ensure_ascii=False), record["transfer"], record["strengths"], record["other_talents"], submitted_at, request_id, fingerprint),
        )
        after = {key: record[key] for key in ("major", "gender", "departments", "transfer", "strengths", "other_talents")}
        conn.execute(
            "INSERT INTO change_logs(student_id, operator, changed_at, before_json, after_json) VALUES (?, ?, ?, ?, ?)",
            (record["student_id"], record["student_id"], submitted_at, json.dumps(before, ensure_ascii=False) if before else None, json.dumps(after, ensure_ascii=False)),
        )
    return submitted_at, None, 200


def _application(environ: dict, start_response):
    cleanup_state()
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = urlparse(environ.get("PATH_INFO", "/")).path

    if path == "/health" and method == "GET":
        status, headers, body = json_response({"ok": True, "service": "zhaoxinsurvey"})
        start_response("200 OK", headers)
        return [body]

    if path == "/favicon.ico" and method == "GET":
        favicon = BASE_DIR / "static" / "logo.png"
        body = favicon.read_bytes()
        start_response(
            "200 OK",
            [
                ("Content-Type", "image/png"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "public, max-age=86400"),
            ],
        )
        return [body]

    if path.startswith("/static/"):
        relative = path.removeprefix("/static/")
        static_file = (BASE_DIR / "static" / relative).resolve()
        static_root = (BASE_DIR / "static").resolve()
        if static_file.is_file() and static_root in static_file.parents:
            body = static_file.read_bytes()
            start_response("200 OK", [("Content-Type", mimetypes.guess_type(static_file.name)[0] or "application/octet-stream"), ("Content-Length", str(len(body)))])
            return [body]
        status, headers, body = html_response("Not Found", 404)
        start_response(f"{status} Not Found", headers)
        return [body]

    if deadline_passed() and path not in {"/admin", "/api/admin/submissions"} and not path.startswith("/api/admin/annotations/"):
        if path.startswith("/api/"):
            status, headers, body = json_response({"ok": False, "error": "报名已截止。"}, 410)
        else:
            status, headers, body = html_response(render_page("timeend.html"), 200)
        start_response(f"{status} {'Gone' if status == 410 else 'OK'}", headers)
        return [body]

    if path == "/" and method == "GET":
        status, headers, body = redirect("/login")
    elif path == "/login" and method == "GET":
        status, headers, body = html_response(render_page("login.html"))
    elif path == "/api/login" and method == "POST":
        if rate_limited(f"login:{client_ip(environ)}"):
            environ["login_reason"] = "rate_limited"
            status, headers, body = json_response({"ok": False, "error": "请求过于频繁，请稍后再试。"}, 429)
        else:
            request = parse_body(environ)
            qq = str(request.get("qq", "")).strip()
            student_id = str(request.get("student_id", "")).strip()
            users = load_users() if USE_LOCAL_USERS_CSV else {}
            remote_error = None
            if not USE_LOCAL_USERS_CSV:
                remote_result = verify_remote_user(qq, student_id)
                remote_error = remote_result.get("error")
                if not remote_error and remote_result.get("known"):
                    users[qq] = {"student_id": remote_result.get("expected_student", ""), "name": remote_result.get("name", ""), "college": normalize_college(remote_result.get("college", "")), "group_card": remote_result.get("group_card", "")}
            if not qq or len(qq) > 32 or len(student_id) > 32:
                reason = "invalid_input"
                environ["login_reason"] = reason
                log_login(environ, qq, student_id, None, "failure", reason)
                status, headers, body = json_response({"ok": False, "error": "请输入有效的 QQ 号和学号。"}, 400)
            elif remote_error:
                environ["login_reason"] = remote_error
                log_login(environ, qq, student_id, None, "failure", remote_error)
                status, headers, body = json_response({"ok": False, "error": "身份验证服务暂时不可用，请稍后重试。"}, 503)
            elif qq not in users:
                environ["login_reason"] = "qq_not_found"
                log_login(environ, qq, student_id, None, "failure", "qq_not_found")
                status, headers, body = json_response({"ok": False, "error": "请加入招新 QQ 群 810192062 后继续。"}, 403)
            else:
                user = users[qq]
                expected_student, name = user["student_id"], user["name"]
                if not expected_student:
                    environ["login_reason"] = "student_missing"
                    log_login(environ, qq, student_id, name, "failure", "student_missing")
                    status, headers, body = json_response({"ok": False, "error": "请在招新群内实名后继续，实名方法请看置顶群公告。"}, 403)
                elif student_id != expected_student:
                    environ["login_reason"] = "student_mismatch"
                    log_login(environ, qq, student_id, name, "failure", "student_mismatch")
                    status, headers, body = json_response({"ok": False, "error": "学号与 QQ 号不匹配，请核对后重试。"}, 403)
                else:
                    environ["login_reason"] = "ok"
                    token = secrets.token_urlsafe(32)
                    csrf = secrets.token_urlsafe(24)
                    is_admin = is_admin_user(qq, name)
                    with STATE_LOCK:
                        campus = campus_for_college(user["college"])
                        SESSIONS[token] = {"qq": qq, "student_id": expected_student, "name": name, "college": user["college"], "group_card": user["group_card"], "campus": campus, "is_admin": is_admin, "csrf": csrf, "created_at": now()}
                    log_login(environ, qq, expected_student, name, "success", "ok")
                    status, headers, body = json_response(
                        {"ok": True, "name": name, "qq": qq, "student_id": expected_student, "college": user["college"], "group_card": user["group_card"], "campus": campus, "csrf": csrf, "is_admin": is_admin},
                        extra_headers=[("Set-Cookie", "survey_session=" + token + "; HttpOnly; SameSite=Lax; Path=/")],
                    )
    elif path == "/admin" and method == "GET":
        _, session = session_from_request(environ)
        if not session:
            status, headers, body = redirect("/login")
        elif not session.get("is_admin"):
            status, headers, body = html_response("<h1>无权访问</h1><p>该页面仅供管理员使用。</p>", 403)
        else:
            status, headers, body = html_response(render_page("admin.html"))
    elif path == "/api/admin/submissions" and method == "GET":
        _, session = session_from_request(environ)
        if not session:
            status, headers, body = json_response({"ok": False, "error": "登录已失效，请重新登录。"}, 401)
        elif not session.get("is_admin"):
            status, headers, body = json_response({"ok": False, "error": "无权访问管理员数据。"}, 403)
        else:
            with db_connect() as conn:
                rows = conn.execute(
                    "SELECT s.qq, s.name, s.student_id, s.college, s.group_card, s.campus, s.major, s.gender, s.departments_json, s.transfer, s.strengths, s.other_talents, s.submitted_at, "
                    "COALESCE(a.stars_json, '[]') AS stars_json, COALESCE(a.comments_json, '[]') AS comments_json "
                    "FROM submissions AS s LEFT JOIN admin_annotations AS a ON a.student_id = s.student_id "
                    "ORDER BY s.submitted_at DESC, s.student_id ASC"
                ).fetchall()
            users_by_student_id = load_users_by_student_id()
            submissions = [admin_submission(row, users_by_student_id.get(row["student_id"])) for row in rows]
            latest_submitted_at = submissions[0]["submitted_at"] if submissions else None
            status, headers, body = json_response(
                {"ok": True, "count": len(submissions), "latest_submitted_at": latest_submitted_at, "submissions": submissions}
            )
    elif path.startswith("/api/admin/annotations/") and method == "GET":
        _, session = session_from_request(environ)
        student_id = path.removeprefix("/api/admin/annotations/")
        if not session:
            status, headers, body = json_response({"ok": False, "error": "登录已失效，请重新登录。"}, 401)
        elif not session.get("is_admin"):
            status, headers, body = json_response({"ok": False, "error": "无权访问管理员数据。"}, 403)
        elif not student_id or len(student_id) > 64:
            status, headers, body = json_response({"ok": False, "error": "学号无效。"}, 400)
        else:
            with db_connect() as conn:
                exists = conn.execute("SELECT 1 FROM submissions WHERE student_id = ?", (student_id,)).fetchone()
                if not exists:
                    status, headers, body = json_response({"ok": False, "error": "未找到该报名记录。"}, 404)
                else:
                    status, headers, body = json_response({"ok": True, "student_id": student_id, **get_admin_annotations(conn, student_id)})
    elif path.startswith("/api/admin/annotations/") and method == "POST":
        session, auth_error = require_session(environ)
        student_id = path.removeprefix("/api/admin/annotations/")
        if auth_error:
            status, headers, body = auth_error
        elif not session.get("is_admin"):
            status, headers, body = json_response({"ok": False, "error": "无权添加管理员批注。"}, 403)
        elif not student_id or len(student_id) > 64:
            status, headers, body = json_response({"ok": False, "error": "学号无效。"}, 400)
        else:
            request = parse_body(environ)
            annotation_type = request.get("type")
            if not isinstance(annotation_type, str) or annotation_type not in {"star", "comment"}:
                status, headers, body = json_response({"ok": False, "error": "批注类型无效。"}, 400)
            else:
                value, error = normalized_annotation(request, annotation_type)
                if error:
                    status, headers, body = json_response({"ok": False, "error": error}, 400)
                else:
                    record = {"content": value, "operator": session["name"], "created_at": iso_now()}
                    with db_connect() as conn:
                        exists = conn.execute("SELECT 1 FROM submissions WHERE student_id = ?", (student_id,)).fetchone()
                        if not exists:
                            status, headers, body = json_response({"ok": False, "error": "未找到该报名记录。"}, 404)
                        else:
                            # Serialize read-modify-write updates so two admins cannot overwrite each other.
                            conn.execute("BEGIN IMMEDIATE")
                            current = get_admin_annotations(conn, student_id)
                            key = "stars" if annotation_type == "star" else "comments"
                            current[key].append(record)
                            if len(current[key]) > 500:
                                current[key] = current[key][-500:]
                            conn.execute(
                                "INSERT INTO admin_annotations(student_id, stars_json, comments_json, updated_at) VALUES (?, ?, ?, ?) "
                                "ON CONFLICT(student_id) DO UPDATE SET stars_json=excluded.stars_json, comments_json=excluded.comments_json, updated_at=excluded.updated_at",
                                (student_id, json.dumps(current["stars"], ensure_ascii=False), json.dumps(current["comments"], ensure_ascii=False), iso_now()),
                            )
                            status, headers, body = json_response({"ok": True, "student_id": student_id, **get_admin_annotations(conn, student_id)}, 201)
    elif path == "/questionnaire" and method == "GET":
        _, session = session_from_request(environ)
        if not session:
            status, headers, body = redirect("/login")
        elif session.get("is_admin"):
            status, headers, body = redirect("/admin")
        else:
            status, headers, body = html_response(render_page("questionnaire.html"))
    elif path == "/success.html" and method == "GET":
        _, session = session_from_request(environ)
        if not session:
            status, headers, body = redirect("/login")
        else:
            status, headers, body = html_response(render_page("success.html"))
    elif path == "/timeend.html" and method == "GET":
        status, headers, body = html_response(render_page("timeend.html"))
    elif path == "/api/me" and method == "GET":
        token, session = session_from_request(environ)
        if not session:
            status, headers, body = json_response({"ok": False, "error": "登录已失效，请重新登录。"}, 401)
        else:
            with db_connect() as conn:
                row = conn.execute("SELECT * FROM submissions WHERE student_id = ?", (session["student_id"],)).fetchone()
            status, headers, body = json_response({"ok": True, "name": session["name"], "qq": session["qq"], "student_id": session["student_id"], "college": session["college"], "group_card": session["group_card"], "campus": session["campus"], "majors": majors_for_college(session["college"]), "csrf": session["csrf"], "is_admin": bool(session.get("is_admin")), "submission": public_submission(row)})
    elif path == "/api/submit" and method == "POST":
        session, auth_error = require_session(environ)
        if auth_error:
            status, headers, body = auth_error
        elif rate_limited(f"submit:ip:{client_ip(environ)}") or rate_limited(f"submit:student:{session['student_id']}"):
            status, headers, body = json_response({"ok": False, "error": "请求过于频繁，请稍后再试。"}, 429)
        else:
            request = parse_body(environ)
            record, error = normalized_submission(request, session)
            if error:
                status, headers, body = json_response({"ok": False, "error": error}, 400)
            else:
                saved_at, error, save_status = save_submission(record, request.get("request_id", ""))
                if error:
                    status, headers, body = json_response({"ok": False, "error": error}, save_status)
                else:
                    status, headers, body = json_response({"ok": True, "submitted_at": saved_at})
    elif path == "/api/logout" and method == "POST":
        token, session = session_from_request(environ)
        if not session:
            status, headers, body = json_response({"ok": False, "error": "登录已失效。"}, 401)
        elif session["csrf"] != environ.get("HTTP_X_CSRF_TOKEN", ""):
            status, headers, body = json_response({"ok": False, "error": "请求校验失败，请刷新页面后重试。"}, 403)
        else:
            with STATE_LOCK:
                SESSIONS.pop(token, None)
            status, headers, body = json_response({"ok": True}, extra_headers=[("Set-Cookie", "survey_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0")])
    else:
        status, headers, body = html_response("Not Found", 404)

    status_phrase = HTTPStatus(status).phrase
    start_response(f"{status} {status_phrase}", headers)
    return [body]


def application(environ: dict, start_response):
    request_id = uuid.uuid4().hex[:16]
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = urlparse(environ.get("PATH_INFO", "/")).path
    started_at = time.perf_counter()
    response_started = False

    def logged_start_response(status, headers, exc_info=None):
        nonlocal response_started
        response_started = True
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        safe_ua = environ.get("HTTP_USER_AGENT", "").replace("\r", " ").replace("\n", " ")[:200]
        status_code = status.split(" ", 1)[0]
        if status_code not in {"200", "302"} and not (status_code == "404" and path == "/favicon.ico"):
            login_reason = environ.get("login_reason")
            LOGGER.info(
                "request_id=%s method=%s path=%s status=%s duration_ms=%.1f ip=%s user_agent=%s%s",
                request_id,
                method,
                path,
                status_code,
                elapsed_ms,
                client_ip(environ),
                safe_ua,
                f" login_reason={login_reason}" if login_reason else "",
            )
        response_headers = list(headers)
        response_headers.append(("X-Request-ID", request_id))
        if exc_info is None:
            return start_response(status, response_headers)
        return start_response(status, response_headers, exc_info)

    try:
        return _application(environ, logged_start_response)
    except Exception:
        LOGGER.exception(
            "unhandled_exception request_id=%s method=%s path=%s ip=%s",
            request_id,
            method,
            path,
            client_ip(environ),
        )
        if response_started:
            raise
        if path.startswith("/api/"):
            status, headers, body = json_response(
                {"ok": False, "error": "服务器内部错误，请稍后重试。", "request_id": request_id},
                500,
            )
        else:
            status, headers, body = html_response(
                "<h1>服务器内部错误</h1><p>请稍后重试。错误编号：" + html.escape(request_id) + "</p>",
                500,
            )
        logged_start_response(f"{status} {HTTPStatus(status).phrase}", headers)
        return [body]


if __name__ == "__main__":
    try:
        init_db()
        port = int(os.environ.get("SURVEY_PORT", "8000"))
        with StableWSGIServer(("0.0.0.0", port), application, QuietWSGIRequestHandler) as server:
            LOGGER.info("server_started address=http://127.0.0.1:%s deadline=%s database=%s log=%s", port, DEADLINE.isoformat(sep=" "), DATABASE_PATH, LOG_PATH)
            server.serve_forever()
    except Exception:
        LOGGER.exception("server_start_failed")
        raise
