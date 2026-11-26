from __future__ import annotations

from contextlib import contextmanager
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator

from werkzeug.security import check_password_hash, generate_password_hash


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class OfflineSyncStore:
    """Thread-safe local SQLite persistence for offline caching and automatic sync."""

    SYNC_TABLES = (
        "residents",
        "documents",
        "concerns",
        "announcements",
        "weather_alerts",
        "officials",
    )

    def __init__(self, db_path: str | None = None) -> None:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        if os.getenv("VERCEL"):
            # Vercel's project folder is read-only; /tmp is the only writable place.
            base_dir = "/tmp"
        self.db_path = db_path or os.path.join(base_dir, "offline_sync.db")
        self._lock = threading.RLock()
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cached_records (
                    table_name TEXT NOT NULL,
                    record_key TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    is_local_only INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (table_name, record_key)
                );

                CREATE TABLE IF NOT EXISTS sync_queue (
                    op_id TEXT PRIMARY KEY,
                    client_uid TEXT,
                    table_name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    row_id TEXT,
                    payload_json TEXT NOT NULL,
                    base_snapshot_json TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sync_queue_status
                    ON sync_queue (status, created_at);

                CREATE TABLE IF NOT EXISTS synced_id_map (
                    client_uid TEXT PRIMARY KEY,
                    table_name TEXT NOT NULL,
                    local_id TEXT,
                    remote_id TEXT NOT NULL,
                    synced_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS offline_users (
                    email TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    profile_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    # ------------------------------------------------------------------
    # Metadata helpers
    # ------------------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM sync_meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_meta(self, key: str, value: str | None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO sync_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, "" if value is None else str(value)),
            )

    LOCAL_TEMP_ID_START = 900_000_001

    @classmethod
    def is_local_temp_id(cls, row_id: Any) -> bool:
        try:
            return int(row_id) >= cls.LOCAL_TEMP_ID_START
        except (TypeError, ValueError):
            return False

    def next_local_temp_id(self) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM sync_meta WHERE key = 'next_local_id'").fetchone()
            current = int(row["value"]) if row and row["value"] else self.LOCAL_TEMP_ID_START
            if current < self.LOCAL_TEMP_ID_START:
                current = self.LOCAL_TEMP_ID_START
            next_val = current + 1
            conn.execute(
                "INSERT INTO sync_meta (key, value) VALUES ('next_local_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(next_val),),
            )
            return current

    # ------------------------------------------------------------------
    # Offline official authentication cache
    # ------------------------------------------------------------------
    def cache_official_credentials(self, email: str, password: str, profile: dict[str, Any]) -> None:
        clean_email = str(email or "").strip().lower()
        if not clean_email or not password:
            return
        pw_hash = generate_password_hash(password)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO offline_users (email, password_hash, profile_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    password_hash = excluded.password_hash,
                    profile_json = excluded.profile_json,
                    updated_at = excluded.updated_at
                """,
                (clean_email, pw_hash, json.dumps(profile, default=str), _utc_now_iso()),
            )

    def verify_offline_login(self, email: str, password: str) -> dict[str, Any] | None:
        clean_email = str(email or "").strip().lower()
        if not clean_email or not password:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT password_hash, profile_json FROM offline_users WHERE email = ?",
                (clean_email,),
            ).fetchone()
            if not row:
                return None
            if not check_password_hash(row["password_hash"], password):
                return None
            return json.loads(row["profile_json"])

    # ------------------------------------------------------------------
    # Cached table records (data protection: never drop unsynced changes)
    # ------------------------------------------------------------------
    @staticmethod
    def _record_key(table_name: str, record: dict[str, Any], index_fallback: int = 0) -> str:
        for field in ("id", "email", "resident_id", "request_id", "concern_id"):
            val = record.get(field)
            if val is not None and str(val).strip() != "":
                return str(val)
        return f"{table_name}-row-{index_fallback}"

    def _pending_row_keys(self, conn: sqlite3.Connection, table_name: str) -> set[str]:
        rows = conn.execute(
            "SELECT row_id FROM sync_queue WHERE table_name = ? AND status IN ('pending', 'failed', 'syncing')",
            (table_name,),
        ).fetchall()
        return {str(r["row_id"]) for r in rows if r["row_id"] is not None}

    def save_table_snapshot(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        """Persist fresh synchronized Supabase rows while protecting any unsynced local rows/edits."""
        now = _utc_now_iso()
        with self._lock, self._connect() as conn:
            pending_keys = self._pending_row_keys(conn, table_name)
            incoming_keys: set[str] = set()
            for idx, rec in enumerate(rows):
                rkey = self._record_key(table_name, rec, idx)
                incoming_keys.add(rkey)
                # Protect rows with pending local edits from being silently clobbered before sync completes
                if rkey in pending_keys:
                    continue
                conn.execute(
                    """
                    INSERT INTO cached_records (table_name, record_key, record_json, updated_at, is_local_only)
                    VALUES (?, ?, ?, ?, 0)
                    ON CONFLICT(table_name, record_key) DO UPDATE SET
                        record_json = excluded.record_json,
                        updated_at = excluded.updated_at,
                        is_local_only = 0
                    """,
                    (table_name, rkey, json.dumps(rec, default=str), now),
                )

            # Remove stale remote-only rows that were deleted on Supabase,
            # while NEVER removing local-only unsynced records or pending-mutation rows.
            existing = conn.execute(
                "SELECT record_key, is_local_only FROM cached_records WHERE table_name = ?",
                (table_name,),
            ).fetchall()
            for row in existing:
                rkey = str(row["record_key"])
                if row["is_local_only"] or rkey in pending_keys:
                    continue
                if rkey not in incoming_keys:
                    conn.execute(
                        "DELETE FROM cached_records WHERE table_name = ? AND record_key = ?",
                        (table_name, rkey),
                    )

    def list_cached(self, table_name: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT record_json FROM cached_records WHERE table_name = ?",
                (table_name,),
            ).fetchall()
            result = [json.loads(r["record_json"]) for r in rows]

        # Keep most recent first (matching Supabase desc order on id / created_at)
        def _sort_key(item: dict[str, Any]) -> tuple[int, str]:
            created = str(item.get("created_at") or item.get("date_requested") or item.get("issued_at") or "")
            raw_id = item.get("id")
            try:
                num_id = int(raw_id)
                if self.is_local_temp_id(num_id):
                    return (2, str(num_id).zfill(12))
                return (1, str(num_id).zfill(12))
            except (TypeError, ValueError):
                return (0, created)

        result.sort(key=_sort_key, reverse=True)
        return result

    def get_cached_row(self, table_name: str, row_id: Any) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT record_json FROM cached_records WHERE table_name = ? AND record_key = ?",
                (table_name, str(row_id)),
            ).fetchone()
            return json.loads(row["record_json"]) if row else None

    def upsert_cached_row(self, table_name: str, record: dict[str, Any], is_local_only: bool = False) -> None:
        rkey = self._record_key(table_name, record)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cached_records (table_name, record_key, record_json, updated_at, is_local_only)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(table_name, record_key) DO UPDATE SET
                    record_json = excluded.record_json,
                    updated_at = excluded.updated_at,
                    is_local_only = CASE
                        WHEN cached_records.is_local_only = 1 AND excluded.is_local_only = 1 THEN 1
                        ELSE excluded.is_local_only
                    END
                """,
                (table_name, rkey, json.dumps(record, default=str), _utc_now_iso(), 1 if is_local_only else 0),
            )

    def delete_cached_row(self, table_name: str, row_id: Any) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM cached_records WHERE table_name = ? AND record_key = ?",
                (table_name, str(row_id)),
            )

    # ------------------------------------------------------------------
    # Durable Sync Queue (idempotent + coalescing)
    # ------------------------------------------------------------------
    def enqueue_insert(
        self,
        table_name: str,
        local_record: dict[str, Any],
        payload: dict[str, Any],
        client_uid: str | None = None,
    ) -> dict[str, Any]:
        uid = client_uid or str(uuid.uuid4())
        op_id = str(uuid.uuid4())
        now = _utc_now_iso()
        record_to_store = {
            **local_record,
            "_offline_pending": True,
            "_client_uid": uid,
        }
        self.upsert_cached_row(table_name, record_to_store, is_local_only=True)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_queue (
                    op_id, client_uid, table_name, action, row_id,
                    payload_json, base_snapshot_json, status, retry_count,
                    last_error, created_at, updated_at
                ) VALUES (?, ?, ?, 'insert', ?, ?, NULL, 'pending', 0, NULL, ?, ?)
                """,
                (
                    op_id,
                    uid,
                    table_name,
                    str(local_record.get("id")),
                    json.dumps(payload, default=str),
                    now,
                    now,
                ),
            )
        return record_to_store

    def enqueue_update(
        self,
        table_name: str,
        row_id: Any,
        updates: dict[str, Any],
        base_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _utc_now_iso()
        str_id = str(row_id)
        existing_cached = self.get_cached_row(table_name, str_id) or (dict(base_snapshot) if base_snapshot else {"id": row_id})
        merged_local = {
            **existing_cached,
            **updates,
            "_offline_pending": True,
        }
        is_temp_local = self.is_local_temp_id(row_id)

        self.upsert_cached_row(table_name, merged_local, is_local_only=is_temp_local)

        with self._lock, self._connect() as conn:
            # If this record was created offline and not yet synced, merge updates directly into its pending INSERT
            pending_insert = conn.execute(
                """
                SELECT op_id, payload_json FROM sync_queue
                WHERE table_name = ? AND row_id = ? AND action = 'insert' AND status IN ('pending', 'failed')
                ORDER BY created_at DESC LIMIT 1
                """,
                (table_name, str_id),
            ).fetchone()
            if pending_insert:
                cur_payload = json.loads(pending_insert["payload_json"])
                cur_payload.update(updates)
                conn.execute(
                    "UPDATE sync_queue SET payload_json = ?, status = 'pending', last_error = NULL, updated_at = ? WHERE op_id = ?",
                    (json.dumps(cur_payload, default=str), now, pending_insert["op_id"]),
                )
                return merged_local

            # If there is already a pending UPDATE for the same row, coalesce field changes (keeps original base_snapshot!)
            pending_update = conn.execute(
                """
                SELECT op_id, payload_json FROM sync_queue
                WHERE table_name = ? AND row_id = ? AND action = 'update' AND status IN ('pending', 'failed')
                ORDER BY created_at DESC LIMIT 1
                """,
                (table_name, str_id),
            ).fetchone()
            if pending_update:
                cur_payload = json.loads(pending_update["payload_json"])
                cur_payload.update(updates)
                conn.execute(
                    "UPDATE sync_queue SET payload_json = ?, status = 'pending', last_error = NULL, updated_at = ? WHERE op_id = ?",
                    (json.dumps(cur_payload, default=str), now, pending_update["op_id"]),
                )
                return merged_local

            conn.execute(
                """
                INSERT INTO sync_queue (
                    op_id, client_uid, table_name, action, row_id,
                    payload_json, base_snapshot_json, status, retry_count,
                    last_error, created_at, updated_at
                ) VALUES (?, ?, ?, 'update', ?, ?, ?, 'pending', 0, NULL, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    str(uuid.uuid4()),
                    table_name,
                    str_id,
                    json.dumps(updates, default=str),
                    json.dumps(base_snapshot or existing_cached, default=str),
                    now,
                    now,
                ),
            )
        return merged_local

    def enqueue_delete(self, table_name: str, row_id: Any) -> None:
        now = _utc_now_iso()
        str_id = str(row_id)
        self.delete_cached_row(table_name, str_id)
        with self._lock, self._connect() as conn:
            # If deleting a record that was only created offline and never pushed, simply cancel its queued ops
            pending_insert = conn.execute(
                "SELECT op_id FROM sync_queue WHERE table_name = ? AND row_id = ? AND action = 'insert' AND status IN ('pending', 'failed')",
                (table_name, str_id),
            ).fetchone()
            if pending_insert:
                conn.execute(
                    "DELETE FROM sync_queue WHERE table_name = ? AND row_id = ? AND status IN ('pending', 'failed')",
                    (table_name, str_id),
                )
                return
            conn.execute(
                """
                INSERT INTO sync_queue (
                    op_id, client_uid, table_name, action, row_id,
                    payload_json, base_snapshot_json, status, retry_count,
                    last_error, created_at, updated_at
                ) VALUES (?, ?, ?, 'delete', ?, '{}', NULL, 'pending', 0, NULL, ?, ?)
                """,
                (str(uuid.uuid4()), str(uuid.uuid4()), table_name, str_id, now, now),
            )

    def list_pending_ops(self, include_failed: bool = True) -> list[dict[str, Any]]:
        statuses = ("pending", "failed") if include_failed else ("pending",)
        placeholders = ",".join("?" for _ in statuses)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM sync_queue WHERE status IN ({placeholders}) ORDER BY created_at ASC",
                statuses,
            ).fetchall()
            ops = []
            for r in rows:
                d = dict(r)
                d["payload"] = json.loads(d.pop("payload_json") or "{}")
                d["base_snapshot"] = json.loads(d.pop("base_snapshot_json")) if d.get("base_snapshot_json") else None
                d.pop("base_snapshot_json", None)
                ops.append(d)
            return ops

    def lookup_synced_remote_id(self, client_uid: str | None) -> str | None:
        if not client_uid:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT remote_id FROM synced_id_map WHERE client_uid = ?",
                (str(client_uid),),
            ).fetchone()
            return str(row["remote_id"]) if row else None

    def record_synced_id(self, client_uid: str | None, table_name: str, local_id: Any, remote_id: Any) -> None:
        if not client_uid:
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO synced_id_map (client_uid, table_name, local_id, remote_id, synced_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(client_uid) DO UPDATE SET
                    remote_id = excluded.remote_id,
                    synced_at = excluded.synced_at
                """,
                (str(client_uid), table_name, str(local_id), str(remote_id), _utc_now_iso()),
            )
            # Re-point any remaining queued updates/deletes referencing local_id to the real remote_id
            if local_id is not None and str(local_id) != str(remote_id):
                conn.execute(
                    "UPDATE sync_queue SET row_id = ? WHERE table_name = ? AND row_id = ? AND status IN ('pending', 'failed')",
                    (str(remote_id), table_name, str(local_id)),
                )
                conn.execute(
                    "DELETE FROM cached_records WHERE table_name = ? AND record_key = ?",
                    (table_name, str(local_id)),
                )

    def mark_op_synced(self, op_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE sync_queue SET status = 'synced', last_error = NULL, updated_at = ? WHERE op_id = ?",
                (_utc_now_iso(), op_id),
            )

    def mark_op_failed(self, op_id: str, error_message: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE sync_queue
                SET status = 'failed',
                    retry_count = retry_count + 1,
                    last_error = ?,
                    updated_at = ?
                WHERE op_id = ?
                """,
                (str(error_message)[:500], _utc_now_iso(), op_id),
            )

    def get_queue_summary(self) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            counts = {"pending": 0, "failed": 0, "synced": 0}
            for row in conn.execute("SELECT status, COUNT(*) AS cnt FROM sync_queue GROUP BY status").fetchall():
                st = str(row["status"])
                if st in counts:
                    counts[st] = int(row["cnt"])
            recent = [
                {
                    "op_id": r["op_id"],
                    "table_name": r["table_name"],
                    "action": r["action"],
                    "row_id": r["row_id"],
                    "status": r["status"],
                    "last_error": r["last_error"],
                    "updated_at": r["updated_at"],
                }
                for r in conn.execute(
                    "SELECT op_id, table_name, action, row_id, status, last_error, updated_at "
                    "FROM sync_queue ORDER BY updated_at DESC LIMIT 10"
                ).fetchall()
            ]
            cached_counts = {}
            for row in conn.execute("SELECT table_name, COUNT(*) AS cnt FROM cached_records GROUP BY table_name").fetchall():
                cached_counts[str(row["table_name"])] = int(row["cnt"])
            return {
                "pending_count": counts["pending"],
                "failed_count": counts["failed"],
                "synced_count": counts["synced"],
                "unsynced_total": counts["pending"] + counts["failed"],
                "cached_counts": cached_counts,
                "recent_ops": recent,
                "last_synced_at": self.get_meta("last_synced_at"),
                "last_sync_status": self.get_meta("last_sync_status", "online"),
                "last_sync_error": self.get_meta("last_sync_error"),
            }
