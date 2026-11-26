import calendar
import json
import os
import re
import threading
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any

from dotenv import load_dotenv
from supabase import Client, create_client
from offline_sync_store import OfflineSyncStore
from time_utils import manila_month_key, manila_today

DOCUMENT_POLICY: dict[str, dict[str, Any]] = {
    "Barangay Clearance": {"fee": 100.0, "validity_months": 6, "validity_label": "6 months"},
    "Certificate of Indigency": {"fee": 0.0, "validity_months": 6, "validity_label": "6 months"},
    "Certificate of Residency": {"fee": 100.0, "validity_months": 6, "validity_label": "6 months"},
    "Business Clearance": {"fee": 500.0, "validity_months": 12, "validity_label": "1 year"},
}


def get_document_policy(document_type: str | None) -> dict[str, Any]:
    key = str(document_type or "").strip()
    return dict(DOCUMENT_POLICY.get(key, {"fee": 100.0, "validity_months": 6, "validity_label": "6 months"}))


def add_calendar_months(base_date: date, months: int) -> date:
    m0 = base_date.month - 1 + int(months)
    year = base_date.year + m0 // 12
    month = (m0 % 12) + 1
    day = min(base_date.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)

env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(env_path)
load_dotenv()


class RepositoryConfigurationError(RuntimeError):
    """Raised when a server-side Supabase credential is not available."""


class RepositoryConflictError(RuntimeError):
    """Raised when a requested record would violate an application policy."""


class RepositoryRateLimitError(RuntimeError):
    """Raised when a user has reached a configured daily limit."""


