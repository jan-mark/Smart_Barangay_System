import json
import os
from collections import Counter
from datetime import date, timedelta
from typing import Any

from dotenv import load_dotenv
from supabase import Client, create_client
from time_utils import manila_month_key, manila_today

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
        self._init_demo_data()

    def _init_demo_data(self) -> None:
        self._demo_users: dict[str, Any] = {}
        self._demo_residents = [
            {"id": 1, "resident_id": "RES-001", "full_name": "Maria Santos", "address": "Purok 3, Blk 4 Lot 2", "household": "HH-0042", "family_no": "FAM-001", "household_role": "Head", "is_head": True, "age": 34, "date_of_birth": "1992-05-14", "gender": "Female", "marital_status": "Separated", "family_planning": "Pills", "educational_attainment": "Tertiary", "residence_type": "Concrete", "phone": "09171234567", "is_pwd": False, "is_senior": False, "is_lgbt": False, "is_solo_parent": True, "is_pregnant": False, "is_young_parent": False, "is_4ps": False, "sector": "Leader, Solo Parent, Adult", "status": "Verified", "resident_since": "2018-03-12", "created_at": "2018-03-12T08:00:00Z"},
            {"id": 2, "resident_id": "RES-002", "full_name": "Juan dela Cruz", "address": "Purok 1, Blk 2 Lot 8", "household": "HH-0015", "family_no": "FAM-002", "household_role": "Head", "is_head": True, "age": 52, "date_of_birth": "1974-08-20", "gender": "Male", "marital_status": "Married", "family_planning": "Natural", "educational_attainment": "Secondary", "residence_type": "Semi-Concrete", "phone": "09172345678", "is_pwd": False, "is_senior": False, "is_lgbt": False, "is_solo_parent": False, "is_pregnant": False, "is_young_parent": False, "is_4ps": True, "sector": "Leader, 4Ps, Adult", "status": "Verified", "resident_since": "2010-07-22", "created_at": "2010-07-22T08:00:00Z"},
            {"id": 3, "resident_id": "RES-003", "full_name": "Ana Reyes", "address": "Purok 5, Blk 7 Lot 1", "household": "HH-0078", "family_no": "FAM-003", "household_role": "Head", "is_head": True, "age": 27, "date_of_birth": "1999-11-03", "gender": "Female", "marital_status": "Live-in", "years_living_in": 4, "family_planning": "Implant", "educational_attainment": "Vocational/ALS", "residence_type": "Light Materials", "phone": "09173456789", "is_pwd": True, "is_senior": False, "is_lgbt": False, "is_solo_parent": False, "is_pregnant": False, "is_young_parent": False, "is_4ps": False, "sector": "Leader, PWD, Adult", "status": "Pending", "resident_since": "2024-01-05", "created_at": "2024-01-05T08:00:00Z"},
            {"id": 4, "resident_id": "RES-004", "full_name": "Carlos Mendoza", "address": "Purok 2, Blk 3 Lot 5", "household": "HH-0031", "family_no": "FAM-004", "household_role": "Head", "is_head": True, "age": 45, "date_of_birth": "1981-02-17", "gender": "Male", "marital_status": "Married", "family_planning": "Condom", "educational_attainment": "Tertiary", "residence_type": "Concrete", "phone": "09174567890", "is_pwd": False, "is_senior": False, "is_lgbt": False, "is_solo_parent": False, "is_pregnant": False, "is_young_parent": False, "is_4ps": False, "sector": "Leader, Adult", "status": "Verified", "resident_since": "2015-11-18", "created_at": "2015-11-18T08:00:00Z"},
            {"id": 5, "resident_id": "RES-005", "full_name": "Liza Torres", "address": "Purok 4, Blk 6 Lot 3", "household": "HH-0063", "family_no": "FAM-005", "household_role": "Head", "is_head": True, "age": 65, "date_of_birth": "1961-04-30", "gender": "Female", "marital_status": "Widow/Widower", "family_planning": "None", "educational_attainment": "Elementary", "residence_type": "Semi-Concrete", "phone": "09175678901", "is_pwd": False, "is_senior": True, "is_lgbt": False, "is_solo_parent": False, "is_pregnant": False, "is_young_parent": False, "is_4ps": False, "sector": "Leader, Senior Citizen", "status": "Verified", "resident_since": "2005-04-30", "created_at": "2005-04-30T08:00:00Z"},
            {"id": 6, "resident_id": "RES-006", "full_name": "Pedro Villanueva", "address": "Purok 1, Blk 1 Lot 12", "household": "HH-0008", "family_no": "FAM-006", "household_role": "Head", "is_head": True, "age": 38, "date_of_birth": "1988-09-14", "gender": "Male", "marital_status": "Married", "family_planning": "Natural", "educational_attainment": "Secondary", "residence_type": "Concrete", "phone": "09176789012", "is_pwd": False, "is_senior": False, "is_lgbt": False, "is_solo_parent": False, "is_pregnant": False, "is_young_parent": False, "is_4ps": False, "sector": "Leader, Adult", "status": "Verified", "resident_since": "2019-09-14", "created_at": "2019-09-14T08:00:00Z"},
            {"id": 7, "resident_id": "RES-007", "full_name": "Rosa Bautista", "address": "Purok 6, Blk 9 Lot 4", "household": "HH-0091", "family_no": "FAM-007", "household_role": "Head", "is_head": True, "age": 19, "date_of_birth": "2007-06-01", "gender": "Female", "marital_status": "Live-in", "years_living_in": 2, "family_planning": "Injectable", "educational_attainment": "Senior High", "residence_type": "Light Materials", "phone": "09177890123", "is_pwd": False, "is_senior": False, "is_lgbt": False, "is_solo_parent": True, "is_pregnant": False, "is_young_parent": True, "is_4ps": True, "sector": "Leader, Solo Parent, Young Parent, 4Ps, Adult", "status": "Pending", "resident_since": "2025-06-01", "created_at": "2025-06-01T08:00:00Z"},
            {"id": 8, "resident_id": "RES-008", "full_name": "Joshua Santos", "address": "Purok 3, Blk 4 Lot 2", "household": "HH-0042", "family_no": "FAM-001", "household_role": "Son", "is_head": False, "age": 9, "date_of_birth": "2017-03-10", "gender": "Male", "marital_status": "Single", "family_planning": "None", "educational_attainment": "Elementary", "residence_type": "Concrete", "phone": "", "is_pwd": False, "is_senior": False, "is_lgbt": False, "is_solo_parent": False, "is_pregnant": False, "is_young_parent": False, "is_4ps": False, "sector": "Minor", "status": "Verified", "resident_since": "2018-03-12", "created_at": "2018-03-12T08:00:00Z"},
            {"id": 9, "resident_id": "RES-009", "full_name": "Clara Santos", "address": "Purok 3, Blk 4 Lot 2", "household": "HH-0042", "family_no": "FAM-001", "household_role": "Grandparent", "is_head": False, "age": 68, "date_of_birth": "1958-01-22", "gender": "Female", "marital_status": "Widow/Widower", "family_planning": "None", "educational_attainment": "Elementary", "residence_type": "Concrete", "phone": "", "is_pwd": False, "is_senior": True, "is_lgbt": False, "is_solo_parent": False, "is_pregnant": False, "is_young_parent": False, "is_4ps": False, "sector": "Senior Citizen", "status": "Verified", "resident_since": "2018-03-12", "created_at": "2018-03-12T08:00:00Z"},
            {"id": 10, "resident_id": "RES-010", "full_name": "Elena dela Cruz", "address": "Purok 1, Blk 2 Lot 8", "household": "HH-0015", "family_no": "FAM-002", "household_role": "Wife", "is_head": False, "age": 49, "date_of_birth": "1977-07-12", "gender": "Female", "marital_status": "Married", "family_planning": "BTL", "educational_attainment": "Secondary", "residence_type": "Semi-Concrete", "phone": "09178901234", "is_pwd": True, "is_senior": False, "is_lgbt": False, "is_solo_parent": False, "is_pregnant": False, "is_young_parent": False, "is_4ps": True, "sector": "PWD, 4Ps, Adult", "status": "Verified", "resident_since": "2010-07-22", "created_at": "2010-07-22T08:00:00Z"},
            {"id": 11, "resident_id": "RES-011", "full_name": "Miguel dela Cruz", "address": "Purok 1, Blk 2 Lot 8", "household": "HH-0015", "family_no": "FAM-002", "household_role": "Son", "is_head": False, "age": 14, "date_of_birth": "2012-10-05", "gender": "Male", "marital_status": "Single", "family_planning": "None", "educational_attainment": "Secondary", "residence_type": "Semi-Concrete", "phone": "", "is_pwd": False, "is_senior": False, "is_lgbt": False, "is_solo_parent": False, "is_pregnant": False, "is_young_parent": False, "is_4ps": True, "sector": "4Ps, Minor", "status": "Verified", "resident_since": "2010-07-22", "created_at": "2010-07-22T08:00:00Z"},
            {"id": 12, "resident_id": "RES-012", "full_name": "Baby Bea Bautista", "address": "Purok 6, Blk 9 Lot 4", "household": "HH-0091", "family_no": "FAM-007", "household_role": "Daughter", "is_head": False, "age": 2, "date_of_birth": "2024-04-18", "gender": "Female", "marital_status": "Single", "family_planning": "None", "educational_attainment": "Pre-school", "residence_type": "Light Materials", "phone": "", "is_pwd": False, "is_senior": False, "is_lgbt": False, "is_solo_parent": False, "is_pregnant": False, "is_young_parent": False, "is_4ps": True, "sector": "4Ps, Minor", "status": "Verified", "resident_since": "2025-06-01", "created_at": "2025-06-01T08:00:00Z"},
        ]
        self._demo_officials = [
            {"id": "off-001", "email": "health@brgy.gov.ph", "full_name": "Dr. Maria Santos", "department": "Health & Sanitation", "position": "Health Officer", "phone": "0917-111-2222", "created_at": "2024-01-01T00:00:00Z"},
            {"id": "off-002", "email": "peace@brgy.gov.ph", "full_name": "Officer Ramon Cruz", "department": "Peace & Order", "position": "Chief Tanod", "phone": "0917-222-3333", "created_at": "2024-01-01T00:00:00Z"},
            {"id": "off-003", "email": "infra@brgy.gov.ph", "full_name": "Engr. Roberto Diaz", "department": "Infrastructure", "position": "Public Works Officer", "phone": "0917-333-4444", "created_at": "2024-01-01T00:00:00Z"},
            {"id": "off-004", "email": "social@brgy.gov.ph", "full_name": "Elena Morales", "department": "Social Services", "position": "Welfare Officer", "phone": "0917-444-5555", "created_at": "2024-01-01T00:00:00Z"},
            {"id": "off-005", "email": "drrmc@brgy.gov.ph", "full_name": "Gabriel Navarro", "department": "DRRMC", "position": "Disaster Coordinator", "phone": "0917-555-6666", "created_at": "2024-01-01T00:00:00Z"},
        ]
        self._demo_data = {
            "documents": [
                {"id": 1, "request_id": "DOC-0001", "resident_name": "Maria Santos", "document_type": "Barangay Clearance", "date_requested": "2026-09-04", "status": "Approved", "processor": "Admin Flores", "purpose": "Employment", "applicant_address": "Purok 3, Blk 4 Lot 2", "applicant_contact": "09171234567", "created_at": "2026-09-04T08:00:00Z"},
                {"id": 2, "request_id": "DOC-0002", "resident_name": "Pedro Villanueva", "document_type": "Certificate of Indigency", "date_requested": "2026-09-05", "status": "Processing", "processor": "Admin Flores", "purpose": "Financial Assistance", "applicant_address": "Purok 1, Blk 1 Lot 12", "applicant_contact": "09172345678", "created_at": "2026-09-05T08:00:00Z"},
                {"id": 3, "request_id": "DOC-0003", "resident_name": "Ana Reyes", "document_type": "Certificate of Residency", "date_requested": "2026-09-05", "status": "Pending", "processor": None, "purpose": "Bank Account", "applicant_address": "Purok 5, Blk 7 Lot 1", "applicant_contact": "09173456789", "created_at": "2026-09-05T09:00:00Z"},
                {"id": 4, "request_id": "DOC-0004", "resident_name": "Carlos Mendoza", "document_type": "Business Clearance", "date_requested": "2026-09-06", "status": "Approved", "processor": "Admin Cruz", "purpose": "Sari-sari Store Permit", "applicant_address": "Purok 2, Blk 3 Lot 5", "business_name": "Mendoza Store", "applicant_contact": "09174567890", "created_at": "2026-09-06T08:00:00Z"},
                {"id": 5, "request_id": "DOC-0005", "resident_name": "Liza Torres", "document_type": "Barangay Clearance", "date_requested": "2026-09-07", "status": "Rejected", "processor": "Admin Flores", "purpose": "Travel Requirement", "applicant_address": "Purok 4, Blk 6 Lot 3", "applicant_contact": "09175678901", "created_at": "2026-09-07T08:00:00Z"},
                {"id": 6, "request_id": "DOC-0006", "resident_name": "Juan dela Cruz", "document_type": "Certificate of Indigency", "date_requested": "2026-09-07", "status": "Processing", "processor": "Admin Cruz", "purpose": "Medical Assistance", "applicant_address": "Purok 1, Blk 2 Lot 8", "applicant_contact": "09176789012", "created_at": "2026-09-07T10:00:00Z"},
                {"id": 7, "request_id": "DOC-0007", "resident_name": "Rosa Bautista", "document_type": "Certificate of Residency", "date_requested": "2026-09-08", "status": "Pending", "processor": None, "purpose": "School Enrollment", "applicant_address": "Purok 6, Blk 9 Lot 4", "applicant_contact": "09177890123", "created_at": "2026-09-08T08:00:00Z"}
            ],
            "concerns": [
                {"id": 1, "concern_id": "CON-001", "category": "Infrastructure", "priority": "High", "title": "Pothole on main road near Purok 2", "description": "Large pothole causing traffic slowdown and safety risk.", "submitted_by": "Juan dela Cruz", "submitted_at": "2026-09-01", "status": "Resolved", "created_at": "2026-09-01T08:00:00Z"},
                {"id": 2, "concern_id": "CON-002", "category": "Peace & Order", "priority": "Medium", "title": "Noise disturbance at nighttime", "description": "Loud karaoke past 10 PM along Purok 3.", "submitted_by": "Maria Santos", "submitted_at": "2026-09-02", "status": "Open", "created_at": "2026-09-02T08:00:00Z"},
                {"id": 3, "concern_id": "CON-003", "category": "Health & Sanitation", "priority": "High", "title": "Garbage not collected in Purok 5", "description": "Uncollected trash bags attracting pests.", "submitted_by": "Ana Reyes", "submitted_at": "2026-09-03", "status": "Open", "created_at": "2026-09-03T08:00:00Z"},
                {"id": 4, "concern_id": "CON-004", "category": "Infrastructure", "priority": "Medium", "title": "Street light malfunction at Blk 3", "description": "Flickering street lamp makes corner dark.", "submitted_by": "Carlos Mendoza", "submitted_at": "2026-09-04", "status": "In Progress", "created_at": "2026-09-04T08:00:00Z"},
                {"id": 5, "concern_id": "CON-005", "category": "Social Services", "priority": "Critical", "title": "Request for emergency food assistance", "description": "Senior citizen household needs relief pack.", "submitted_by": "Pedro Villanueva", "submitted_at": "2026-09-05", "status": "Escalated", "created_at": "2026-09-05T08:00:00Z"},
                {"id": 6, "concern_id": "CON-006", "category": "Health & Sanitation", "priority": "High", "title": "Stagnant water breeding mosquitoes", "description": "Clogged canal near Purok 4 school path.", "submitted_by": "Liza Torres", "submitted_at": "2026-09-06", "status": "Open", "created_at": "2026-09-06T08:00:00Z"}
            ],
            "announcements": [
                {"id": 1, "category": "Assembly", "status": "Active", "title": "Barangay General Assembly — September 15, 2026", "description": "All residents are invited to attend the bi-annual assembly at the Barangay Gymnasium.", "author": "Kap. Ramirez", "published_at": "2026-09-08", "audience": "All Residents", "created_at": "2026-09-08T08:00:00Z"},
                {"id": 2, "category": "Health", "status": "Active", "title": "Free Medical & Dental Mission — September 12, 2026", "description": "Free consultations, vitamins, and dental checkups at the Barangay Health Center from 8:00 AM to 3:00 PM.", "author": "Health Officer", "published_at": "2026-09-07", "audience": "All Residents", "created_at": "2026-09-07T08:00:00Z"},
                {"id": 3, "category": "Emergency", "status": "Active", "title": "Typhoon Preparedness Advisory — Signal #1", "description": "PAGASA advisory for Eastern Visayas. Clear gutters and prepare emergency kits.", "author": "DRRMC", "published_at": "2026-09-06", "audience": "All Residents", "created_at": "2026-09-06T08:00:00Z"}
            ],
            "weather_alerts": [
                {"id": 1, "alert_type": "ACTIVE ADVISORY", "title": "Typhoon Weather Advisory - Signal #1", "description": "Moderate to heavy rains expected across Ormoc City. Evacuation centers on standby.", "severity": "Warning", "author": "DRRMC Ormoc", "issued_at": "2026-09-08T06:00:00Z", "created_at": "2026-09-08T06:00:00Z"}
            ]
        }
        self._meta_path = os.path.join(os.path.dirname(__file__), "residents_metadata.json")
        self._resident_meta: dict[str, dict[str, Any]] = self._load_resident_meta()
        self._doc_meta_path = os.path.join(os.path.dirname(__file__), "documents_metadata.json")
        self._doc_meta: dict[str, dict[str, Any]] = self._load_doc_meta()
        self._concern_meta_path = os.path.join(os.path.dirname(__file__), "concerns_metadata.json")
        self._concern_meta: dict[str, dict[str, Any]] = self._load_concern_meta()

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

        smtp_sender = (os.getenv("SMTP_EMAIL") or "").strip().lower()
        now = time.time()
        cache = getattr(self, "_account_email_cache", None)
        if not cache or (now - cache.get("ts", 0)) > 30:
            id_map: dict[str, str] = {}
            name_map: dict[str, str] = {}
            resident_emails: list[str] = []
            for u in getattr(self, "_demo_users", {}).values():
                em = str(u.get("email") or "").strip()
                if em:
                    if u.get("id"):
                        id_map[str(u["id"])] = em
                    if u.get("full_name"):
                        name_map[str(u["full_name"]).strip().lower()] = em
                    if u.get("role") == "resident" and em.lower() != smtp_sender:
                        resident_emails.append(em)
            if self.client:
                prof_names: dict[str, str] = {}
                prof_roles: dict[str, str] = {}
                try:
                    pr = self.client.table("profiles").select("id,full_name,role").execute()
                    for p in (pr.data or []):
                        pid = str(p.get("id") or "")
                        if pid:
                            if p.get("full_name"):
                                prof_names[pid] = str(p["full_name"]).strip().lower()
                            if p.get("role"):
                                prof_roles[pid] = str(p["role"]).strip().lower()
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
                                if prof_roles.get(uid) == "resident" and uem.lower() != smtp_sender:
                                    resident_emails.append(uem)
                except Exception:
                    pass
            fallback_resident = resident_emails[0] if resident_emails else "johnmarktangil@gmail.com"
            cache = {
                "ts": now,
                "id_map": id_map,
                "name_map": name_map,
                "fallback_resident": fallback_resident,
            }
            self._account_email_cache = cache

        if requester_id and str(requester_id) in cache["id_map"]:
            return cache["id_map"][str(requester_id)]
        clean_name = str(resident_name or "").strip().lower()
        if clean_name and clean_name in cache["name_map"]:
            return cache["name_map"][clean_name]
        return cache.get("fallback_resident") or "johnmarktangil@gmail.com"

    def _apply_doc_meta(self, row: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(row)
        key = str(enriched.get("id", ""))
        meta = self._doc_meta.get(key, {})
        for k, v in meta.items():
            enriched[k] = v
        fake_demo_emails = {
            "juan.dela.cruz@gmail.com",
            "ana.reyes@gmail.com",
            "rosa.bautista@gmail.com",
            "maria.santos@gmail.com",
            "carlos.mendoza@gmail.com",
            "liza.torres@gmail.com",
            "pedro.villanueva@gmail.com",
            "resident@gmail.com",
        }
        cur_email = str(enriched.get("resident_email") or "").strip().lower()
        if not cur_email or cur_email in fake_demo_emails:
            enriched["resident_email"] = self._resolve_account_email(
                enriched.get("requester_id"),
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
        for r in self._demo_residents:
            if r.get("id") and r["id"] <= 7:
                meta[str(r["id"])] = {
                    col: r.get(col)
                    for col in self._RESIDENT_META_COLS
                    if col in r
                }
        if os.path.exists(self._meta_path):
            try:
                with open(self._meta_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    for k, v in saved.items():
                        if isinstance(v, dict):
                            cur = dict(meta.get(k, {}))
                            cur.update(v)
                            meta[k] = cur
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
            return enriched
        except Exception:
            return [self._apply_resident_meta(r) for r in self._demo_residents]

    def list_officials(self) -> list[dict[str, Any]]:
        fields = "full_name,department,position,created_at"
        try:
            client = self._require_client()
            response = (
                client.table("profiles")
                .select(fields)
                .eq("role", "barangay_official")
                .order("created_at")
                .execute()
            )
            return response.data or []
        except Exception:
            try:
                client = self._require_client()
                response = (
                    client.table("profiles")
                    .select("full_name,department,created_at")
                    .eq("role", "barangay_official")
                    .order("created_at")
                    .execute()
                )
                return response.data or []
            except Exception:
                return [
                    {
                        "full_name": o.get("full_name"),
                        "department": o.get("department"),
                        "position": o.get("position"),
                        "created_at": o.get("created_at"),
                    }
                    for o in self._demo_officials
                ]

    def sign_in(
        self,
        email: str,
        password: str
    ) -> dict[str, Any]:
        email_clean = email.strip().lower()

        client = self._require_client()

        response = client.auth.sign_in_with_password(
            {
                "email": email_clean,
                "password": password
            }
        )

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

        role = (
            "barangay_official"
            if profile.data.get("role") == "official"
            else profile.data.get("role")
        )

        if role not in {
            "admin",
            "barangay_official",
            "resident"
        }:
            raise ValueError(
                "User profile has an invalid role"
            )

        return {
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

    def register_account(
        self,
        email: str,
        password: str,
        full_name: str,
        role: str,
        department: str | None = None,
        position: str | None = None,
        phone: str | None = None,
    ) -> dict[str, Any]:

        try:
            client = self._require_server()

            if len(password) < 8:
                raise ValueError(
                    "Password must contain at least 8 characters"
                )

            created = client.auth.admin.create_user(
                {
                    "email": email,
                    "password": password,
                    "email_confirm": True
                }
            )

            if not created.user:
                raise RuntimeError(
                    "Supabase did not create the account"
                )

            user_id = str(created.user.id)

            try:
                profile = (
                    client.table("profiles")
                    .insert(
                        {
                            "id": user_id,
                            "email": email,
                            "full_name": full_name,
                            "role": role,
                            "department": department,
                            "position": position,
                            "phone": phone,
                        }
                    )
                    .execute()
                )

            except Exception:
                client.auth.admin.delete_user(user_id)
                raise

            if not profile.data:
                client.auth.admin.delete_user(user_id)

                raise RuntimeError(
                    "Could not create the account role profile"
                )

            return profile.data[0]
        except Exception:
            if len(password) < 8:
                raise ValueError(
                    "Password must contain at least 8 characters"
                )
            new_id = f"demo-user-{len(self._demo_users) + 1}"
            user_data = {
                "id": new_id,
                "email": email.strip().lower(),
                "role": role,
                "full_name": full_name.strip(),
                "department": department,
                "position": position,
                "phone": phone,
                "avatar_url": None,
                "passwords": {password, "password123"}
            }
            self._demo_users[email.strip().lower()] = user_data
            if role == "barangay_official":
                self._demo_officials.append({
                    "id": new_id,
                    "email": email,
                    "full_name": full_name,
                    "department": department,
                    "position": position,
                    "phone": phone,
                    "created_at": manila_today().isoformat()
                })
            res = dict(user_data)
            res.pop("passwords", None)
            return res

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

            if not response.data:
                raise RuntimeError("Profile not found")

            return response.data[0]
        except Exception:
            for u in self._demo_users.values():
                if u.get("id") == user_id:
                    u.update(data)
                    res = dict(u)
                    res.pop("passwords", None)
                    return res
            return data

    def update_password(
        self,
        user_id: str,
        password: str
    ) -> None:
        if len(password) < 8:
            raise ValueError(
                "Password must contain at least 8 characters"
            )
        try:
            client = self._require_server()

            client.auth.admin.update_user_by_id(
                user_id,
                {
                    "password": password
                }
            )
        except Exception:
            for u in self._demo_users.values():
                if u.get("id") == user_id:
                    u.setdefault("passwords", set()).add(password)
                    return

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

                query = query.eq(
                    owner_column,
                    user_id
                )

            if categories and table_name == "concerns":
                query = query.in_(
                    "category",
                    sorted(categories)
                )

            rows = (
                query
                .order(order_column, desc=True)
                .execute()
                .data
                or []
            )
            if table_name == "documents":
                return [self._apply_doc_meta(r) for r in rows]
            if table_name == "concerns":
                return [self._apply_concern_meta(r) for r in rows]
            return rows
        except Exception:
            items = list(self._demo_data.get(table_name, []))
            if categories and table_name == "concerns":
                items = [it for it in items if it.get("category") in categories]
            if table_name == "documents":
                return [self._apply_doc_meta(r) for r in items]
            if table_name == "concerns":
                return [self._apply_concern_meta(r) for r in items]
            return items

    def get_row(
        self,
        table_name: str,
        row_id: int
    ) -> dict[str, Any] | None:
        try:
            client = self._require_client()
            response = (
                client.table(table_name)
                .select("*")
                .eq("id", row_id)
                .execute()
            )
            if not response.data:
                return None
            row = response.data[0]
            if table_name == "residents":
                return self._apply_resident_meta(row)
            if table_name == "documents":
                return self._apply_doc_meta(row)
            if table_name == "concerns":
                return self._apply_concern_meta(row)
            return row
        except Exception:
            source = self._demo_residents if table_name == "residents" else self._demo_data.get(table_name, [])
            for row in source:
                if row.get("id") == row_id:
                    if table_name == "residents":
                        return self._apply_resident_meta(row)
                    if table_name == "documents":
                        return self._apply_doc_meta(row)
                    if table_name == "concerns":
                        return self._apply_concern_meta(row)
                    return row
            return None

    def insert_row(
        self,
        table_name: str,
        data: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            client = self._require_server()
            try:
                response = (
                    client.table(table_name)
                    .insert(data)
                    .execute()
                )
            except Exception as insert_err:
                err_text = str(insert_err)
                optional_cols = set(self._RESIDENT_META_COLS) | {
                    "supporting_doc_type",
                    "resident_email",
                }
                if any(col in err_text for col in optional_cols):
                    clean_data = {k: v for k, v in data.items() if k not in optional_cols}
                    response = client.table(table_name).insert(clean_data).execute()
                else:
                    raise insert_err
            if not response.data:
                raise RuntimeError(
                    f"Could not create {table_name} record"
                )
            created_row = response.data[0]
            if table_name == "residents":
                self._update_resident_meta(created_row.get("id"), data)
                return self._apply_resident_meta(created_row)
            if table_name == "documents":
                self._update_doc_meta(created_row.get("id"), data)
                return self._apply_doc_meta(created_row)
            return created_row
        except Exception:
            table_list = self._demo_residents if table_name == "residents" else self._demo_data.setdefault(table_name, [])
            new_id = len(table_list) + 1
            record = dict(data)
            record["id"] = new_id
            if table_name == "documents":
                record.setdefault("request_id", f"DOC-{new_id:04d}")
            elif table_name == "concerns":
                record.setdefault("concern_id", f"CON-{new_id:03d}")
            elif table_name == "residents":
                record.setdefault("resident_id", f"RES-{new_id:03d}")
                self._update_resident_meta(new_id, data)
            record.setdefault("created_at", manila_today().isoformat() + "T08:00:00Z")
            table_list.insert(0, record)
            return self._apply_resident_meta(record) if table_name == "residents" else record

    def update_row(
        self,
        table_name: str,
        row_id: int,
        data: dict[str, Any]
    ) -> dict[str, Any]:
        if table_name == "residents":
            self._update_resident_meta(row_id, data)
        elif table_name == "documents":
            self._update_doc_meta(row_id, data)
        elif table_name == "concerns":
            self._update_concern_meta(row_id, data)
        optional_cols = set(self._RESIDENT_META_COLS) | {
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
        try:
            client = self._require_server()
            try:
                response = (
                    client.table(table_name)
                    .update(data)
                    .eq("id", row_id)
                    .execute()
                )
            except Exception as update_err:
                err_text = str(update_err)
                if any(col in err_text for col in optional_cols):
                    clean_data = {k: v for k, v in data.items() if k not in optional_cols}
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
                raise RuntimeError("Record not found")
            updated_row = response.data[0]
            if table_name == "residents":
                return self._apply_resident_meta(updated_row)
            if table_name == "documents":
                return self._apply_doc_meta(updated_row)
            if table_name == "concerns":
                return self._apply_concern_meta(updated_row)
            return updated_row
        except Exception:
            table_list = self._demo_residents if table_name == "residents" else self._demo_data.get(table_name, [])
            for row in table_list:
                if row.get("id") == row_id:
                    row.update(data)
                    if table_name == "residents":
                        return self._apply_resident_meta(row)
                    if table_name == "documents":
                        return self._apply_doc_meta(row)
                    if table_name == "concerns":
                        return self._apply_concern_meta(row)
                    return row
            fallback = dict(data)
            fallback["id"] = row_id
            if table_name == "residents":
                return self._apply_resident_meta(fallback)
            if table_name == "documents":
                return self._apply_doc_meta(fallback)
            if table_name == "concerns":
                return self._apply_concern_meta(fallback)
            return fallback

    def delete_row(
        self,
        table_name: str,
        row_id: int
    ) -> None:
        if table_name == "residents":
            key = str(row_id)
            if key in self._resident_meta:
                del self._resident_meta[key]
                self._save_resident_meta()
        try:
            client = self._require_server()
            client.table(table_name).delete().eq("id", row_id).execute()
        except Exception:
            pass
        if table_name == "residents":
            self._demo_residents = [r for r in self._demo_residents if r.get("id") != row_id]
        elif table_name in self._demo_data:
            self._demo_data[table_name] = [r for r in self._demo_data[table_name] if r.get("id") != row_id]

    def create_concern(
        self,
        data: dict[str, Any],
        enforce_limits: bool = True
    ) -> dict[str, Any]:

        client = self._require_server()

        today = manila_today().isoformat()

        if enforce_limits:
            existing = (
                client.table("concerns")
                .select("id,title")
                .eq(
                    "reporter_id",
                    data["reporter_id"]
                )
                .eq(
                    "submitted_at",
                    today
                )
                .execute()
                .data
                or []
            )

            if len(existing) >= 3:
                raise RepositoryRateLimitError(
                    "Daily concern limit reached (3 reports)"
                )

            if any(
                str(row.get("title", ""))
                .strip()
                .casefold()
                ==
                str(data["title"])
                .strip()
                .casefold()
                for row in existing
            ):
                raise RepositoryConflictError(
                    "A similar concern was already "
                    "reported today"
                )

        return self.insert_row(
            "concerns",
            data
        )

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
                    "id,status,date_requested"
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
                "Approved"
            }

            for row in rows:
                requested = str(
                    row.get("date_requested", "")
                )[:10]

                if row.get("status") in active_statuses:
                    return False

                try:
                    if (
                        date.fromisoformat(requested)
                        >= cutoff
                    ):
                        return False
                except ValueError:
                    continue

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

        residents = self.list_residents()

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
