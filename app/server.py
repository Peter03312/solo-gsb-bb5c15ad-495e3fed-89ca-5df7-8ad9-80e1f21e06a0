"""事故复盘时间线 —— HTTP 服务（仅用 Python 标准库）。

提供 JSON API 与浏览器静态页面，启动时自动初始化 SQLite 数据库。
配置项见 README（环境变量 PORT / DB_PATH / SEED_DEMO / HOST）。
"""

from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import db
from .compute import PayloadError, compute

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

DB_PATH = os.environ.get("DB_PATH", "/data/timeline.db")
SEED_DEMO = os.environ.get("SEED_DEMO", "1") not in ("0", "false", "False", "")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8800"))

# 每个请求新建 SQLite 连接（线程内使用），简单稳妥
_local = threading.local()


def conn():
    c = getattr(_local, "conn", None)
    if c is None:
        c = db.connect(DB_PATH)
        _local.conn = c
    return c


class HttpError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# API 处理
# --------------------------------------------------------------------------- #

def _retro_or_404(retro_id: int) -> dict:
    retro = db.get_retro(conn(), retro_id)
    if retro is None:
        raise HttpError(404, f"复盘 #{retro_id} 不存在")
    return retro


def _bundle(retro_id: int) -> dict:
    retro = _retro_or_404(retro_id)
    version = db.get_latest_version(conn(), retro_id)
    return {"retro": retro, "version": version}


def api_list_retros() -> dict:
    retros = db.list_retros(conn())
    # 附带最新版本状态，供列表状态点使用
    for r in retros:
        v = db.get_latest_version(conn(), r["id"])
        r["status_hint"] = v["status"] if v else "ok"
    return {"retros": retros}


def api_create_retro(body: dict) -> tuple[int, dict]:
    rid = db.create_retro(conn(), str(body.get("name") or ""))
    v = db.save_version(conn(), rid, 0, db.EMPTY_PAYLOAD,
                        compute(db.EMPTY_PAYLOAD),
                        author=str(body.get("author") or ""), note="创建复盘")
    return 201, _bundle(rid)


def api_get_retro(retro_id: int) -> dict:
    return _bundle(retro_id)


def api_rename_retro(retro_id: int, body: dict) -> dict:
    _retro_or_404(retro_id)
    db.rename_retro(conn(), retro_id, str(body.get("name") or ""))
    return _bundle(retro_id)


def api_delete_retro(retro_id: int) -> dict:
    _retro_or_404(retro_id)
    db.delete_retro(conn(), retro_id)
    return {"deleted": retro_id}


def api_list_versions(retro_id: int) -> dict:
    _retro_or_404(retro_id)
    return {"versions": db.list_versions(conn(), retro_id)}


def api_get_one_version(retro_id: int, version: int) -> dict:
    _retro_or_404(retro_id)
    v = db.get_version(conn(), retro_id, version)
    if v is None:
        raise HttpError(404, f"版本 {version} 不存在")
    return {"retro": _retro_or_404(retro_id), "version": v}


def api_save(retro_id: int, body: dict) -> tuple[int, dict]:
    _retro_or_404(retro_id)
    if "base_version" not in body:
        raise HttpError(400, "缺少 base_version（防止覆盖他人的新记录）")
    try:
        base_version = int(body["base_version"])
    except (TypeError, ValueError):
        raise HttpError(400, "base_version 必须是整数")
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise HttpError(400, "缺少 payload（事件与关系的原始输入）")
    # 先推算：校验失败 → 400；冲突不阻断保存，只是新版本不能定稿
    result = compute(payload)
    version = db.save_version(
        conn(), retro_id, base_version, payload, result,
        author=str(body.get("author") or ""), note=str(body.get("note") or ""))
    return 200, _bundle(retro_id) | {"saved_version": version}


def api_preview(body: dict) -> dict:
    """只推算不落库，供前端编辑时实时预览时间窗与依据链。"""
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise HttpError(400, "缺少 payload")
    return {"result": compute(payload)}


def api_finalize(retro_id: int, body: dict) -> dict:
    _retro_or_404(retro_id)
    version = body.get("version")
    if version is None:
        retro = _retro_or_404(retro_id)
        version = retro["current_version"]
    try:
        db.finalize_version(conn(), retro_id, int(version))
    except LookupError as e:
        raise HttpError(404, str(e))
    except db.NotFinalizable as e:
        raise HttpError(422, str(e))
    return _bundle(retro_id)