class SupabaseRepository:
    def __init__(self) -> None:
        # Supabase configuration
        self.url = (
            os.getenv("SUPABASE_URL")
            or "https://juvuvrtpliztrbiegcsn.supabase.co"
        ).strip()

        # The Flask backend requires a server-side key for privileged
        # operations such as creating accounts and updating records.
        self.key = (
            os.getenv("SUPABASE_SERVICE_ROLE_KEY")
            or os.getenv("SUPABASE_KEY")
            or ""
        ).strip()

        self.client: Client | None = None

        if self.url and self.key and not self._is_placeholder(self.key):
            self.client = create_client(self.url, self.key)

        self.using_service_role = self._is_server_key(self.key)
        self.offline_store = OfflineSyncStore()
        self._is_online: bool = True
        self._sync_in_progress: bool = False
        self._sync_lock = threading.Lock()
        self._init_local_storage()

    @staticmethod
    def _is_network_error(error: Exception) -> bool:
        msg = f"{type(error).__name__}: {error}".lower()
        markers = (
            "connecterror",
            "connecttimeout",
            "readtimeout",
            "timeoutexception",
            "remoteprotocolerror",
            "networkerror",
            "connectionerror",
            "nameresolutionerror",
            "getaddrinfo",
            "temporary failure in name resolution",
            "nodename nor servname",
            "failed to establish a new connection",
            "network is unreachable",
            "winerror 10051",
            "winerror 10060",
            "winerror 10061",
            "winerror 11001",
            "errno 11001",
            "eof occurred in violation of protocol",
            "connection reset",
            "connection aborted",
            "server disconnected",
            "ssl",
            "502 bad gateway",
            "503 service unavailable",
            "504 gateway timeout",
        )
        return any(m in msg for m in markers)

    def _mark_online(self) -> None:
        self._is_online = True
        if not self._sync_in_progress:
            cur = self.offline_store.get_meta("last_sync_status", "online")
            if cur == "offline":
                self.offline_store.set_meta("last_sync_status", "online")

    def _mark_offline(self, reason: str | None = None) -> None:
        self._is_online = False
        self.offline_store.set_meta("last_sync_status", "offline")
        if reason:
            self.offline_store.set_meta("last_sync_error", str(reason)[:300])

    @staticmethod
    def _local_path(filename: str) -> str:
        src = os.path.join(os.path.dirname(__file__), filename)
        if not os.getenv("VERCEL"):
            return src
        # Read-only deployment folder: keep a writable copy in /tmp.
        dst = os.path.join("/tmp", filename)
        try:
            if not os.path.exists(dst) and os.path.exists(src):
                import shutil
                shutil.copyfile(src, dst)
        except OSError:
            pass
        return dst

    def _init_local_storage(self) -> None:
        self._meta_path = self._local_path("residents_metadata.json")
        self._resident_meta: dict[str, dict[str, Any]] = self._load_resident_meta()
        self._doc_meta_path = self._local_path("documents_metadata.json")
        self._doc_meta: dict[str, dict[str, Any]] = self._load_doc_meta()
        self._concern_meta_path = self._local_path("concerns_metadata.json")
        self._concern_meta: dict[str, dict[str, Any]] = self._load_concern_meta()
        self._ann_meta_path = self._local_path("announcements_metadata.json")
        self._ann_meta: dict[str, dict[str, Any]] = self._load_ann_meta()
        self._official_meta_path = self._local_path("officials_metadata.json")
        self._official_meta: dict[str, dict[str, Any]] = self._load_official_meta()

    def _load_official_meta(self) -> dict[str, dict[str, Any]]:
        if os.path.exists(self._official_meta_path):
            try:
                with open(self._official_meta_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    return saved
            except Exception:
                pass
        return {}

    def _save_official_meta(self) -> None:
        try:
            with open(self._official_meta_path, "w", encoding="utf-8") as f:
                json.dump(self._official_meta, f, indent=2)
        except Exception:
            pass

    def _update_official_meta(self, official_id: Any, data: dict[str, Any]) -> None:
        if official_id is None:
            return
        self._official_meta = self._load_official_meta()
        updates = {k: data[k] for k in ("status", "phone", "position", "department") if k in data and data[k] is not None}
        if not updates:
            return
        key = str(official_id)
        entry = self._official_meta.get(key, {})
        entry.update(updates)
        self._official_meta[key] = entry
        self._save_official_meta()

    def is_official_archived(self, user_id: Any, email: str | None = None) -> bool:
        meta = self._load_official_meta()
        if user_id:
            entry = meta.get(str(user_id), {})
            if str(entry.get("status") or "Active").strip().lower() == "archived":
                return True
        if email:
            em_key = f"email:{email.strip().lower()}"
            entry = meta.get(em_key, {})
            if str(entry.get("status") or "Active").strip().lower() == "archived":
                return True
        return False

    def _load_ann_meta(self) -> dict[str, dict[str, Any]]:
        if os.path.exists(self._ann_meta_path):
            try:
                with open(self._ann_meta_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    return saved
            except Exception:
                pass
        return {}

    def _save_ann_meta(self) -> None:
        try:
            with open(self._ann_meta_path, "w", encoding="utf-8") as f:
                json.dump(self._ann_meta, f, indent=2)
        except Exception:
            pass

    def _update_ann_meta(self, row_id: Any, data: dict[str, Any]) -> None:
        if row_id is None:
            return
        self._ann_meta = self._load_ann_meta()
        updates = {k: data[k] for k in ("is_urgent", "is_pinned", "status") if k in data}
        if not updates:
            return
        key = str(row_id)
        current = dict(self._ann_meta.get(key, {}))
        current.update(updates)
        self._ann_meta[key] = current
        self._save_ann_meta()

    def _apply_ann_meta(self, row: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(row)
        key = str(enriched.get("id", ""))
        meta = self._ann_meta.get(key, {})
        for k, v in meta.items():
            enriched[k] = v
        # Normalize legacy Expired -> Archived and infer Urgent for Emergency category if unset
        raw_status = str(enriched.get("status") or "Active").strip()
        if raw_status.lower() in ("expired", "archived"):
            enriched["status"] = "Archived"
        else:
            enriched["status"] = "Active"
        if "is_urgent" not in enriched:
            enriched["is_urgent"] = str(enriched.get("category") or "").strip().lower() == "emergency"
        else:
            enriched["is_urgent"] = bool(enriched["is_urgent"])
        enriched["is_pinned"] = bool(enriched.get("is_pinned", False))
        return enriched

    def _load_concern_meta(self) -> dict[str, dict[str, Any]]:
        if os.path.exists(self._concern_meta_path):
            try:
                with open(self._concern_meta_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    return saved
            except Exception:
                pass
        return {}

    def _save_concern_meta(self) -> None:
        try:
            with open(self._concern_meta_path, "w", encoding="utf-8") as f:
                json.dump(self._concern_meta, f, indent=2)
        except Exception:
            pass

    def _update_concern_meta(self, row_id: Any, data: dict[str, Any]) -> None:
        if row_id is None:
            return
        concern_cols = (
            "assigned_to",
            "official_feedback",
            "feedback_by",
            "feedback_at",
            "appointment_date",
            "appointment_time",
            "appointment_notes",
            "appointment_status",
            "reschedule_note",
            "resolution_remarks",
            "updated_at",
        )
        updates = {k: data[k] for k in concern_cols if k in data}
        if not updates:
            return
        key = str(row_id)
        current = dict(self._concern_meta.get(key, {}))
        current.update(updates)
        self._concern_meta[key] = current
        self._save_concern_meta()

    def _apply_concern_meta(self, row: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(row)
        key = str(enriched.get("id", ""))
        meta = self._concern_meta.get(key, {})
        for k, v in meta.items():
            enriched[k] = v
        return enriched

    def _load_doc_meta(self) -> dict[str, dict[str, Any]]:
        if os.path.exists(self._doc_meta_path):
            try:
                with open(self._doc_meta_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    return saved
            except Exception:
                pass
        return {}

    def _save_doc_meta(self) -> None:
        try:
            with open(self._doc_meta_path, "w", encoding="utf-8") as f:
                json.dump(self._doc_meta, f, indent=2)
        except Exception:
            pass

    def _update_doc_meta(self, row_id: Any, data: dict[str, Any]) -> None:
        if row_id is None:
            return
        doc_cols = (
            "status",
            "tx_ref",
            "amount_due",
            "amount_paid",
            "resident_email",
            "payment_status",
            "payment_method",
            "receipt_ref_no",
            "receipt_amount",
            "receipt_date",
            "receipt_filename",
            "receipt_file_url",
            "receipt_submitted_at",
            "receipt_auto_checks",
            "email_logs",
            "transaction_history",
            "supporting_doc_type",
            "verification_status",
            "checklist_summary",
            "verified_by",
            "verified_at",
            "issued_at",
            "expires_at",
            "validity_months",
            "validity_label",
            "action_performed",
            "verification_remarks",
            "manual_checks",
        )
        updates = {k: data[k] for k in doc_cols if k in data}
        if not updates:
            return
        key = str(row_id)
        current = dict(self._doc_meta.get(key, {}))
        current.update(updates)
        self._doc_meta[key] = current
        self._save_doc_meta()

    def _resolve_account_email(self, requester_id: Any, resident_name: str | None) -> str:
        import time

        now = time.time()
        cache = getattr(self, "_account_email_cache", None)
        if not cache or (now - cache.get("ts", 0)) > 30:
            id_map: dict[str, str] = {}
            name_map: dict[str, str] = {}
            # Also seed from locally cached residents so email resolution still works offline
            for r in self.offline_store.list_cached("residents"):
                rem = str(r.get("email") or "").strip()
                if rem:
                    if r.get("id") is not None:
                        id_map[str(r["id"])] = rem
                    rname = str(r.get("full_name") or r.get("name") or "").strip().lower()
                    if rname:
                        name_map[rname] = rem
            if self.client:
                # 1. Check residents table (populated by mobile app / barangay registry)
                try:
                    rr = self.client.table("residents").select("id,full_name,name,email").execute()
                    for r in (rr.data or []):
                        rem = str(r.get("email") or "").strip()
                        if rem:
                            rid = str(r.get("id") or "")
                            rname = str(r.get("full_name") or r.get("name") or "").strip().lower()
                            if rid:
                                id_map[rid] = rem
                            if rname:
                                name_map[rname] = rem
                except Exception:
                    try:
                        rr = self.client.table("residents").select("id,full_name,email").execute()
                        for r in (rr.data or []):
                            rem = str(r.get("email") or "").strip()
                            if rem:
                                rid = str(r.get("id") or "")
                                rname = str(r.get("full_name") or "").strip().lower()
                                if rid:
                                    id_map[rid] = rem
                                if rname:
                                    name_map[rname] = rem
                    except Exception:
                        pass
                # 2. Check profiles + auth.users (for mobile app resident accounts)
                prof_names: dict[str, str] = {}
                try:
                    pr = self.client.table("profiles").select("id,full_name,email").execute()
                    for p in (pr.data or []):
                        pid = str(p.get("id") or "")
                        pname = str(p.get("full_name") or "").strip().lower()
                        pem = str(p.get("email") or "").strip()
                        if pid and pname:
                            prof_names[pid] = pname
                        if pem:
                            if pid:
                                id_map[pid] = pem
                            if pname:
                                name_map[pname] = pem
                except Exception:
                    try:
                        pr = self.client.table("profiles").select("id,full_name").execute()
                        for p in (pr.data or []):
                            pid = str(p.get("id") or "")
                            if pid and p.get("full_name"):
                                prof_names[pid] = str(p["full_name"]).strip().lower()
                    except Exception:
                        pass
                try:
                    if self.using_service_role:
                        users_resp = self.client.auth.admin.list_users()
                        user_list = getattr(users_resp, "users", None) or (users_resp if isinstance(users_resp, list) else [])
                        for au in user_list:
                            uid = str(getattr(au, "id", "") or "")
                            uem = str(getattr(au, "email", "") or "").strip()
                            if uid and uem:
                                id_map[uid] = uem
                                if uid in prof_names:
                                    name_map[prof_names[uid]] = uem
                except Exception:
                    pass
            cache = {
                "ts": now,
                "id_map": id_map,
                "name_map": name_map,
            }
            self._account_email_cache = cache

        if requester_id and str(requester_id) in cache["id_map"]:
            return cache["id_map"][str(requester_id)]
        clean_name = str(resident_name or "").strip().lower()
        if clean_name and clean_name in cache["name_map"]:
            return cache["name_map"][clean_name]
        return ""

    def _apply_doc_meta(self, row: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(row)
        key = str(enriched.get("id", ""))
        meta = self._doc_meta.get(key, {})
        for k, v in meta.items():
            enriched[k] = v
        if enriched.get("verification_status") == "Rejected" and enriched.get("status") not in ("Approved", "Resolved"):
            enriched["status"] = "Rejected"

        # Enforce fixed document fee and automatic expiration policy
        policy = get_document_policy(enriched.get("document_type"))
        fixed_fee = float(policy["fee"])
        validity_months = int(policy["validity_months"])
        validity_label = str(policy["validity_label"])
        enriched["fixed_fee"] = fixed_fee
        enriched["amount_due"] = fixed_fee
        enriched["validity_months"] = validity_months
        enriched["validity_label"] = validity_label

        is_approved = str(enriched.get("status") or "").strip() in ("Approved", "Resolved")
        if is_approved:
            if enriched.get("amount_paid") in (None, ""):
                enriched["amount_paid"] = fixed_fee
            issued_date: date | None = None
            for date_key in ("issued_at", "verified_at", "date_requested", "created_at"):
                raw_dt = str(enriched.get(date_key) or "").strip()
                if len(raw_dt) >= 10:
                    try:
                        issued_date = date.fromisoformat(raw_dt[:10])
                        break
                    except ValueError:
                        continue
            if issued_date is None:
                issued_date = manila_today()

            expires_date: date | None = None
            raw_exp = str(enriched.get("expires_at") or "").strip()
            if len(raw_exp) >= 10:
                try:
                    expires_date = date.fromisoformat(raw_exp[:10])
                except ValueError:
                    expires_date = None
            if expires_date is None:
                expires_date = add_calendar_months(issued_date, validity_months)

            today = manila_today()
            is_expired = today > expires_date
            enriched["issued_at"] = issued_date.isoformat()
            enriched["expires_at"] = expires_date.isoformat()
            enriched["is_expired"] = is_expired
            enriched["days_remaining"] = (expires_date - today).days
        else:
            enriched["is_expired"] = False
            enriched["expires_at"] = None
            enriched["days_remaining"] = None

        # Prioritize any email column sent by the Resident Mobile App on the document request row itself
        row_email = ""
        for email_key in ("resident_email", "email", "applicant_email", "requester_email", "contact_email"):
            candidate = str(enriched.get(email_key) or "").strip()
            if candidate:
                row_email = candidate
                break
        if row_email:
            enriched["resident_email"] = row_email
        else:
            enriched["resident_email"] = self._resolve_account_email(
                enriched.get("requester_id") or enriched.get("resident_id"),
                enriched.get("resident_name"),
            )
        return enriched

    _RESIDENT_META_COLS = (
        "is_head",
        "household_role",
        "family_no",
        "date_of_birth",
        "marital_status",
        "years_living_in",
        "family_planning",
        "educational_attainment",
        "residence_type",
        "is_pwd",
        "is_senior",
        "is_lgbt",
        "is_solo_parent",
        "is_pregnant",
        "is_young_parent",
        "is_4ps",
        "sector",
    )

    def _load_resident_meta(self) -> dict[str, dict[str, Any]]:
        meta: dict[str, dict[str, Any]] = {}
        if os.path.exists(self._meta_path):
            try:
                with open(self._meta_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    for k, v in saved.items():
                        if isinstance(v, dict):
                            meta[k] = dict(v)
            except Exception:
                pass
        return meta

    def _save_resident_meta(self) -> None:
        try:
            with open(self._meta_path, "w", encoding="utf-8") as f:
                json.dump(self._resident_meta, f, indent=2)
        except Exception:
            pass

    def _update_resident_meta(self, row_id: Any, data: dict[str, Any]) -> None:
        if row_id is None:
            return
        self._resident_meta = self._load_resident_meta()
        updates = {k: data[k] for k in self._RESIDENT_META_COLS if k in data}
        if not updates:
            return
        key = str(row_id)
        current = dict(self._resident_meta.get(key, {}))
        current.update(updates)
        self._resident_meta[key] = current
        self._save_resident_meta()

    def _apply_resident_meta(self, row: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(row)
        key = str(enriched.get("id", ""))
        meta = self._resident_meta.get(key, {})
        for col in self._RESIDENT_META_COLS:
            if col not in enriched or enriched[col] is None:
                if col in meta:
                    enriched[col] = meta[col]
            elif col in meta:
                enriched[col] = meta[col]
        return enriched

    @staticmethod
    def _is_placeholder(value: str) -> bool:
        return not value or value.lower().startswith(
            ("your-", "replace-", "change-")
        )

    @staticmethod
    def _is_server_key(value: str) -> bool:
        if not value or SupabaseRepository._is_placeholder(value):
            return False

        return not value.startswith(
            ("sb_publishable_", "sb_anon_")
        )

    @property
    def configured(self) -> bool:
        return self.client is not None

    @property
    def server_configured(self) -> bool:
        return self.client is not None and self.using_service_role

    @property
    def configuration(self) -> dict[str, bool]:
        return {
            "url_configured": bool(self.url),
            "key_configured": bool(self.key)
            and not self._is_placeholder(self.key),
            "server_configured": self.server_configured,
        }

    def _require_client(self) -> Client:
        if not self.client:
            raise RepositoryConfigurationError(
                "Supabase is not configured"
            )

        return self.client

    def _require_server(self) -> Client:
        if not self.server_configured:
            raise RepositoryConfigurationError(
                "Server-side Supabase access is not configured. "
                "Set SUPABASE_SERVICE_ROLE_KEY."
            )
        try:
            self.client.postgrest.auth(self.key)  # type: ignore[union-attr]
        except Exception:
            pass
        return self.client  # type: ignore[return-value]

    def list_residents(self) -> list[dict[str, Any]]:
        self._resident_meta = self._load_resident_meta()
        try:
            client = self._require_client()
            response = (
                client.table("residents")
                .select("*")
                .order("resident_id")
                .execute()
            )
            self._mark_online()
            rows = response.data or []
            enriched = [self._apply_resident_meta(r) for r in rows]
            seen: dict[tuple[str, str, str], dict[str, Any]] = {}
            duplicates_to_delete: list[int] = []
            for r in enriched:
                name_key = str(r.get("full_name") or "").strip().lower()
                dob_key = str(r.get("date_of_birth") or r.get("age") or "").strip()
                sex_key = str(r.get("gender") or "").strip().lower()
                if not name_key:
                    continue
                sig = (name_key, dob_key, sex_key)
                if sig in seen:
                    prev = seen[sig]
                    if bool(r.get("is_head")) and not bool(prev.get("is_head")):
                        if prev.get("id") is not None:
                            duplicates_to_delete.append(int(prev["id"]))
                        seen[sig] = r
                    else:
                        if r.get("id") is not None:
                            duplicates_to_delete.append(int(r["id"]))
                else:
                    seen[sig] = r
            if duplicates_to_delete:
                dup_set = set(duplicates_to_delete)
                for dup_id in dup_set:
                    try:
                        self.delete_row("residents", dup_id)
                    except Exception:
                        pass
                enriched = [r for r in enriched if r.get("id") not in dup_set]
            # Snapshot real synchronized records into SQLite (protecting pending local edits)
            self.offline_store.save_table_snapshot("residents", enriched)
            # Return merged view from SQLite so any still-pending offline edits/inserts stay visible
            return [self._apply_resident_meta(r) for r in self.offline_store.list_cached("residents")]
        except Exception as err:
            self._mark_offline(str(err))
            return [self._apply_resident_meta(r) for r in self.offline_store.list_cached("residents")]

    def list_officials(self) -> list[dict[str, Any]]:
        fields = "id,email,full_name,department,position,phone,role,created_at"
        meta = self._load_official_meta()

        def _attach_meta(rows_in: list[dict[str, Any]]) -> list[dict[str, Any]]:
            enriched = []
            seen_emails: set[str] = set()
            # Prefer real UUID rows over stale offline temp IDs if both share the same email
            ordered = sorted(
                rows_in,
                key=lambda r: 1 if self.offline_store.is_local_temp_id(r.get("id")) else 0,
            )
            for row in ordered:
                item = dict(row)
                em = str(item.get("email") or "").strip().lower()
                if em and em in seen_emails and self.offline_store.is_local_temp_id(item.get("id")):
                    continue
                if em:
                    seen_emails.add(em)
                m = meta.get(str(item.get("id") or ""), {}) or (meta.get(f"email:{em}", {}) if em else {})
                item["status"] = m.get("status") or item.get("status") or "Active"
                if not item.get("phone") and m.get("phone"):
                    item["phone"] = m["phone"]
                enriched.append(item)
            return enriched

        try:
            client = self._require_client()
            try:
                response = (
                    client.table("profiles")
                    .select(fields)
                    .in_("role", ["barangay_official", "admin"])
                    .order("created_at")
                    .execute()
                )
            except Exception as col_err:
                if self._is_network_error(col_err):
                    raise
                response = (
                    client.table("profiles")
                    .select("id,email,full_name,department,position,role,created_at")
                    .in_("role", ["barangay_official", "admin"])
                    .order("created_at")
                    .execute()
                )
            self._mark_online()
            rows = response.data or []
            # Show all registered officials & Barangay Secretary (exclude the Super Admin / Barangay Captain account itself)
            result = []
            for row in rows:
                r = str(row.get("role") or "").strip().lower()
                pos = str(row.get("position") or "").strip().lower()
                if r == "barangay_official" or (r == "admin" and "secretary" in pos):
                    result.append(row)
            enriched = _attach_meta(result)
            self.offline_store.save_table_snapshot("officials", enriched)
            return _attach_meta(self.offline_store.list_cached("officials"))
        except Exception as err:
            if not self._is_network_error(err):
                try:
                    client = self._require_client()
                    response = (
                        client.table("profiles")
                        .select("id,full_name,department,position,role,created_at")
                        .eq("role", "barangay_official")
                        .order("created_at")
                        .execute()
                    )
                    self._mark_online()
                    rows = _attach_meta(response.data or [])
                    self.offline_store.save_table_snapshot("officials", rows)
                    return _attach_meta(self.offline_store.list_cached("officials"))
                except Exception as inner_err:
                    self._mark_offline(str(inner_err))
            else:
                self._mark_offline(str(err))
            return _attach_meta(self.offline_store.list_cached("officials"))

    def update_official(self, official_id: str, data: dict[str, Any]) -> dict[str, Any]:
        oid = str(official_id).strip()
        if not oid:
            raise ValueError("Official ID is required")
        meta_patch: dict[str, Any] = {}
        if "status" in data:
            status_val = "Archived" if str(data["status"]).strip().lower() == "archived" else "Active"
            meta_patch["status"] = status_val
        if "phone" in data and data["phone"] is not None:
            meta_patch["phone"] = str(data["phone"]).strip()

        db_patch = {
            k: v
            for k, v in data.items()
            if k in ("full_name", "position", "department", "role", "phone") and v is not None
        }

        cached_row = self.offline_store.get_cached_row("officials", oid) or {}
        cached_email = str(cached_row.get("email") or data.get("email") or "").strip().lower()
        is_temp_id = self.offline_store.is_local_temp_id(oid) or not re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            oid,
        )

        target_uuid: str | None = None if is_temp_id else oid
        if is_temp_id and cached_email:
            try:
                client = self._require_server()
                lookup = client.table("profiles").select("id,email").eq("email", cached_email).limit(1).execute()
                if lookup.data:
                    target_uuid = str(lookup.data[0]["id"])
            except Exception:
                target_uuid = None

        updated_row: dict[str, Any] = {**cached_row, "id": target_uuid or oid, **db_patch, **meta_patch}

        if db_patch and target_uuid:
            try:
                client = self._require_server()
                try:
                    resp = client.table("profiles").update(db_patch).eq("id", target_uuid).execute()
                except Exception as col_err:
                    if self._is_network_error(col_err):
                        raise
                    fallback_patch = {k: v for k, v in db_patch.items() if k != "phone"}
                    resp = client.table("profiles").update(fallback_patch).eq("id", target_uuid).execute() if fallback_patch else None
                self._mark_online()
                if resp and resp.data:
                    updated_row = {**resp.data[0], **meta_patch}
            except Exception as err:
                if not self._is_network_error(err):
                    raise
                self._mark_offline(str(err))
                self.offline_store.enqueue_update("profiles", target_uuid, db_patch, base_snapshot=updated_row)
        elif db_patch and is_temp_id:
            # Update local-only cached official cleanly without sending temp ID to Postgres UUID column
            merged_local = {**cached_row, **db_patch, **meta_patch}
            self.offline_store.upsert_cached_row("officials", merged_local, is_local_only=True)
            updated_row = merged_local

        if meta_patch:
            self._update_official_meta(oid, meta_patch)
            if target_uuid and target_uuid != oid:
                self._update_official_meta(target_uuid, meta_patch)
            if cached_email:
                self._update_official_meta(f"email:{cached_email}", meta_patch)

        # Keep cached_records updated immediately so reload reflects changes even offline
        if cached_row or target_uuid:
            self.offline_store.upsert_cached_row(
                "officials",
                {**cached_row, **updated_row, "id": target_uuid or cached_row.get("id") or oid},
                is_local_only=bool(is_temp_id and not target_uuid),
            )

        updated_row["status"] = meta_patch.get("status") or self._load_official_meta().get(target_uuid or oid, {}).get("status", "Active")
        return updated_row

    def sign_in(
        self,
        email: str,
        password: str
    ) -> dict[str, Any]:
        email_clean = email.strip().lower()
        try:
            # Always authenticate using an isolated client so self.client keeps its service_role header
            self._require_client()
            auth_client = create_client(self.url, self.key)
            response = auth_client.auth.sign_in_with_password(
                {
                    "email": email_clean,
                    "password": password
                }
            )
            self._mark_online()
            client = self._require_server() if self.server_configured else self._require_client()
        except Exception as auth_err:
            if self._is_network_error(auth_err):
                self._mark_offline(str(auth_err))
                cached_user = self.offline_store.verify_offline_login(email_clean, password)
                if cached_user:
                    return cached_user
                raise ValueError("Offline login unavailable for this account. Please connect to the internet for first-time sign-in.")
            raise

        if not response.user:
            raise ValueError("Invalid credentials")

        user_id = str(response.user.id)

        fields = (
            "role,full_name,department,"
            "position,phone,avatar_url"
        )

        try:
            profile = (
                client.table("profiles")
                .select(fields)
                .eq("id", user_id)
                .single()
                .execute()
            )
        except Exception as error:
            if "column" not in str(error).lower():
                raise

            profile = (
                client.table("profiles")
                .select(
                    "role,full_name,department"
                )
                .eq("id", user_id)
                .single()
                .execute()
            )

        if not profile.data:
            raise ValueError(
                "User profile is not configured"
            )

        raw_role = str(profile.data.get("role") or "").strip().lower()
        position_val = str(profile.data.get("position") or "").strip()
        if raw_role == "official":
            role = "barangay_official"
        elif raw_role == "super_admin":
            role = "super_admin"
        elif raw_role == "admin":
            # Barangay Secretary is Admin; Barangay Captain (or legacy admin account) is Super Admin
            role = "admin" if "secretary" in position_val.lower() else "super_admin"
        else:
            role = raw_role

        if role not in {
            "super_admin",
            "admin",
            "barangay_official",
            "resident",
        }:
            raise ValueError(
                "User profile has an invalid role"
            )

        user_profile = {
            "id": user_id,
            "email": response.user.email,
            "role": role,
            "full_name": profile.data.get(
                "full_name", ""
            ),
            "department": profile.data.get(
                "department"
            ),
            "position": profile.data.get(
                "position"
            ),
            "phone": profile.data.get(
                "phone"
            ),
            "avatar_url": profile.data.get(
                "avatar_url"
            ),
        }
        if role in {"super_admin", "admin", "barangay_official"}:
            self.offline_store.cache_official_credentials(email_clean, password, user_profile)
        return user_profile

    def register_account(
        self,
        email: str,
        password: str,
        full_name: str,
        role: str,
        department: str | None = None,
        position: str | None = None,
        phone: str | None = None,
        _from_sync: bool = False,
    ) -> dict[str, Any]:
        if len(password) < 8:
            raise ValueError(
                "Password must contain at least 8 characters"
            )
        clean_email = email.strip().lower()
        try:
            client = self._require_server()
            created_new_user = False
            user_id = None
            try:
                created = client.auth.admin.create_user(
                    {
                        "email": clean_email,
                        "password": password,
                        "email_confirm": True,
                    }
                )
                if not created.user:
                    raise RuntimeError("Supabase did not create the account")
                user_id = str(created.user.id)
                created_new_user = True
            except Exception as create_err:
                err_msg = str(create_err).lower()
                if "already been registered" in err_msg or "already exists" in err_msg:
                    # Check if this is an orphaned Auth user with no profiles row
                    existing_auth_user = None
                    try:
                        auth_users = client.auth.admin.list_users()
                        for au in auth_users or []:
                            if str(getattr(au, "email", "") or "").strip().lower() == clean_email:
                                existing_auth_user = au
                                break
                    except Exception:
                        existing_auth_user = None

                    if existing_auth_user:
                        candidate_id = str(existing_auth_user.id)
                        existing_prof = (
                            client.table("profiles")
                            .select("id,role,position,full_name")
                            .eq("id", candidate_id)
                            .execute()
                        )
                        if not existing_prof.data:
                            # Orphaned Auth user (deleted from profiles previously) — reclaim and update password
                            client.auth.admin.update_user_by_id(
                                candidate_id,
                                {"password": password, "email_confirm": True},
                            )
                            user_id = candidate_id
                        else:
                            if self.is_official_archived(candidate_id, clean_email):
                                raise ValueError(
                                    "This official is currently archived. Restore them in Settings → Archives."
                                )
                            raise ValueError(
                                "A user with this email address has already been registered"
                            )
                    else:
                        raise ValueError(
                            "A user with this email address has already been registered"
                        )
                else:
                    raise

            try:
                update_fields = {
                    "email": clean_email,
                    "full_name": full_name,
                    "role": role,
                    "department": department,
                    "position": position,
                    "phone": phone,
                }
                fallback_update = {k: v for k, v in update_fields.items() if k != "phone"}
                profile = None

                # 1. Try UPDATE first (Supabase auth trigger often auto-creates the profiles row on create_user)
                try:
                    profile = (
                        client.table("profiles")
                        .update(update_fields)
                        .eq("id", user_id)
                        .execute()
                    )
                except Exception as upd_err:
                    if self._is_network_error(upd_err):
                        raise
                    profile = (
                        client.table("profiles")
                        .update(fallback_update)
                        .eq("id", user_id)
                        .execute()
                    )

                # 2. If no trigger created the row yet, INSERT it
                if not profile or not profile.data:
                    insert_payload = {"id": user_id, **update_fields}
                    fallback_insert = {"id": user_id, **fallback_update}
                    try:
                        profile = client.table("profiles").insert(insert_payload).execute()
                    except Exception as ins_err:
                        if self._is_network_error(ins_err):
                            raise
                        # If a race/trigger inserted in between, update instead
                        if "23505" in str(ins_err) or "duplicate key" in str(ins_err).lower():
                            profile = (
                                client.table("profiles")
                                .update(fallback_update)
                                .eq("id", user_id)
                                .execute()
                            )
                        else:
                            profile = client.table("profiles").insert(fallback_insert).execute()
            except Exception:
                if created_new_user and user_id:
                    client.auth.admin.delete_user(user_id)
                raise

            if not profile.data:
                if created_new_user and user_id:
                    client.auth.admin.delete_user(user_id)
                raise RuntimeError(
                    "Could not create the account role profile"
                )

            self._mark_online()
            created_profile = profile.data[0]
            self.offline_store.upsert_cached_row("officials", created_profile, is_local_only=False)
            self.offline_store.cache_official_credentials(clean_email, password, {
                **created_profile,
                "email": clean_email,
                "role": role,
            })
            return created_profile
        except Exception as err:
            if _from_sync or not self._is_network_error(err):
                raise
            self._mark_offline(str(err))
            local_id = self.offline_store.next_local_temp_id()
            local_record = {
                "id": local_id,
                "email": clean_email,
                "full_name": full_name.strip(),
                "role": role,
                "department": department,
                "position": position,
                "phone": phone,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            payload = {
                "email": clean_email,
                "password": password,
                "full_name": full_name.strip(),
                "role": role,
                "department": department,
                "position": position,
                "phone": phone,
            }
            stored = self.offline_store.enqueue_insert("officials", local_record, payload)
            self.offline_store.cache_official_credentials(clean_email, password, stored)
            return stored

    def update_profile(
        self,
        user_id: str,
        data: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            client = self._require_server()
            response = (
                client.table("profiles")
                .update(data)
                .eq("id", user_id)
                .execute()
            )
            self._mark_online()
            if not response.data:
                raise RuntimeError("Profile not found")
            return response.data[0]
        except Exception as err:
            if not self._is_network_error(err):
                raise
            self._mark_offline(str(err))
            return self.offline_store.enqueue_update("profiles", user_id, data, base_snapshot={"id": user_id})

    def update_password(
        self,
        user_id: str,
        password: str
    ) -> None:
        if len(password) < 8:
            raise ValueError(
                "Password must contain at least 8 characters"
            )
        client = self._require_server()
        client.auth.admin.update_user_by_id(
            user_id,
            {
                "password": password
            }
        )
        self._mark_online()

    def _apply_table_meta(self, table_name: str, row: dict[str, Any]) -> dict[str, Any]:
        if table_name == "residents":
            return self._apply_resident_meta(row)
        if table_name == "documents":
            return self._apply_doc_meta(row)
        if table_name == "concerns":
            return self._apply_concern_meta(row)
        if table_name == "announcements":
            return self._apply_ann_meta(row)
        return dict(row)

    def _migrate_local_meta_id(self, table_name: str, local_id: Any, remote_id: Any) -> None:
        old_key = str(local_id)
        new_key = str(remote_id)
        if old_key == new_key:
            return
        if table_name == "residents" and old_key in self._resident_meta:
            self._resident_meta[new_key] = {**self._resident_meta.get(new_key, {}), **self._resident_meta.pop(old_key)}
            self._save_resident_meta()
        elif table_name == "documents" and old_key in self._doc_meta:
            self._doc_meta[new_key] = {**self._doc_meta.get(new_key, {}), **self._doc_meta.pop(old_key)}
            self._save_doc_meta()
        elif table_name == "concerns" and old_key in self._concern_meta:
            self._concern_meta[new_key] = {**self._concern_meta.get(new_key, {}), **self._concern_meta.pop(old_key)}
            self._save_concern_meta()
        elif table_name == "announcements" and old_key in self._ann_meta:
            self._ann_meta[new_key] = {**self._ann_meta.get(new_key, {}), **self._ann_meta.pop(old_key)}
            self._save_ann_meta()

    def list_table(
        self,
        table_name: str,
        order_column: str = "created_at",
        own_only: bool = False,
        user_id: str | None = None,
        categories: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        try:
            client = self._require_client()

            query = (
                client.table(table_name)
                .select("*")
            )

            if own_only and user_id:
                owner_column = (
                    "requester_id"
                    if table_name == "documents"
                    else "reporter_id"
                )
                query = query.eq(owner_column, user_id)

            if categories and table_name == "concerns":
                query = query.in_("category", sorted(categories))

            rows = (
                query
                .order(order_column, desc=True)
                .execute()
                .data
                or []
            )
            self._mark_online()
            enriched = [self._apply_table_meta(table_name, r) for r in rows]
            if not own_only and not categories:
                self.offline_store.save_table_snapshot(table_name, enriched)
                cached_merged = self.offline_store.list_cached(table_name)
                return [self._apply_table_meta(table_name, r) for r in cached_merged]
            return enriched
        except Exception as err:
            self._mark_offline(str(err))
            items = self.offline_store.list_cached(table_name)
            if own_only and user_id:
                owner_column = "requester_id" if table_name == "documents" else "reporter_id"
                items = [it for it in items if str(it.get(owner_column) or "") == str(user_id)]
            if categories and table_name == "concerns":
                items = [it for it in items if it.get("category") in categories]
            return [self._apply_table_meta(table_name, r) for r in items]

    def get_row(
        self,
        table_name: str,
        row_id: int
    ) -> dict[str, Any] | None:
        if OfflineSyncStore.is_local_temp_id(row_id):
            cached = self.offline_store.get_cached_row(table_name, row_id)
            return self._apply_table_meta(table_name, cached) if cached else None
        try:
            client = self._require_client()
            response = (
                client.table(table_name)
                .select("*")
                .eq("id", row_id)
                .execute()
            )
            self._mark_online()
            if not response.data:
                return None
            row = self._apply_table_meta(table_name, response.data[0])
            self.offline_store.upsert_cached_row(table_name, row, is_local_only=False)
            return row
        except Exception as err:
            self._mark_offline(str(err))
            cached = self.offline_store.get_cached_row(table_name, row_id)
            return self._apply_table_meta(table_name, cached) if cached else None

    def _supabase_insert_only(self, table_name: str, data: dict[str, Any]) -> dict[str, Any]:
        client = self._require_server()
        payload = {k: v for k, v in data.items() if not str(k).startswith("_")}
        try:
            response = client.table(table_name).insert(payload).execute()
        except Exception as insert_err:
            err_text = str(insert_err)
            optional_cols = set(self._RESIDENT_META_COLS) | {
                "supporting_doc_type",
                "resident_email",
                "is_urgent",
                "is_pinned",
            }
            if any(col in err_text for col in optional_cols):
                clean_data = {k: v for k, v in payload.items() if k not in optional_cols}
                response = client.table(table_name).insert(clean_data).execute()
            else:
                raise insert_err
        if not response.data:
            raise RuntimeError(f"Could not create {table_name} record")
        self._mark_online()
        created_row = response.data[0]
        if table_name == "residents":
            self._update_resident_meta(created_row.get("id"), payload)
        elif table_name == "documents":
            self._update_doc_meta(created_row.get("id"), payload)
        elif table_name == "concerns":
            self._update_concern_meta(created_row.get("id"), payload)
        elif table_name == "announcements":
            self._update_ann_meta(created_row.get("id"), payload)
        enriched = self._apply_table_meta(table_name, created_row)
        self.offline_store.upsert_cached_row(table_name, enriched, is_local_only=False)
        return enriched

    def insert_row(
        self,
        table_name: str,
        data: dict[str, Any],
        _from_sync: bool = False,
    ) -> dict[str, Any]:
        try:
            return self._supabase_insert_only(table_name, data)
        except Exception as err:
            if _from_sync or not self._is_network_error(err):
                raise
            self._mark_offline(str(err))
            local_id = self.offline_store.next_local_temp_id()
            record = {k: v for k, v in data.items() if not str(k).startswith("_")}
            record["id"] = local_id
            if table_name == "documents":
                record.setdefault("request_id", f"DOC-OFF-{local_id % 10000:04d}")
                self._update_doc_meta(local_id, record)
            elif table_name == "concerns":
                record.setdefault("concern_id", f"CON-OFF-{local_id % 1000:03d}")
                self._update_concern_meta(local_id, record)
            elif table_name == "residents":
                record.setdefault("resident_id", f"RES-OFF-{local_id % 1000:03d}")
                self._update_resident_meta(local_id, record)
            elif table_name == "announcements":
                self._update_ann_meta(local_id, record)
            record.setdefault("created_at", datetime.now(timezone.utc).isoformat())
            stored = self.offline_store.enqueue_insert(table_name, record, data)
            return self._apply_table_meta(table_name, stored)

    def _supabase_update_only(self, table_name: str, row_id: int, data: dict[str, Any]) -> dict[str, Any]:
        payload = {k: v for k, v in data.items() if not str(k).startswith("_")}
        if table_name == "residents":
            self._update_resident_meta(row_id, payload)
        elif table_name == "documents":
            self._update_doc_meta(row_id, payload)
        elif table_name == "concerns":
            self._update_concern_meta(row_id, payload)
        elif table_name == "announcements":
            self._update_ann_meta(row_id, payload)
        optional_cols = set(self._RESIDENT_META_COLS) | {
            "is_urgent",
            "is_pinned",
            "tx_ref",
            "amount_due",
            "amount_paid",
            "resident_email",
            "payment_status",
            "payment_method",
            "receipt_ref_no",
            "receipt_amount",
            "receipt_date",
            "receipt_filename",
            "receipt_file_url",
            "receipt_submitted_at",
            "receipt_auto_checks",
            "email_logs",
            "transaction_history",
            "supporting_doc_type",
            "verification_status",
            "checklist_summary",
            "verified_by",
            "verified_at",
            "issued_at",
            "expires_at",
            "validity_months",
            "validity_label",
            "action_performed",
            "verification_remarks",
            "manual_checks",
            "assigned_to",
            "official_feedback",
            "feedback_by",
            "feedback_at",
            "appointment_date",
            "appointment_time",
            "appointment_notes",
            "appointment_status",
            "reschedule_note",
            "resolution_remarks",
            "updated_at",
        }
        client = self._require_server()
        try:
            response = (
                client.table(table_name)
                .update(payload)
                .eq("id", row_id)
                .execute()
            )
        except Exception as update_err:
            err_text = str(update_err)
            if table_name == "announcements" and payload.get("status") == "Archived" and ("check" in err_text.lower() or "constraint" in err_text.lower() or any(col in err_text for col in optional_cols)):
                clean_data = {k: v for k, v in payload.items() if k not in optional_cols}
                if clean_data.get("status") == "Archived":
                    clean_data["status"] = "Expired"
                response = (
                    client.table(table_name)
                    .update(clean_data)
                    .eq("id", row_id)
                    .execute()
                ) if clean_data else client.table(table_name).select("*").eq("id", row_id).execute()
            elif any(col in err_text for col in optional_cols):
                clean_data = {k: v for k, v in payload.items() if k not in optional_cols}
                if clean_data:
                    response = (
                        client.table(table_name)
                        .update(clean_data)
                        .eq("id", row_id)
                        .execute()
                    )
                else:
                    response = (
                        client.table(table_name)
                        .select("*")
                        .eq("id", row_id)
                        .execute()
                    )
            else:
                raise update_err
        if not response.data:
            # Verify whether the row actually exists (in case PostgREST returned minimal representation)
            check_resp = client.table(table_name).select("*").eq("id", row_id).execute()
            if check_resp.data:
                response = check_resp
            else:
                raise RuntimeError("Record not found")
        self._mark_online()
        enriched = self._apply_table_meta(table_name, response.data[0])
        self.offline_store.upsert_cached_row(table_name, enriched, is_local_only=False)
        return enriched

    def update_row(
        self,
        table_name: str,
        row_id: int,
        data: dict[str, Any],
        _from_sync: bool = False,
    ) -> dict[str, Any]:
        if table_name == "residents":
            self._update_resident_meta(row_id, data)
        elif table_name == "documents":
            self._update_doc_meta(row_id, data)
        elif table_name == "concerns":
            self._update_concern_meta(row_id, data)
        elif table_name == "announcements":
            self._update_ann_meta(row_id, data)

        # Offline-created record not yet synced to Supabase: update locally and coalesce into its queued INSERT
        if OfflineSyncStore.is_local_temp_id(row_id) and not _from_sync:
            merged = self.offline_store.enqueue_update(table_name, row_id, data)
            return self._apply_table_meta(table_name, merged)

        try:
            return self._supabase_update_only(table_name, row_id, data)
        except Exception as err:
            if _from_sync or not self._is_network_error(err):
                raise
            self._mark_offline(str(err))
            existing = self.offline_store.get_cached_row(table_name, row_id)
            merged = self.offline_store.enqueue_update(table_name, row_id, data, base_snapshot=existing)
            return self._apply_table_meta(table_name, merged)

    def delete_row(
        self,
        table_name: str,
        row_id: int,
        _from_sync: bool = False,
    ) -> None:
        if table_name == "residents":
            key = str(row_id)
            if key in self._resident_meta:
                del self._resident_meta[key]
                self._save_resident_meta()
        if OfflineSyncStore.is_local_temp_id(row_id) and not _from_sync:
            self.offline_store.enqueue_delete(table_name, row_id)
            return
        try:
            client = self._require_server()
            client.table(table_name).delete().eq("id", row_id).execute()
            self._mark_online()
            self.offline_store.delete_cached_row(table_name, row_id)
        except Exception as err:
            if _from_sync or not self._is_network_error(err):
                raise
            self._mark_offline(str(err))
            self.offline_store.enqueue_delete(table_name, row_id)

    def create_concern(
        self,
        data: dict[str, Any],
        enforce_limits: bool = True
    ) -> dict[str, Any]:
        today = manila_today().isoformat()
        if enforce_limits and data.get("reporter_id"):
            try:
                client = self._require_server()
                existing = (
                    client.table("concerns")
                    .select("id,title")
                    .eq("reporter_id", data["reporter_id"])
                    .eq("submitted_at", today)
                    .execute()
                    .data
                    or []
                )
                self._mark_online()
                if len(existing) >= 3:
                    raise RepositoryRateLimitError("Daily concern limit reached (3 reports)")
                if any(
                    str(row.get("title", "")).strip().casefold() == str(data["title"]).strip().casefold()
                    for row in existing
                ):
                    raise RepositoryConflictError("A similar concern was already reported today")
            except (RepositoryRateLimitError, RepositoryConflictError):
                raise
            except Exception as err:
                if not self._is_network_error(err):
                    raise
                self._mark_offline(str(err))

        return self.insert_row("concerns", data)

    # ------------------------------------------------------------------
    # Connectivity & Automatic Synchronization Engine
    # ------------------------------------------------------------------
    def check_connectivity(self) -> bool:
        if not self.client:
            self._mark_offline("Supabase client not configured")
            return False
        try:
            self.client.table("residents").select("id").limit(1).execute()
            self._mark_online()
            return True
        except Exception as err:
            if self._is_network_error(err):
                self._mark_offline(str(err))
                return False
            # Non-network error (e.g., empty table or schema warning) still means central server is reachable
            self._mark_online()
            return True

    def get_sync_status(self, probe: bool = False) -> dict[str, Any]:
        if probe and not self._sync_in_progress:
            self.check_connectivity()
        summary = self.offline_store.get_queue_summary()
        if self._sync_in_progress:
            state = "syncing"
        elif not self._is_online:
            state = "offline"
        elif summary["failed_count"] > 0:
            state = "sync_failed"
        else:
            state = "online"
        return {
            "status": state,
            "online": self._is_online,
            "syncing": self._sync_in_progress,
            **summary,
        }

    def _find_remote_duplicate(self, table_name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Prevent creating duplicate records when replaying queued offline inserts."""
        try:
            client = self._require_server()
            if table_name == "residents":
                name = str(payload.get("full_name") or "").strip().lower()
                if not name:
                    return None
                dob = str(payload.get("date_of_birth") or "").strip()
                hh = str(payload.get("household") or "").strip().lower()
                rows = client.table("residents").select("*").execute().data or []
                for r in rows:
                    r_name = str(r.get("full_name") or "").strip().lower()
                    if r_name != name:
                        continue
                    r_dob = str(r.get("date_of_birth") or "").strip()
                    r_hh = str(r.get("household") or "").strip().lower()
                    if (dob and r_dob == dob) or (hh and r_hh == hh) or (not dob and not hh):
                        return r
            elif table_name == "documents":
                rname = str(payload.get("resident_name") or "").strip().lower()
                dtype = str(payload.get("document_type") or "").strip().lower()
                dreq = str(payload.get("date_requested") or "").strip()[:10]
                purpose = str(payload.get("purpose") or "").strip().lower()
                rows = client.table("documents").select("*").eq("document_type", payload.get("document_type")).execute().data or []
                for r in rows:
                    if (
                        str(r.get("resident_name") or "").strip().lower() == rname
                        and str(r.get("document_type") or "").strip().lower() == dtype
                        and str(r.get("date_requested") or "").strip()[:10] == dreq
                        and str(r.get("purpose") or "").strip().lower() == purpose
                    ):
                        return r
            elif table_name == "concerns":
                title = str(payload.get("title") or "").strip().lower()
                sub_by = str(payload.get("submitted_by") or "").strip().lower()
                sub_at = str(payload.get("submitted_at") or "").strip()[:10]
                rows = client.table("concerns").select("*").execute().data or []
                for r in rows:
                    if (
                        str(r.get("title") or "").strip().lower() == title
                        and str(r.get("submitted_by") or "").strip().lower() == sub_by
                        and str(r.get("submitted_at") or "").strip()[:10] == sub_at
                    ):
                        return r
            elif table_name == "announcements":
                title = str(payload.get("title") or "").strip().lower()
                pub_at = str(payload.get("published_at") or "").strip()[:10]
                rows = client.table("announcements").select("*").execute().data or []
                for r in rows:
                    if (
                        str(r.get("title") or "").strip().lower() == title
                        and str(r.get("published_at") or "").strip()[:10] == pub_at
                    ):
                        return r
            elif table_name == "weather_alerts":
                title = str(payload.get("title") or "").strip().lower()
                issued = str(payload.get("issued_at") or "").strip()[:16]
                rows = client.table("weather_alerts").select("*").execute().data or []
                for r in rows:
                    if (
                        str(r.get("title") or "").strip().lower() == title
                        and str(r.get("issued_at") or "").strip()[:16] == issued
                    ):
                        return r
            elif table_name == "officials":
                em = str(payload.get("email") or "").strip().lower()
                if not em:
                    return None
                rows = client.table("profiles").select("*").eq("email", em).execute().data or []
                if rows:
                    return rows[0]
        except Exception:
            return None
        return None

    @staticmethod
    def _merge_non_conflicting_update(
        local_updates: dict[str, Any],
        base_snapshot: dict[str, Any] | None,
        remote_row: dict[str, Any] | None,
        op_updated_at: str | None,
    ) -> tuple[dict[str, Any], int]:
        """Field-level merge that prevents overwriting newer central edits on conflicting fields."""
        if not remote_row or not base_snapshot:
            return dict(local_updates), 0
        remote_ts = str(remote_row.get("updated_at") or "")
        op_ts = str(op_updated_at or "")
        remote_is_newer = bool(remote_ts and op_ts and remote_ts > op_ts)
        safe_updates: dict[str, Any] = {}
        conflicts_resolved = 0
        for field, local_val in local_updates.items():
            if str(field).startswith("_"):
                continue
            base_val = base_snapshot.get(field)
            remote_val = remote_row.get(field)
            if remote_val == local_val:
                continue
            # If central DB changed this exact field independently AND is newer, preserve central value
            if remote_val != base_val and remote_is_newer:
                conflicts_resolved += 1
                continue
            safe_updates[field] = local_val
        return safe_updates, conflicts_resolved

    def sync_now(self) -> dict[str, Any]:
        """Push pending offline changes (deduplicated & conflict-safe) and pull latest central records."""
        if not self._sync_lock.acquire(blocking=False):
            return self.get_sync_status(probe=False)
        self._sync_in_progress = True
        self.offline_store.set_meta("last_sync_status", "syncing")
        pushed = 0
        skipped_duplicates = 0
        conflicts_resolved = 0
        failed = 0
        pulled_counts: dict[str, int] = {}
        try:
            if not self.check_connectivity():
                return {
                    **self.get_sync_status(probe=False),
                    "pushed": 0,
                    "skipped_duplicates": 0,
                    "conflicts_resolved": 0,
                    "failed": 0,
                    "pulled_counts": {},
                }

            pending_ops = self.offline_store.list_pending_ops(include_failed=True)
            for op in pending_ops:
                op_id = op["op_id"]
                table = op["table_name"]
                action = op["action"]
                row_id = op.get("row_id")
                payload = op.get("payload") or {}
                client_uid = op.get("client_uid")

                try:
                    if action == "insert":
                        already_remote_id = self.offline_store.lookup_synced_remote_id(client_uid)
                        if already_remote_id:
                            self.offline_store.mark_op_synced(op_id)
                            skipped_duplicates += 1
                            continue
                        existing_dup = self._find_remote_duplicate(table, payload)
                        if existing_dup and existing_dup.get("id") is not None:
                            remote_id = existing_dup["id"]
                            self._migrate_local_meta_id(table, row_id, remote_id)
                            self.offline_store.record_synced_id(client_uid, table, row_id, remote_id)
                            self.offline_store.upsert_cached_row(
                                table, self._apply_table_meta(table, existing_dup), is_local_only=False
                            )
                            self.offline_store.mark_op_synced(op_id)
                            skipped_duplicates += 1
                            continue

                        if table == "officials":
                            created = self.register_account(
                                email=str(payload["email"]),
                                password=str(payload["password"]),
                                full_name=str(payload["full_name"]),
                                role=str(payload.get("role") or "barangay_official"),
                                department=payload.get("department"),
                                position=payload.get("position"),
                                phone=payload.get("phone"),
                                _from_sync=True,
                            )
                        else:
                            created = self._supabase_insert_only(table, payload)

                        remote_id = created.get("id")
                        if remote_id is not None:
                            self._migrate_local_meta_id(table, row_id, remote_id)
                            self.offline_store.record_synced_id(client_uid, table, row_id, remote_id)
                        self.offline_store.mark_op_synced(op_id)
                        pushed += 1

                    elif action == "update":
                        if table == "profiles":
                            self.update_profile(str(row_id), payload)
                            self.offline_store.mark_op_synced(op_id)
                            pushed += 1
                            continue
                        target_id = int(row_id)
                        remote_row = None
                        try:
                            client = self._require_server()
                            resp = client.table(table).select("*").eq("id", target_id).execute()
                            remote_row = resp.data[0] if resp.data else None
                        except Exception:
                            remote_row = None

                        merged_payload, c_count = self._merge_non_conflicting_update(
                            payload,
                            op.get("base_snapshot"),
                            remote_row,
                            op.get("updated_at"),
                        )
                        conflicts_resolved += c_count
                        if merged_payload:
                            self._supabase_update_only(table, target_id, merged_payload)
                        self.offline_store.mark_op_synced(op_id)
                        pushed += 1

                    elif action == "delete":
                        target_id = int(row_id)
                        self.delete_row(table, target_id, _from_sync=True)
                        self.offline_store.mark_op_synced(op_id)
                        pushed += 1

                except Exception as op_err:
                    if self._is_network_error(op_err):
                        self._mark_offline(str(op_err))
                        # Abort cleanly without marking ops as permanently failed; they stay pending
                        return {
                            **self.get_sync_status(probe=False),
                            "pushed": pushed,
                            "skipped_duplicates": skipped_duplicates,
                            "conflicts_resolved": conflicts_resolved,
                            "failed": failed,
                            "pulled_counts": pulled_counts,
                        }
                    self.offline_store.mark_op_failed(op_id, str(op_err))
                    failed += 1

            # Phase 2: Pull latest snapshots from Supabase into SQLite cache (protecting any failed/pending rows)
            try:
                pulled_counts["residents"] = len(self.list_residents())
                pulled_counts["documents"] = len(self.list_table("documents", "date_requested"))
                pulled_counts["concerns"] = len(self.list_table("concerns", "submitted_at"))
                pulled_counts["announcements"] = len(self.list_table("announcements", "published_at"))
                pulled_counts["weather_alerts"] = len(self.list_table("weather_alerts", "issued_at"))
                pulled_counts["officials"] = len(self.list_officials())
            except Exception as pull_err:
                if self._is_network_error(pull_err):
                    self._mark_offline(str(pull_err))

            now_iso = datetime.now(timezone.utc).isoformat()
            if self._is_online:
                self.offline_store.set_meta("last_synced_at", now_iso)
                if failed > 0:
                    self.offline_store.set_meta("last_sync_status", "sync_failed")
                    self.offline_store.set_meta("last_sync_error", f"{failed} change(s) failed to synchronize")
                else:
                    self.offline_store.set_meta("last_sync_status", "online")
                    self.offline_store.set_meta("last_sync_error", "")

            return {
                **self.get_sync_status(probe=False),
                "pushed": pushed,
                "skipped_duplicates": skipped_duplicates,
                "conflicts_resolved": conflicts_resolved,
                "failed": failed,
                "pulled_counts": pulled_counts,
            }
        finally:
            self._sync_in_progress = False
            self._sync_lock.release()

    def can_create_document(
        self,
        requester_id: str,
        document_type: str
    ) -> bool:
        try:
            client = self._require_server()

            cutoff = (
                manila_today()
                - timedelta(days=30)
            )

            rows = (
                client.table("documents")
                .select(
                    "id,status,document_type,date_requested,created_at"
                )
                .eq(
                    "requester_id",
                    requester_id
                )
                .eq(
                    "document_type",
                    document_type
                )
                .execute()
                .data
                or []
            )

            active_statuses = {
                "Pending",
                "In Progress",
                "Processing",
                "Approved",
                "Resolved",
            }

            for row in rows:
                enriched = self._apply_doc_meta(row)
                # Expired or Rejected documents allow the resident to submit a new request
                if enriched.get("is_expired"):
                    continue
                if str(enriched.get("status") or "").strip() == "Rejected":
                    continue
                if str(enriched.get("status") or "").strip() in active_statuses:
                    return False

            return True
        except Exception:
            return True

    def add_resident(
        self,
        resident: dict[str, Any]
    ) -> dict[str, Any]:

        return self.insert_row(
            "residents",
            resident
        )

    def dashboard_summary(
        self,
        categories: set[str] | None = None
    ) -> dict[str, Any]:

        all_residents = self.list_residents()
        residents = [
            r for r in all_residents
            if str(r.get("status") or "").strip().lower() != "inactive"
        ]

        documents = self.list_table(
            "documents"
        )

        concerns = self.list_table(
            "concerns",
            categories=categories
        )

        today = manila_today()

        month_keys = []

        for offset in range(6, -1, -1):
            month = today.month - offset

            year = (
                today.year
                + (month - 1) // 12
            )

            month = (
                (month - 1) % 12
                + 1
            )

            month_keys.append(
                (year, month)
            )

        registrations = Counter()
        verified_registrations = Counter()

        age_groups = Counter(
            {
                "0-12": 0,
                "13-17": 0,
                "18-35": 0,
                "36-59": 0,
                "60+": 0,
            }
        )

        document_types = Counter()
        document_statuses = Counter()
        concern_statuses = Counter()
        concern_trend = Counter()

        households = {
            resident.get("household")
            for resident in residents
            if resident.get("household")
            not in (
                None,
                "",
                "UNASSIGNED"
            )
        }

        resident_statuses = Counter(
            str(
                resident.get(
                    "status",
                    "Pending"
                )
            )
            for resident in residents
        )

        for resident in residents:

            registration_month = (
                manila_month_key(
                    resident.get("created_at")
                )
            )

            if not registration_month:
                registration_month = (
                    manila_month_key(
                        resident.get(
                            "resident_since"
                        )
                    )
                )

            if registration_month:
                registrations[
                    registration_month
                ] += 1

                if resident.get(
                    "status"
                ) == "Verified":
                    verified_registrations[
                        registration_month
                    ] += 1

            try:
                age = int(
                    resident.get(
                        "age",
                        0
                    )
                )
            except (
                TypeError,
                ValueError
            ):
                age = 0

            group = (
                "60+"
                if age >= 60
                else "36-59"
                if age >= 36
                else "18-35"
                if age >= 18
                else "13-17"
                if age >= 13
                else "0-12"
            )

            age_groups[group] += 1

        for document in documents:
            document_types[
                str(
                    document.get(
                        "document_type",
                        "Unknown"
                    )
                )
            ] += 1

            document_statuses[
                str(
                    document.get(
                        "status",
                        "Pending"
                    )
                )
            ] += 1

        for concern in concerns:
            concern_statuses[
                str(
                    concern.get(
                        "status",
                        "Pending"
                    )
                )
            ] += 1

            submitted = str(
                concern.get(
                    "submitted_at",
                    ""
                )
            )[:7]

            if submitted:
                concern_trend[
                    submitted
                ] += 1

        return {
            "total_residents": len(
                residents
            ),

            "households": len(
                households
            ),

            "documents_issued": sum(
                1
                for document in documents
                if document.get(
                    "status"
                ) == "Approved"
                and str(
                    document.get(
                        "date_requested",
                        ""
                    )
                )[:7]
                == today.strftime("%Y-%m")
            ),

            "documents_total": len(
                documents
            ),

            "open_concerns": sum(
                1
                for concern in concerns
                if concern.get(
                    "status"
                ) != "Resolved"
            ),

            "verified": resident_statuses.get(
                "Verified",
                0
            ),

            "pending": resident_statuses.get(
                "Pending",
                0
            ),

            "document_statuses": dict(
                document_statuses
            ),

            "concern_statuses": dict(
                concern_statuses
            ),

            "documents_by_type": dict(
                document_types
            ),

            "concerns_trend": [
                concern_trend.get(
                    f"{year:04d}-{month:02d}",
                    0
                )
                for year, month in month_keys
            ],

            "registrations": {
                "labels": [
                    date(
                        year,
                        month,
                        1
                    ).strftime("%b")
                    for year, month
                    in month_keys
                ],

                "registered": [
                    registrations.get(
                        f"{year:04d}-{month:02d}",
                        0
                    )
                    for year, month
                    in month_keys
                ],

                "verified": [
                    verified_registrations.get(
                        f"{year:04d}-{month:02d}",
                        0
                    )
                    for year, month
                    in month_keys
                ],
            },

            "age_groups": dict(
                age_groups
            ),
        }


supabase_repository = SupabaseRepository()
