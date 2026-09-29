from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, SPOT_CHECK_STATES, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        spot_statuses = ",".join("'" + s.replace("'", "''") + "'" for s in SPOT_CHECK_STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS spot_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    check_no TEXT NOT NULL UNIQUE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    description TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ({spot_statuses})),
                    item_version INTEGER NOT NULL,
                    closed_at TEXT,
                    closed_by TEXT,
                    close_reason TEXT,
                    decision_note TEXT,
                    decided_by TEXT,
                    decided_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_spot_check_open
                    ON spot_checks(item_id) WHERE status='pending';
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def create_spot_check(self, check_no: str, item_id: int, description: str,
                          reviewer: str, item_version: int, closed_at: str,
                          closed_by: str, close_reason: Optional[str],
                          actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            open_row = self.conn.execute(
                "SELECT id FROM spot_checks WHERE item_id=? AND status='pending'",
                (item_id,),
            ).fetchone()
            if open_row is not None:
                raise ConflictError("该缺陷已有未结抽检")
            try:
                cur = self.conn.execute(
                    """INSERT INTO spot_checks(check_no, item_id, description, reviewer,
                       status, item_version, closed_at, closed_by, close_reason,
                       created_by, created_at, updated_at, version)
                       VALUES(?,?,?,?, 'pending', ?,?,?,?,?,?,?,1)""",
                    (check_no, item_id, description, reviewer, item_version,
                     closed_at, closed_by, close_reason, actor, now, now),
                )
                check_id = int(cur.lastrowid)
            except sqlite3.IntegrityError as exc:
                raise ConflictError("抽检编号已存在") from exc
        return self.get_spot_check(check_id)

    def get_spot_check(self, check_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM spot_checks WHERE id=?", (check_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("抽检不存在")
        return dict(row)

    def get_open_spot_check(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM spot_checks WHERE item_id=? AND status='pending'",
                (item_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_spot_checks(self, item_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM spot_checks"
        params: tuple = ()
        if item_id is not None:
            sql += " WHERE item_id=?"
            params = (item_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def latest_transition_actor(self, item_id: int, to_status: str) -> Optional[str]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT actor, detail FROM audit_events
                   WHERE entity_id=? AND action='transition' ORDER BY id""",
                (item_id,),
            ).fetchall()
        for row in reversed(rows):
            try:
                detail = json.loads(row["detail"])
            except (TypeError, ValueError):
                continue
            if detail.get("to") == to_status:
                return row["actor"]
        return None

    def decide_spot_check(self, check_id: int, result: str, note: Optional[str],
                          actor: str, item_version_expected: int,
                          return_record: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """判定抽检；不通过时原子化退回维修、重计时并写入退回记录。"""
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM spot_checks WHERE id=?", (check_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("抽检不存在")
            if row["status"] != "pending":
                raise ConflictError("抽检已判定，不能重复判定")
            item = self.conn.execute(
                "SELECT * FROM items WHERE id=?", (row["item_id"],)
            ).fetchone()
            if item is None:
                raise NotFoundError("项目不存在")
            if item["version"] != item_version_expected:
                raise ConflictError("版本冲突，请刷新后重试")
            self.conn.execute(
                """UPDATE spot_checks SET status=?, decision_note=?, decided_by=?,
                   decided_at=?, updated_at=?, version=version+1 WHERE id=?""",
                (result, note, actor, now, now, check_id),
            )
            new_status = item["status"]
            new_version = item["version"]
            if result == "failed":
                cur = self.conn.execute(
                    """UPDATE items SET status='repair', version=version+1, updated_at=?
                       WHERE id=? AND version=?""",
                    (now, item["id"], item_version_expected),
                )
                if cur.rowcount == 0:
                    raise ConflictError("版本冲突，请刷新后重试")
                if return_record is not None:
                    self.conn.execute(
                        """INSERT INTO records(item_id, kind, detail, status, external_ref,
                           created_by, created_at) VALUES(?,?,?, 'closed', ?,?,?)""",
                        (item["id"], return_record["kind"], return_record["detail"],
                         return_record["external_ref"], actor, now),
                    )
                new_status = "repair"
                new_version = item["version"] + 1
        return {"spot_check": self.get_spot_check(check_id),
                "item_status": new_status, "item_version": new_version}

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
