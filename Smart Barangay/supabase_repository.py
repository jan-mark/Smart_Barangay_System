import os
from collections import Counter
from datetime import date, timedelta
from typing import Any

from dotenv import load_dotenv
from supabase import Client, create_client
from time_utils import manila_month_key, manila_today

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
        self.url = os.getenv("SUPABASE_URL", "").strip()

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
        client = self._require_client()

        response = (
            client.table("residents")
            .select("*")
            .order("resident_id")
            .execute()
        )

        return response.data or []

    def list_officials(self) -> list[dict[str, Any]]:
        client = self._require_client()

        fields = (
            "id,email,full_name,department,"
            "position,phone,created_at"
        )

        try:
            response = (
                client.table("profiles")
                .select(fields)
                .eq("role", "barangay_official")
                .order("created_at")
                .execute()
            )
        except Exception as error:
            if "column" not in str(error).lower():
                raise

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

    def sign_in(
        self,
        email: str,
        password: str
    ) -> dict[str, Any]:

        client = self._require_client()

        response = client.auth.sign_in_with_password(
            {
                "email": email,
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

    def update_profile(
        self,
        user_id: str,
        data: dict[str, Any]
    ) -> dict[str, Any]:

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

    def update_password(
        self,
        user_id: str,
        password: str
    ) -> None:

        client = self._require_server()

        if len(password) < 8:
            raise ValueError(
                "Password must contain at least 8 characters"
            )

        client.auth.admin.update_user_by_id(
            user_id,
            {
                "password": password
            }
        )

    def list_table(
        self,
        table_name: str,
        order_column: str = "created_at",
        own_only: bool = False,
        user_id: str | None = None,
        categories: set[str] | None = None,
    ) -> list[dict[str, Any]]:

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

    def get_row(
        self,
        table_name: str,
        row_id: int
    ) -> dict[str, Any] | None:

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

    def insert_row(
        self,
        table_name: str,
        data: dict[str, Any]
    ) -> dict[str, Any]:

        client = self._require_server()

        response = (
            client.table(table_name)
            .insert(data)
            .execute()
        )

        if not response.data:
            raise RuntimeError(
                f"Could not create {table_name} record"
            )

        return response.data[0]

    def update_row(
        self,
        table_name: str,
        row_id: int,
        data: dict[str, Any]
    ) -> dict[str, Any]:

        client = self._require_server()

        response = (
            client.table(table_name)
            .update(data)
            .eq("id", row_id)
            .select("*")
            .execute()
        )

        if not response.data:
            raise RuntimeError("Record not found")

        return response.data[0]

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
