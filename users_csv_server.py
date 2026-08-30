"""Authenticated remote identity verification service backed by users.csv.

这个脚本配合zhaoxinsurvey使用，负责接收来自zhaoxinsurvey的登录请求，验证QQ号和学号是否匹配，并返回实名信息。
它和zhaoxinqbot项目中的同名文件是同一个文件。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock


BASE_DIR = Path(__file__).resolve().parent
CSV_PATH = BASE_DIR / "data//realname_members.csv"
CSV_HAS_HEADER = False
SHARED_SECRET = "REMOTE_USERS_CSV_SHARED_SECRET"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = "25565"
AUTH_CLOCK_SKEW_SECONDS = 30
MAX_BODY_BYTES = 64 * 1024
NONCE_RETENTION_SECONDS = 60
USED_NONCES: dict[str, float] = {}
NONCE_LOCK = Lock()
LOGGER = logging.getLogger("users_csv_server")


def auth_signature(secret: str, method: str, path: str, timestamp: str, nonce: str, body: bytes, status: int | None = None) -> str:
    status_part = str(status) if status is not None else "request"
    material = "\n".join((status_part, method.upper(), path, timestamp, nonce, hashlib.sha256(body).hexdigest())).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), material, hashlib.sha256).hexdigest()


def load_users() -> dict[str, tuple[str, str]]:
    users: dict[str, tuple[str, str]] = {}
    if not CSV_PATH.is_file():
        return users
    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as file:
        rows = csv.reader(file)
        if CSV_HAS_HEADER:
            next(rows, None)
        for row in rows:
            if len(row) < 3:
                continue
            qq, student_id, name = (cell.strip() for cell in row[:3])
            if qq:
                users[qq] = (student_id, name)
    return users


def nonce_is_fresh(nonce: str, timestamp: int) -> bool:
    current_time = time.time()
    with NONCE_LOCK:
        for key, created_at in list(USED_NONCES.items()):
            if current_time - created_at > NONCE_RETENTION_SECONDS:
                del USED_NONCES[key]
        if nonce in USED_NONCES:
            return False
        USED_NONCES[nonce] = float(timestamp)
        return True


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "UsersCsvServer/1.0"

    def log_message(self, format: str, *args) -> None:
        LOGGER.info("%s - %s", self.address_string(), format % args)

    def send_json(self, payload: dict, status: int, signed: bool = True, auth_nonce: str | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store",
        }
        if signed:
            timestamp = str(int(time.time()))
            nonce = auth_nonce or secrets.token_urlsafe(24)
            headers.update(
                {
                    "X-Auth-Timestamp": timestamp,
                    "X-Auth-Nonce": nonce,
                    "X-Auth-Signature": auth_signature(SHARED_SECRET, "POST", "/verify", timestamp, nonce, body, status),
                }
            )
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json({"ok": True, "service": "users_csv_server"}, HTTPStatus.OK, signed=False)
            return
        self.send_json({"ok": False, "error": "not_found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if self.path != "/verify":
            self.send_json({"ok": False, "error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        if not SHARED_SECRET:
            self.send_json({"ok": False, "error": "server_not_configured"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        try:
            content_length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            content_length = -1
        if not 0 <= content_length <= MAX_BODY_BYTES:
            self.send_json({"ok": False, "error": "invalid_body"}, HTTPStatus.BAD_REQUEST)
            return
        body = self.rfile.read(content_length)
        timestamp_text = self.headers.get("X-Auth-Timestamp", "")
        nonce = self.headers.get("X-Auth-Nonce", "")
        received_signature = self.headers.get("X-Auth-Signature", "")
        try:
            timestamp = int(timestamp_text)
        except ValueError:
            timestamp = 0
        expected_signature = auth_signature(SHARED_SECRET, "POST", "/verify", timestamp_text, nonce, body)
        if (
            not timestamp_text
            or not nonce
            or len(nonce) > 128
            or abs(int(time.time()) - timestamp) > AUTH_CLOCK_SKEW_SECONDS
            or not hmac.compare_digest(received_signature, expected_signature)
            or not nonce_is_fresh(nonce, timestamp)
        ):
            self.send_json({"ok": False, "error": "invalid_authentication"}, HTTPStatus.UNAUTHORIZED)
            return

        try:
            request = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json({"ok": False, "error": "invalid_body"}, HTTPStatus.BAD_REQUEST)
            return
        if not isinstance(request, dict):
            self.send_json({"ok": False, "error": "invalid_body"}, HTTPStatus.BAD_REQUEST)
            return
        qq = str(request.get("qq", "")).strip()
        student_id = str(request.get("student_id", "")).strip()
        users = load_users()
        if qq not in users:
            result = {"known": False, "reason": "qq_not_found"}
        else:
            expected_student, name = users[qq]
            result = {"known": True, "expected_student": expected_student, "name": name}
            if not expected_student:
                result["reason"] = "student_missing"
            elif student_id != expected_student:
                result["reason"] = "student_mismatch"
            else:
                result["reason"] = "ok"
        self.send_json({"ok": True, **result}, HTTPStatus.OK, auth_nonce=nonce)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the authenticated users.csv verification service.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Listen address (default: %(default)s)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Listen port (default: %(default)s)")
    args = parser.parse_args()
    if not SHARED_SECRET:
        parser.error("set USERS_CSV_SHARED_SECRET before starting the server")
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with ThreadingHTTPServer((args.host, args.port), RequestHandler) as server:
        LOGGER.info("server_started address=http://%s:%s csv=%s", args.host, args.port, CSV_PATH)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            LOGGER.info("server_stopped")


if __name__ == "__main__":
    main()
