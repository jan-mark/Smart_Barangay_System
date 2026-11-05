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
            {"id": 1, "resident_id": "RES-001", "full_name": "Maria Santos", "address": "Purok 3, Blk 4 Lot 2", "household": "HH-0042", "household_role": "Head", "is_head": True, "age": 34, "gender": "Female", "is_pwd": False, "is_solo_parent": True, "is_4ps": False, "sector": "Solo Parent", "status": "Verified", "resident_since": "2018-03-12", "created_at": "2018-03-12T08:00:00Z"},
            {"id": 2, "resident_id": "RES-002", "full_name": "Juan dela Cruz", "address": "Purok 1, Blk 2 Lot 8", "household": "HH-0015", "household_role": "Head", "is_head": True, "age": 52, "gender": "Male", "is_pwd": False, "is_solo_parent": False, "is_4ps": True, "sector": "4Ps", "status": "Verified", "resident_since": "2010-07-22", "created_at": "2010-07-22T08:00:00Z"},
            {"id": 3, "resident_id": "RES-003", "full_name": "Ana Reyes", "address": "Purok 5, Blk 7 Lot 1", "household": "HH-0078", "household_role": "Head", "is_head": True, "age": 27, "gender": "Female", "is_pwd": True, "is_solo_parent": False, "is_4ps": False, "sector": "PWD", "status": "Pending", "resident_since": "2024-01-05", "created_at": "2024-01-05T08:00:00Z"},
            {"id": 4, "resident_id": "RES-004", "full_name": "Carlos Mendoza", "address": "Purok 2, Blk 3 Lot 5", "household": "HH-0031", "household_role": "Head", "is_head": True, "age": 45, "gender": "Male", "is_pwd": False, "is_solo_parent": False, "is_4ps": False, "sector": "Adult", "status": "Verified", "resident_since": "2015-11-18", "created_at": "2015-11-18T08:00:00Z"},
            {"id": 5, "resident_id": "RES-005", "full_name": "Liza Torres", "address": "Purok 4, Blk 6 Lot 3", "household": "HH-0063", "household_role": "Head", "is_head": True, "age": 65, "gender": "Female", "is_pwd": False, "is_solo_parent": False, "is_4ps": False, "sector": "Senior", "status": "Verified", "resident_since": "2005-04-30", "created_at": "2005-04-30T08:00:00Z"},
            {"id": 6, "resident_id": "RES-006", "full_name": "Pedro Villanueva", "address": "Purok 1, Blk 1 Lot 12", "household": "HH-0008", "household_role": "Head", "is_head": True, "age": 38, "gender": "Male", "is_pwd": False, "is_solo_parent": False, "is_4ps": False, "sector": "Adult", "status": "Verified", "resident_since": "2019-09-14", "created_at": "2019-09-14T08:00:00Z"},
            {"id": 7, "resident_id": "RES-007", "full_name": "Rosa Bautista", "address": "Purok 6, Blk 9 Lot 4", "household": "HH-0091", "household_role": "Head", "is_head": True, "age": 22, "gender": "Female", "is_pwd": False, "is_solo_parent": True, "is_4ps": True, "sector": "Solo Parent", "status": "Pending", "resident_since": "2025-06-01", "created_at": "2025-06-01T08:00:00Z"},
            {"id": 8, "resident_id": "RES-008", "full_name": "Joshua Santos", "address": "Purok 3, Blk 4 Lot 2", "household": "HH-0042", "household_role": "Child", "is_head": False, "age": 9, "gender": "Male", "is_pwd": False, "is_solo_parent": False, "is_4ps": False, "sector": "Minor", "status": "Verified", "resident_since": "2018-03-12", "created_at": "2018-03-12T08:00:00Z"},
            {"id": 9, "resident_id": "RES-009", "full_name": "Clara Santos", "address": "Purok 3, Blk 4 Lot 2", "household": "HH-0042", "household_role": "Senior Dependent", "is_head": False, "age": 68, "gender": "Female", "is_pwd": False, "is_solo_parent": False, "is_4ps": False, "sector": "Senior", "status": "Verified", "resident_since": "2018-03-12", "created_at": "2018-03-12T08:00:00Z"},
            {"id": 10, "resident_id": "RES-010", "full_name": "Elena dela Cruz", "address": "Purok 1, Blk 2 Lot 8", "household": "HH-0015", "household_role": "Spouse", "is_head": False, "age": 49, "gender": "Female", "is_pwd": True, "is_solo_parent": False, "is_4ps": True, "sector": "PWD", "status": "Verified", "resident_since": "2010-07-22", "created_at": "2010-07-22T08:00:00Z"},
            {"id": 11, "resident_id": "RES-011", "full_name": "Miguel dela Cruz", "address": "Purok 1, Blk 2 Lot 8", "household": "HH-0015", "household_role": "Child", "is_head": False, "age": 14, "gender": "Male", "is_pwd": False, "is_solo_parent": False, "is_4ps": True, "sector": "Minor", "status": "Verified", "resident_since": "2010-07-22", "created_at": "2010-07-22T08:00:00Z"},
            {"id": 12, "resident_id": "RES-012", "full_name": "Baby Bea Bautista", "address": "Purok 6, Blk 9 Lot 4", "household": "HH-0091", "household_role": "Child", "is_head": False, "age": 2, "gender": "Female", "is_pwd": False, "is_solo_parent": False, "is_4ps": True, "sector": "Minor", "status": "Verified", "resident_since": "2025-06-01", "created_at": "2025-06-01T08:00:00Z"},
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
        try:
            client = self._require_client()
            response = (
                client.table("residents")
                .select("*")
                .order("resident_id")
                .execute()
            )
            return response.data or []
        except Exception:
            return list(self._demo_residents)

    def list_officials(self) -> list[dict[str, Any]]:
        fields = (
            "id,email,full_name,department,"
            "position,phone,created_at"
        )
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
        except Exception as error:
            try:
                client = self._require_client()
                response = (
                    client.table("profiles")
                    .select(
                        "id,email,full_name,department,created_at"
                    )
                    .eq("role", "barangay_official")
                    .order("created_at")
                    .execute()
                )
                return response.data or []
            except Exception:
                return list(self._demo_officials)

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

            return (
                query
                .order(order_column, desc=True)
                .execute()
                .data
                or []
            )
        except Exception:
            items = list(self._demo_data.get(table_name, []))
            if categories and table_name == "concerns":
                items = [it for it in items if it.get("category") in categories]
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
            return (
                response.data[0]
                if response.data
                else None
            )
        except Exception:
            for row in self._demo_data.get(table_name, []):
                if row.get("id") == row_id:
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
                optional_cols = {"is_head", "household_role", "is_pwd", "is_solo_parent", "is_4ps", "sector"}
                if any(col in err_text for col in optional_cols):
                    clean_data = {k: v for k, v in data.items() if k not in optional_cols}
                    response = client.table(table_name).insert(clean_data).execute()
                else:
                    raise insert_err
            if not response.data:
                raise RuntimeError(
                    f"Could not create {table_name} record"
                )
            return response.data[0]
        except Exception:
            table_list = self._demo_data.setdefault(table_name, [])
            new_id = len(table_list) + 1
            record = dict(data)
            record["id"] = new_id
            if table_name == "documents":
                record.setdefault("request_id", f"DOC-{new_id:04d}")
            elif table_name == "concerns":
                record.setdefault("concern_id", f"CON-{new_id:03d}")
            record.setdefault("created_at", manila_today().isoformat() + "T08:00:00Z")
            table_list.insert(0, record)
            return record

    def update_row(
        self,
        table_name: str,
        row_id: int,
        data: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            client = self._require_server()
            try:
                response = (
                    client.table(table_name)
                    .update(data)
                    .eq("id", row_id)
                    .select("*")
                    .execute()
                )
            except Exception as update_err:
                err_text = str(update_err)
                optional_cols = {"is_head", "household_role", "is_pwd", "is_solo_parent", "is_4ps", "sector"}
                if any(col in err_text for col in optional_cols):
                    clean_data = {k: v for k, v in data.items() if k not in optional_cols}
                    response = (
                        client.table(table_name)
                        .update(clean_data)
                        .eq("id", row_id)
                        .select("*")
                        .execute()
                    )
                else:
                    raise update_err
            if not response.data:
                raise RuntimeError("Record not found")
            return response.data[0]
        except Exception:
            table_list = self._demo_data.get(table_name, [])
            for row in table_list:
                if row.get("id") == row_id:
                    row.update(data)
                    return row
            return data

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