# --------------------------------------------------------------------------- #
# 路由 & HTTP 适配
# --------------------------------------------------------------------------- #

ROUTES = [
    ("GET",    r"^/api/retros$",                       lambda b, m: api_list_retros()),
    ("POST",   r"^/api/retros$",                       lambda b, m: api_create_retro(b)),
    ("GET",    r"^/api/retros/(?P<id>\d+)$",           lambda b, m: api_get_retro(int(m["id"]))),
    ("PATCH",  r"^/api/retros/(?P<id>\d+)$",           lambda b, m: api_rename_retro(int(m["id"]), b)),
    ("DELETE", r"^/api/retros/(?P<id>\d+)$",           lambda b, m: api_delete_retro(int(m["id"]))),
    ("GET",    r"^/api/retros/(?P<id>\d+)/versions$",  lambda b, m: api_list_versions(int(m["id"]))),
    ("GET",    r"^/api/retros/(?P<id>\d+)/versions/(?P<v>\d+)$",
              lambda b, m: api_get_one_version(int(m["id"]), int(m["v"]))),
    ("PUT",    r"^/api/retros/(?P<id>\d+)/versions$",  lambda b, m: api_save(int(m["id"]), b)),
    ("POST",   r"^/api/retros/(?P<id>\d+)/finalize$",  lambda b, m: api_finalize(int(m["id"]), b)),
    ("POST",   r"^/api/compute/preview$",              lambda b, m: api_preview(b)),
]

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "IncidentTimeline/1.0"

    def log_message(self, fmt, *args):  # 简洁日志
        print("[http] %s - %s" % (self.address_string(), fmt % args))

    # ---- 响应工具 ----
    def _send_json(self, code: int, obj: dict):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_static(self, path: str):
        ext = os.path.splitext(path)[1]
        try:
            with open(path, "rb") as f:
                data = f.read()
        except (FileNotFoundError, IsADirectoryError):
            self._send_json(404, {"error": "资源不存在"})
            return
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HttpError(400, "请求体不是合法的 JSON")
        if not isinstance(body, dict):
            raise HttpError(400, "请求体必须是 JSON 对象")
        return body

    # ---- 统一入口 ----
    def _dispatch(self, method: str):
        path = urlparse(self.path).path
        try:
            for verb, pattern, fn in ROUTES:
                if verb != method:
                    continue
                m = re.match(pattern, path)
                if m:
                    body = self._read_body() if method in ("POST", "PUT", "PATCH") else {}
                    outcome = fn(body, m.groupdict())
                    if isinstance(outcome, tuple):
                        code, obj = outcome
                    else:
                        code, obj = 200, outcome
                    self._send_json(code, obj)
                    return
            if method == "GET":
                self._serve_file(path)
            else:
                self._send_json(405, {"error": f"不支持 {method} {path}"})
        except HttpError as e:
            self._send_json(e.code, {"error": e.message})
        except PayloadError as e:
            self._send_json(400, {"error": str(e)})
        except db.VersionConflict as e:
            self._send_json(409, {"error": str(e), "code": "version_conflict"})
        except LookupError as e:
            self._send_json(404, {"error": str(e)})
        except Exception as e:  # noqa: BLE001 — 兜底，避免连接挂死
            self._send_json(500, {"error": f"服务器内部错误：{e}"})

    def _serve_file(self, path: str):
        if path in ("/", ""):
            self._send_static(os.path.join(STATIC_DIR, "index.html"))
            return
        # 防目录穿越：只允许 STATIC_DIR 内的普通文件
        rel = path.lstrip("/")
        target = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not target.startswith(STATIC_DIR + os.sep) or not os.path.isfile(target):
            self._send_json(404, {"error": "资源不存在"})
            return
        self._send_static(target)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")


def main():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    init_conn = db.connect(DB_PATH)
    db.init_db(init_conn, seed_demo=SEED_DEMO)
    init_conn.close()
    print(f"[init] 数据库就绪：{DB_PATH}（示例数据：{'是' if SEED_DEMO else '否'}）")

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[init] 事故复盘时间线已启动：http://localhost:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[init] 收到中断，退出")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
