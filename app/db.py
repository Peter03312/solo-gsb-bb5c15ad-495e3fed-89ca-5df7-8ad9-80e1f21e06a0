"""SQLite 持久层。

* retros / versions 两张表；versions.payload 保存「原始输入」，versions.result
  保存推算引擎的输出，二者分列、互不覆盖。
* 每次保存生成新版本号（version 单调递增）；更新时校验 base_version，旧版本
  提交直接拒绝（乐观锁，防多人互相覆盖）。
* 容器启动时调用 init_db() 自动建表（不存在才建），并可按配置写入示例复盘。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS retros (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    is_final    INTEGER NOT NULL DEFAULT 0,
    current_version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    retro_id    INTEGER NOT NULL REFERENCES retros(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    payload     TEXT    NOT NULL,   -- 原始输入（事件时间范围、时长、先于关系）
    result      TEXT    NOT NULL,   -- 推算结果（时间窗、依据链、冲突）
    status      TEXT    NOT NULL,   -- ok / draft / conflict
    created_at  TEXT    NOT NULL,
    author      TEXT    NOT NULL DEFAULT '',
    note        TEXT    NOT NULL DEFAULT '',
    UNIQUE (retro_id, version)
);

CREATE INDEX IF NOT EXISTS idx_versions_retro ON versions(retro_id, version);
"""

EMPTY_PAYLOAD = {"events": [], "relations": []}


def now_str() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    folder = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class VersionConflict(Exception):
    """提交基于的版本号已过期（HTTP 409）。"""


class NotFinalizable(Exception):
    """存在冲突或没有事件，不能定稿（HTTP 422）。"""


def init_db(conn: sqlite3.Connection, seed_demo: bool = True) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
    if seed_demo and conn.execute("SELECT COUNT(*) AS c FROM retros").fetchone()["c"] == 0:
        # 延迟导入避免循环依赖
        from .compute import compute

        rid = create_retro(conn, "示例：支付网关 10-03 故障复盘")
        payload = {
            "events": [
                {"id": "alert", "title": "监控告警触发",
                 "start_min": "2026-10-03T08:05", "start_max": "2026-10-03T08:10",
                 "duration_minutes": 5,
                 "note": "错误率超过阈值"},
                {"id": "locate", "title": "定位到数据库慢查询",
                 "start_min": "2026-10-03T08:00", "start_max": "2026-10-03T08:40",
                 "duration_minutes": 20,
                 "note": "缺少索引导致连接池打满"},
                {"id": "mitigate", "title": "限流并重启连接池",
                 "start_min": "2026-10-03T08:10", "start_max": "2026-10-03T09:30",
                 "duration_minutes": 15},
                {"id": "recover", "title": "服务恢复",
                 "start_min": "2026-10-03T08:20", "start_max": "2026-10-03T10:00",
                 "duration_minutes": 10},
                {"id": "postmortem", "title": "召开复盘会",
                 "start_min": None, "start_max": None,
                 "duration_minutes": None,
                 "note": "时间未定，先暂存"},
            ],
            "relations": [
                {"before": "alert", "after": "locate"},
                {"before": "locate", "after": "mitigate"},
                {"before": "mitigate", "after": "recover"},
                {"before": "recover", "after": "postmortem"},
            ],
        }
        save_version(conn, rid, 0, payload, compute(payload), author="system",
                     note="初始化示例")


# --------------------------------------------------------------------------- #
# 复盘（retro）
# --------------------------------------------------------------------------- #

def _row_to_retro(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "name": row["name"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "is_final": bool(row["is_final"]), "current_version": row["current_version"],
    }


def list_retros(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM retros ORDER BY updated_at DESC, id DESC").fetchall()
    return [_row_to_retro(r) for r in rows]


def create_retro(conn: sqlite3.Connection, name: str = "") -> int:
    ts = now_str()
    name = (name or "").strip() or "未命名复盘"
    cur = conn.execute(
        "INSERT INTO retros (name, created_at, updated_at) VALUES (?, ?, ?)",
        (name, ts, ts))
    conn.commit()
    return int(cur.lastrowid)


def rename_retro(conn: sqlite3.Connection, retro_id: int, name: str) -> None:
    name = (name or "").strip() or "未命名复盘"
    conn.execute("UPDATE retros SET name=?, updated_at=? WHERE id=?",
                 (name, now_str(), retro_id))
    conn.commit()


def get_retro(conn: sqlite3.Connection, retro_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM retros WHERE id=?", (retro_id,)).fetchone()
    return _row_to_retro(row) if row else None


def delete_retro(conn: sqlite3.Connection, retro_id: int) -> bool:
    cur = conn.execute("DELETE FROM retros WHERE id=?", (retro_id,))
    conn.commit()
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# 版本
# --------------------------------------------------------------------------- #

def save_version(conn: sqlite3.Connection, retro_id: int, base_version: int,
                 payload: dict, result: dict, author: str = "",
                 note: str = "") -> dict:
    """基于 base_version 保存新版本；过期提交抛 VersionConflict。

    原始输入与推算结果分别存入 payload / result 两列。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT current_version, is_final FROM retros WHERE id=?",
            (retro_id,)).fetchone()
        if row is None:
            raise LookupError(f"retro {retro_id} 不存在")
        if int(base_version) != int(row["current_version"]):
            raise VersionConflict(
                f"你基于第 {base_version} 版修改，但当前已是第 {row['current_version']} 版")
        new_version = int(row["current_version"]) + 1
        ts = now_str()
        conn.execute(
            "INSERT INTO versions (retro_id, version, payload, result, status, "
            "created_at, author, note) VALUES (?,?,?,?,?,?,?,?)",
            (retro_id, new_version, json.dumps(payload, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False), result["status"],
             ts, author or "", note or ""))
        conn.execute(
            "UPDATE retros SET current_version=?, updated_at=?, is_final=0 WHERE id=?",
            (new_version, ts, retro_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_version(conn, retro_id, new_version)


def finalize_version(conn: sqlite3.Connection, retro_id: int, version: int) -> dict:
    """将指定版本标记定稿；冲突版本拒绝定稿。"""
    v = get_version(conn, retro_id, version)
    if v is None:
        raise LookupError(f"版本 {version} 不存在")
    if v["status"] == "conflict":
        raise NotFinalizable("该版本存在约束冲突，不能定稿；请先解决冲突再保存")
    if not v["payload"]["events"]:
        raise NotFinalizable("复盘还没有任何事件，不能定稿")
    conn.execute("UPDATE retros SET is_final=1, updated_at=? WHERE id=?",
                 (now_str(), retro_id))
    conn.commit()
    return v


def unfinalize(conn: sqlite3.Connection, retro_id: int) -> None:
    conn.execute("UPDATE retros SET is_final=0 WHERE id=?", (retro_id,))
    conn.commit()


def _row_to_version(row: sqlite3.Row) -> dict:
    return {
        "version": row["version"], "status": row["status"],
        "payload": json.loads(row["payload"]),
        "result": json.loads(row["result"]),
        "created_at": row["created_at"], "author": row["author"],
        "note": row["note"],
    }


def get_version(conn: sqlite3.Connection, retro_id: int, version: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM versions WHERE retro_id=? AND version=?",
        (retro_id, version)).fetchone()
    return _row_to_version(row) if row else None


def get_latest_version(conn: sqlite3.Connection, retro_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM versions WHERE retro_id=? ORDER BY version DESC LIMIT 1",
        (retro_id,)).fetchone()
    return _row_to_version(row) if row else None


def list_versions(conn: sqlite3.Connection, retro_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT version, status, created_at, author, note FROM versions "
        "WHERE retro_id=? ORDER BY version DESC", (retro_id,)).fetchall()
    return [dict(r) for r in rows]
