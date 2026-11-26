
import os
import csv
import io
import re
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import urlencode
from urllib.request import urlopen
import json
import httpx
from xml.sax.saxutils import escape as xml_escape

from flask import Flask, jsonify, make_response, redirect, render_template, request, session, url_for
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Image as RLImage, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "System is running!"
from supabase_repository import (
    RepositoryConfigurationError,
    RepositoryConflictError,
    RepositoryRateLimitError,
    supabase_repository,
)

from time_utils import manila_now, manila_today


app = Flask(__name__, static_folder="public/static", static_url_path="/static")
app.secret_key = os.getenv("FLASK_SECRET_KEY") or "change-this-development-secret"
app.config.update(
    TEMPLATES_AUTO_RELOAD=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("FLASK_SESSION_SECURE", "false").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)

@app.after_request
def add_no_cache_headers(response):
    if response.content_type.startswith(("text/html", "text/css", "application/javascript")):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

ALL_RBAC_MODULES = [
    "Dashboard",
    "Residents",
    "Manage Officials",
    "Documents",
    "Concerns",
    "Announcements",
    "Weather & Alerts",
    "Reports",
    "Settings",
]
ALL_RBAC_MODULES_SET = set(ALL_RBAC_MODULES)
RBAC_ROLES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rbac_roles.json")

DEFAULT_RBAC_ROLES = [
    {
        "id": "super-admin",
        "name": "Super Admin (Barangay Captain)",
        "description": "Full system control & registers Barangay Officials",
        "modules": list(ALL_RBAC_MODULES),
    },
    {
        "id": "admin",
        "name": "Barangay Secretary",
        "description": "Manage residents, documents, concerns & reports",
        "modules": ["Dashboard", "Residents", "Documents", "Reports", "Settings"],
    },
    {
        "id": "barangay-official",
        "name": "Barangay Officials",
        "description": "Officials registered by the Super Admin",
        "modules": ["Dashboard", "Residents", "Documents", "Concerns"],
    },
]

ROLE_ACCESS = {
    "super_admin": set(ALL_RBAC_MODULES),
    "admin": {"Dashboard", "Residents", "Documents", "Reports", "Settings"},
    "barangay_official": {"Dashboard", "Residents", "Documents", "Concerns"},
    "official": {"Dashboard", "Residents", "Documents", "Concerns"},
}


def _normalize_rbac_roles(raw_roles) -> list[dict]:
    if not isinstance(raw_roles, list) or not raw_roles:
        return [dict(r, modules=list(r["modules"])) for r in DEFAULT_RBAC_ROLES]
    normalized: list[dict] = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(raw_roles):
        if not isinstance(item, dict):
            continue
        rid = str(item.get("id") or f"role-{idx + 1}").strip()
        if not rid or rid in seen_ids:
            continue
        seen_ids.add(rid)
        name = str(item.get("name") or "Custom Role").strip()[:80] or "Custom Role"
        desc = str(item.get("description") or "").strip()[:200]
        if rid == "super-admin":
            mods = list(ALL_RBAC_MODULES)
        else:
            raw_mods = item.get("modules") if isinstance(item.get("modules"), list) else []
            mods = [m for m in ALL_RBAC_MODULES if m in set(str(x).strip() for x in raw_mods)]
        normalized.append({"id": rid, "name": name, "description": desc, "modules": mods})
    if "super-admin" not in seen_ids:
        normalized.insert(0, dict(DEFAULT_RBAC_ROLES[0], modules=list(ALL_RBAC_MODULES)))
    return normalized


def load_rbac_roles() -> list[dict]:
    if os.path.exists(RBAC_ROLES_FILE):
        try:
            with open(RBAC_ROLES_FILE, "r", encoding="utf-8") as fh:
                return _normalize_rbac_roles(json.load(fh))
        except Exception:
            app.logger.exception("Could not read rbac_roles.json; falling back to defaults")
    return [dict(r, modules=list(r["modules"])) for r in DEFAULT_RBAC_ROLES]


def save_rbac_roles(raw_roles) -> list[dict]:
    cleaned = _normalize_rbac_roles(raw_roles)
    tmp_path = f"{RBAC_ROLES_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(cleaned, fh, indent=2, ensure_ascii=False)
    os.replace(tmp_path, RBAC_ROLES_FILE)
    return cleaned


def resolve_user_rbac(user: dict | None) -> tuple[set[str], dict | None]:
    if not user:
        return set(), None
    roles = load_rbac_roles()
    by_id = {r["id"]: r for r in roles}
    raw_role = str(user.get("role") or "").strip().lower()
    pos = str(user.get("position") or "").strip().lower()
    dept = str(user.get("department") or "").strip().lower()

    if raw_role == "super_admin":
        sa = by_id.get("super-admin") or DEFAULT_RBAC_ROLES[0]
        return set(ALL_RBAC_MODULES), sa

    # 1. Check custom roles added by Super Admin (matched against official's position or department)
    built_in_ids = {"super-admin", "admin", "barangay-official"}
    for r in roles:
        if r["id"] in built_in_ids:
            continue
        rname = str(r.get("name") or "").strip().lower()
        if not rname:
            continue
        if pos and (rname == pos or (len(rname) >= 4 and rname in pos) or (len(pos) >= 4 and pos in rname)):
            return set(r.get("modules") or []), r
        if dept and rname == dept:
            return set(r.get("modules") or []), r

    # 2. Barangay Secretary / Admin role
    if raw_role == "admin" or "secretary" in pos:
        sec_role = by_id.get("admin") or next((r for r in roles if "secretary" in r["name"].lower()), None)
        if sec_role:
            return set(sec_role.get("modules") or []), sec_role
        return set(ROLE_ACCESS.get("admin", set())), None

    # 3. Authorized Barangay Officials role
    if raw_role in {"barangay_official", "official"}:
        off_role = by_id.get("barangay-official") or next((r for r in roles if "official" in r["name"].lower()), None)
        if off_role:
            return set(off_role.get("modules") or []), off_role
        return set(ROLE_ACCESS.get("barangay_official", set())), None

    return set(ROLE_ACCESS.get(raw_role, set())), None
DEPARTMENT_CATEGORIES = {
    "Health & Sanitation": {"Health & Sanitation"},
    "Peace & Order": {"Peace & Order"},
    "Infrastructure": {"Infrastructure"},
    "Social Services": {"Social Services"},
    "DRRMC": {"Weather & Alerts"},
}
DOCUMENT_TYPES = {"Barangay Clearance", "Certificate of Residency", "Certificate of Indigency", "Business Clearance"}
DOCUMENT_STATUSES = {"Pending", "In Progress", "Processing", "Approved", "Rejected", "Resolved"}
CONCERN_STATUSES = {"Pending", "Open", "In Progress", "Escalated", "Resolved"}
CONCERN_PRIORITIES = {"Low", "Medium", "High", "Critical"}
ANNOUNCEMENT_CATEGORIES = {"General", "Assembly", "Health", "Emergency", "Livelihood", "Social Services"}
ALERT_SEVERITIES = {"Open", "Advisory", "Warning", "Critical", "Escalated", "Resolved"}
CIVIL_STATUSES = {"Single", "Live-in", "Married", "Widowed", "Widow/Widower", "Separated", "Divorced"}


def server_data_unavailable():
    return jsonify(
        {
            "error": "Server-side Supabase access is not configured. Set SUPABASE_SERVICE_ROLE_KEY.",
            "configured": supabase_repository.configured,
            "configuration": supabase_repository.configuration,
        }
    ), 503


def current_department_categories(user: dict | None) -> set[str] | None:
    return None


def current_user() -> dict | None:
    user = session.get("user")
    if not user:
        return None
    raw_role = str(user.get("role") or "").strip().lower()
    pos = str(user.get("position") or "").strip().lower()
    if raw_role == "official":
        user = {**user, "role": "barangay_official"}
        session["user"] = user
    elif raw_role == "admin" and "secretary" not in pos:
        user = {**user, "role": "super_admin"}
        session["user"] = user
    if user.get("role") != "super_admin" and supabase_repository.is_official_archived(user.get("id"), user.get("email")):
        session.clear()
        return None
    return user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def roles_required(*roles):
    allowed = set(roles)
    if "admin" in allowed:
        allowed.add("super_admin")

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                return jsonify({"error": "Authentication required"}), 401
            if user.get("role") not in allowed:
                return jsonify({"error": "You do not have permission for this action"}), 403
            return view(*args, **kwargs)
        return wrapped
    return decorator


def module_required(module_name: str):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                return jsonify({"error": "Authentication required"}), 401
            allowed_modules, _ = resolve_user_rbac(user)
            if module_name not in allowed_modules:
                return jsonify({"error": f"Your role does not have permission to access {module_name}."}), 403
            return view(*args, **kwargs)
        return wrapped
    return decorator


@app.get("/health")
def health():
    ready = supabase_repository.server_configured
    return jsonify(
        {
            "status": "ok" if ready else "degraded",
            "service": "smartbarangay",
            "supabase": supabase_repository.configuration,
        }
    ), 200 if ready else 503


@app.get("/sw.js")
def service_worker():
    sw_path = os.path.join(app.static_folder or "public/static", "sw.js")
    if not os.path.exists(sw_path):
        return "", 404
    with open(sw_path, "r", encoding="utf-8") as f:
        content = f.read()
    resp = make_response(content)
    resp.headers["Content-Type"] = "application/javascript; charset=utf-8"
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/api/sync/status")
@login_required
def sync_status():
    probe = str(request.args.get("probe", "0")).strip().lower() in ("1", "true", "yes")
    return jsonify({"data": supabase_repository.get_sync_status(probe=probe)})


@app.post("/api/sync/run")
@login_required
def sync_run():
    result = supabase_repository.sync_now()
    return jsonify({"data": result})


@app.get("/login")
def login():
    if current_user():
        return redirect(url_for("home"))
    return render_template("login.html")


@app.post("/api/login")
def login_api():
    payload = request.get_json(silent=True) or request.form
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400
    try:
        user = supabase_repository.sign_in(email, password)
        if user.get("role") not in {"super_admin", "admin", "barangay_official"}:
            return jsonify({
                "error": "This web portal is for authorized Barangay Officials only. Residents should use the SmartBarangay Mobile App."
            }), 403
        if user.get("role") != "super_admin" and supabase_repository.is_official_archived(user.get("id"), user.get("email")):
            return jsonify({
                "error": "This official account has been archived. Please contact the Barangay Captain."
            }), 403
        session["user"] = user
        session.permanent = True
        return jsonify({"data": user})
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
    except ValueError as error:
        msg = str(error)
        if "offline" in msg.lower():
            return jsonify({"error": msg}), 503
        return jsonify({"error": "Invalid email or password"}), 401
    except Exception:
        app.logger.exception("Login failed")
        return jsonify({"error": "Invalid email or password"}), 401


@app.post("/api/logout")
def logout_api():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
def me():
    user = current_user()
    if not user:
        return jsonify({"user": None}), 401
    allowed_modules, matched_role = resolve_user_rbac(user)
    return jsonify({
        "user": user,
        "access": sorted(allowed_modules),
        "matched_role": matched_role,
    })


@app.get("/api/rbac/roles")
@login_required
def get_rbac_roles():
    user = current_user()
    return jsonify({
        "data": load_rbac_roles(),
        "persisted": os.path.exists(RBAC_ROLES_FILE),
        "can_edit": user.get("role") == "super_admin",
    })


@app.put("/api/rbac/roles")
@roles_required("super_admin")
def update_rbac_roles():
    payload = request.get_json(silent=True) or {}
    roles = payload.get("roles")
    if not isinstance(roles, list) or not roles:
        return jsonify({"error": "A non-empty roles list is required"}), 400
    saved = save_rbac_roles(roles)
    return jsonify({"data": saved, "persisted": True})


@app.patch("/api/profile")
@login_required
def update_profile():
    payload = request.get_json(silent=True) or {}
    full_name = str(payload.get("full_name", "")).strip()
    if not full_name:
        return jsonify({"error": "Full name is required"}), 400
    updates = {"full_name": full_name}
    for field in ("phone", "position", "avatar_url"):
        if field in payload:
            updates[field] = str(payload[field] or "").strip() or None
    if updates.get("avatar_url") and len(updates["avatar_url"]) > 800_000:
        return jsonify({"error": "Profile photo is too large"}), 400
    try:
        profile = supabase_repository.update_profile(current_user()["id"], updates)
        session["user"].update({key: value for key, value in updates.items() if key != "avatar_url" or value})
        if "avatar_url" in updates:
            session["user"]["avatar_url"] = updates["avatar_url"]
        return jsonify({"data": profile})
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
    except Exception as error:
        app.logger.exception("Profile update failed")
        return jsonify({"error": str(error)}), 502


@app.post("/api/account/password")
@login_required
def update_password():
    payload = request.get_json(silent=True) or {}
    password = str(payload.get("password", ""))
    confirmation = str(payload.get("confirmation", ""))
    if password != confirmation:
        return jsonify({"error": "Passwords do not match"}), 400
    try:
        supabase_repository.update_password(current_user()["id"], password)
        return jsonify({"ok": True})
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
    except Exception as error:
        app.logger.exception("Password update failed")
        return jsonify({"error": str(error)}), 502


@app.route("/")
@login_required
def home():
    user = current_user()
    if user.get("role") not in {"super_admin", "admin", "barangay_official"}:
        session.clear()
        return redirect(url_for("login"))
    return render_template(
        "dashboard.html",
        dashboard_role=user.get("role"),
        dashboard_department=user.get("department"),
        official_mode=False,
    )


@app.get("/api/residents")
@roles_required("admin", "barangay_official")
def residents():
    if not supabase_repository.server_configured:
        return server_data_unavailable()
    try:
        return jsonify({"data": supabase_repository.list_residents(), "configured": True})
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
    except Exception as error:
        app.logger.exception("Could not load residents from Supabase")
        return jsonify({"error": str(error), "configured": True}), 502


@app.get("/api/officials")
@module_required("Manage Officials")
def officials():
    if not supabase_repository.server_configured:
        return server_data_unavailable()
    try:
        return jsonify({"data": supabase_repository.list_officials(), "configured": True})
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
    except Exception as error:
        app.logger.exception("Could not load barangay officials")
        return jsonify({"error": str(error), "configured": True}), 502


@app.patch("/api/officials/<official_id>")
@module_required("Manage Officials")
def update_official_endpoint(official_id: str):
    if not supabase_repository.server_configured:
        return server_data_unavailable()
    payload = request.get_json(silent=True) or {}
    updates: dict = {}
    if "full_name" in payload:
        name_val = str(payload.get("full_name") or "").strip()
        if not name_val:
            return jsonify({"error": "Full name cannot be empty"}), 400
        updates["full_name"] = name_val
    if "position" in payload:
        pos_val = str(payload.get("position") or "").strip()
        if not pos_val:
            return jsonify({"error": "Position cannot be empty"}), 400
        updates["position"] = pos_val
        updates["role"] = "admin" if "secretary" in pos_val.lower() else "barangay_official"
    if "department" in payload:
        dept_val = str(payload.get("department") or "").strip()
        if dept_val in DEPARTMENT_CATEGORIES:
            updates["department"] = dept_val
    if "phone" in payload:
        updates["phone"] = str(payload.get("phone") or "").strip()
    if "status" in payload:
        st_val = str(payload.get("status") or "").strip().lower()
        if st_val not in ("active", "archived"):
            return jsonify({"error": "Status must be Active or Archived"}), 400
        updates["status"] = "Archived" if st_val == "archived" else "Active"
    if not updates:
        return jsonify({"error": "No valid update fields provided"}), 400
    try:
        updated = supabase_repository.update_official(official_id, updates)
        return jsonify({"data": updated})
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
    except Exception as error:
        app.logger.exception("Could not update barangay official")
        return jsonify({"error": str(error)}), 502


@app.get("/api/dashboard")
@login_required
def dashboard_summary():
    if not supabase_repository.server_configured:
        return server_data_unavailable()
    try:
        return jsonify({"data": supabase_repository.dashboard_summary(current_department_categories(current_user())), "configured": True})
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
    except Exception as error:
        app.logger.exception("Could not load dashboard summary from Supabase")
        return jsonify({"error": str(error), "configured": True}), 502


def table_endpoint(table_name: str, own_only: bool = False):
    if not supabase_repository.server_configured:
        return server_data_unavailable()
    try:
        user = current_user()
        user_id = user.get("id") if own_only else None
        categories = current_department_categories(user)
        return jsonify({"data": supabase_repository.list_table(table_name, own_only=own_only, user_id=user_id, categories=categories), "configured": True})
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
    except Exception as error:
        app.logger.exception("Could not load %s from Supabase", table_name)
        return jsonify({"error": str(error), "configured": True}), 502


@app.get("/api/documents")
@login_required
def documents():
    return table_endpoint("documents", own_only=current_user()["role"] == "resident")


@app.get("/api/concerns")
@login_required
def concerns():
    return table_endpoint("concerns", own_only=current_user()["role"] == "resident")


@app.get("/api/announcements")
@login_required
def announcements():
    return table_endpoint("announcements")


@app.post("/api/announcements")
@module_required("Announcements")
def create_announcement():
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title") or "").strip()
    description = str(payload.get("description") or "").strip() or None
    audience = str(payload.get("audience") or "All Residents").strip() or "All Residents"
    is_urgent = bool(payload.get("is_urgent", False))
    is_pinned = bool(payload.get("is_pinned", False))
    raw_category = str(payload.get("category") or "").strip()
    if raw_category in ANNOUNCEMENT_CATEGORIES:
        category = raw_category
    else:
        category = "Emergency" if is_urgent else "General"
    if not title or len(title) > 180:
        return jsonify({"error": "Announcement title must be between 1 and 180 characters"}), 400
    if description and len(description) > 2000:
        return jsonify({"error": "Announcement description must be 2000 characters or fewer"}), 400
    try:
        return jsonify(
            {
                "data": supabase_repository.insert_row(
                    "announcements",
                    {
                        "category": category,
                        "title": title,
                        "description": description,
                        "audience": audience,
                        "author": current_user().get("full_name", current_user()["email"]),
                        "published_at": manila_today().isoformat(),
                        "status": "Active",
                        "is_urgent": is_urgent,
                        "is_pinned": is_pinned,
                    },
                )
            }
        ), 201
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
    except Exception as error:
        app.logger.exception("Could not create announcement")
        return jsonify({"error": str(error)}), 502


@app.patch("/api/announcements/<int:announcement_id>")
@module_required("Announcements")
def update_announcement(announcement_id: int):
    payload = request.get_json(silent=True) or {}
    updates: dict[str, object] = {}
    if "status" in payload:
        status = str(payload.get("status", "")).strip()
        if status not in {"Active", "Archived", "Expired"}:
            return jsonify({"error": "Invalid announcement status"}), 400
        updates["status"] = "Archived" if status == "Expired" else status
    if "title" in payload:
        title = str(payload.get("title") or "").strip()
        if not title or len(title) > 180:
            return jsonify({"error": "Announcement title must be between 1 and 180 characters"}), 400
        updates["title"] = title
    if "description" in payload:
        description = str(payload.get("description") or "").strip()
        if len(description) > 2000:
            return jsonify({"error": "Announcement description must be 2000 characters or fewer"}), 400
        updates["description"] = description or None
    if "audience" in payload:
        updates["audience"] = str(payload.get("audience") or "All Residents").strip() or "All Residents"
    if "is_urgent" in payload:
        is_urgent = bool(payload.get("is_urgent"))
        updates["is_urgent"] = is_urgent
        if "category" not in payload:
            updates["category"] = "Emergency" if is_urgent else "General"
    if "is_pinned" in payload:
        updates["is_pinned"] = bool(payload.get("is_pinned"))
    if "category" in payload:
        cat = str(payload.get("category") or "").strip()
        if cat in ANNOUNCEMENT_CATEGORIES:
            updates["category"] = cat
    if not updates:
        return jsonify({"error": "No announcement fields to update"}), 400
    try:
        return jsonify({"data": supabase_repository.update_row("announcements", announcement_id, updates)})
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
    except Exception as error:
        app.logger.exception("Could not update announcement")
        return jsonify({"error": str(error)}), 502


@app.get("/api/weather-alerts")
@login_required
def weather_alerts():
    return table_endpoint("weather_alerts")


@app.post("/api/weather-alerts")
@module_required("Weather & Alerts")
def create_weather_alert():
    payload = request.get_json(silent=True) or {}
    required = ("alert_type", "title", "description", "severity")
    if any(not str(payload.get(field, "")).strip() for field in required):
        return jsonify({"error": "Alert type, title, description, and severity are required"}), 400
    severity = str(payload["severity"]).strip()
    if severity not in ALERT_SEVERITIES:
        return jsonify({"error": "Choose a valid alert severity"}), 400
    try:
        return jsonify(
            {
                "data": supabase_repository.insert_row(
                    "weather_alerts",
                    {
                        "alert_type": str(payload["alert_type"]).strip(),
                        "title": str(payload["title"]).strip(),
                        "description": str(payload["description"]).strip(),
                        "severity": severity,
                        "author": current_user().get("full_name", current_user()["email"]),
                        "issued_at": manila_now().isoformat(),
                    },
                )
            }
        ), 201
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
    except Exception as error:
        app.logger.exception("Could not create weather alert")
        return jsonify({"error": str(error)}), 502


@app.post("/api/weather/auto-alert")
@module_required("Weather & Alerts")
def auto_weather_early_warning():
    if not supabase_repository.server_configured:
        return server_data_unavailable()
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title") or "").strip()
    description = str(payload.get("description") or "").strip()
    severity = str(payload.get("severity") or "Advisory").strip()
    alert_type = str(payload.get("alert_type") or "ADVANCE EARLY WARNING").strip()
    if not title or not description:
        return jsonify({"error": "Title and description are required"}), 400
    if severity not in ALERT_SEVERITIES:
        severity = "Advisory"

    now_dt = manila_now()
    cutoff_dt = now_dt - timedelta(hours=6)
    try:
        existing = supabase_repository.list_table("weather_alerts") or []
        for row in existing:
            raw_ts = str(row.get("issued_at") or row.get("created_at") or "")
            same_alert = (
                str(row.get("title") or "").strip().lower() == title.lower()
                or (
                    str(row.get("alert_type") or "").strip().upper() == alert_type.upper()
                    and str(row.get("severity") or "").strip().lower() == severity.lower()
                )
            )
            if not same_alert:
                continue
            try:
                parsed = now_dt.fromisoformat(raw_ts.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=now_dt.tzinfo)
                if parsed >= cutoff_dt:
                    return jsonify({"data": {"created": False, "deduplicated": True, "existing": row}}), 200
            except Exception:
                continue

        alert_row = supabase_repository.insert_row(
            "weather_alerts",
            {
                "alert_type": alert_type,
                "title": title,
                "description": description,
                "severity": severity,
                "author": "SmartBarangay Advance Early-Warning (Verify PAGASA)",
                "issued_at": now_dt.isoformat(),
            },
        )
        announcement_row = None
        try:
            announcement_row = supabase_repository.insert_row(
                "announcements",
                {
                    "category": "Emergency",
                    "title": title,
                    "description": description,
                    "audience": "All Residents & Barangay Officials",
                    "author": "SmartBarangay Advance Early-Warning",
                    "published_at": manila_today().isoformat(),
                    "status": "Active",
                },
            )
        except Exception:
            announcement_row = None

        return jsonify({"data": {"created": True, "alert": alert_row, "announcement": announcement_row}}), 201
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
    except Exception as error:
        app.logger.exception("Could not record automated advance weather alert")
        return jsonify({"error": str(error)}), 502


def _parse_iso_date_prefix(raw: object) -> date | None:
    s = str(raw or "").strip()
    if len(s) < 10:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _matches_report_period(raw_date: object, period: str) -> bool:
    p = str(period or "all").strip()
    if not p or p.lower() == "all":
        return True
    dt = _parse_iso_date_prefix(raw_date)
    if not dt:
        return False
    m_month = re.match(r"^(\d{4})-(\d{2})$", p)
    if m_month:
        return dt.year == int(m_month.group(1)) and dt.month == int(m_month.group(2))
    m_q = re.match(r"^Q([1-4])-(\d{4})$", p, re.IGNORECASE)
    if m_q:
        q = int(m_q.group(1))
        yr = int(m_q.group(2))
        q_start = (q - 1) * 3 + 1
        return dt.year == yr and q_start <= dt.month <= q_start + 2
    m_yr = re.match(r"^(\d{4})$", p)
    if m_yr:
        return dt.year == int(m_yr.group(1))
    return True


def _build_report_pdf(
    title: str,
    subtitle: str,
    filters_summary: str,
    summary_cards: list[dict[str, object]],
    columns: list[tuple[str, str]],
    rows: list[dict[str, object]],
    prepared_by: str,
) -> bytes:
    styles = getSampleStyleSheet()
    page_size = landscape(A4)
    margin = 0.55 * inch
    usable_w = page_size[0] - (margin * 2)

    buf = io.BytesIO()
    pdf = SimpleDocTemplate(
        buf,
        pagesize=page_size,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=0.55 * inch,
        bottomMargin=0.55 * inch,
        title=title,
        author="Barangay Don Felipe Larrazabal, Ormoc City",
    )

    h_gov = ParagraphStyle("RptGov", parent=styles["Normal"], fontName="Helvetica", fontSize=9, leading=12, alignment=TA_CENTER, textColor=colors.HexColor("#475569"))
    h_title = ParagraphStyle("RptTitle", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=16, leading=20, alignment=TA_CENTER, textColor=colors.HexColor("#0B2545"))
    h_sub = ParagraphStyle("RptSub", parent=styles["Normal"], fontName="Helvetica", fontSize=9.5, leading=13, alignment=TA_CENTER, textColor=colors.HexColor("#64748B"))
    th_style = ParagraphStyle("RptTh", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=8.5, leading=11, textColor=colors.white)
    td_style = ParagraphStyle("RptTd", parent=styles["Normal"], fontName="Helvetica", fontSize=8.5, leading=11.5, textColor=colors.HexColor("#1E293B"))
    kpi_lbl = ParagraphStyle("RptKpiLbl", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=7.5, leading=10, alignment=TA_CENTER, textColor=colors.HexColor("#64748B"))
    kpi_val = ParagraphStyle("RptKpiVal", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=14, leading=17, alignment=TA_CENTER, textColor=colors.HexColor("#0B2545"))
    sig_style = ParagraphStyle("RptSig", parent=styles["Normal"], fontName="Helvetica", fontSize=9, leading=13, textColor=colors.HexColor("#0F172A"))

    now_label = manila_now().strftime("%B %d, %Y · %I:%M %p")
    story: list[object] = [
        Paragraph("Republic of the Philippines · City of Ormoc · Barangay Don Felipe Larrazabal", h_gov),
        Spacer(1, 3),
        Paragraph(xml_escape(title), h_title),
        Paragraph(f"{xml_escape(subtitle)} &nbsp;|&nbsp; Generated: {xml_escape(now_label)}", h_sub),
        Paragraph(f"<b>Filters:</b> {xml_escape(filters_summary)}", h_sub),
        Spacer(1, 12),
    ]

    if summary_cards:
        card_cells = [
            Paragraph(f"{xml_escape(str(c['label']).upper())}<br/><font size='13' color='#0B2545'><b>{xml_escape(str(c['value']))}</b></font>", kpi_lbl)
            for c in summary_cards
        ]
        kpi_tbl = Table([card_cells], colWidths=[usable_w / len(card_cells)] * len(card_cells))
        kpi_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
            ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#CBD5E1")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.extend([kpi_tbl, Spacer(1, 12)])

    if rows:
        header_row = [Paragraph(xml_escape(lbl), th_style) for _, lbl in columns]
        body_rows = [
            [Paragraph(xml_escape(str(r.get(key) if r.get(key) not in (None, "") else "—")), td_style) for key, _ in columns]
            for r in rows
        ]
        col_w = usable_w / max(len(columns), 1)
        data_tbl = Table([header_row, *body_rows], colWidths=[col_w] * len(columns), repeatRows=1)
        tbl_styles = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0B2545")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E2E8F0")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]
        for idx in range(1, len(body_rows) + 1):
            if idx % 2 == 0:
                tbl_styles.append(("BACKGROUND", (0, idx), (-1, idx), colors.HexColor("#F8FAFC")))
        data_tbl.setStyle(TableStyle(tbl_styles))
        story.append(data_tbl)
    else:
        story.append(Paragraph("No matching records found for the selected filters.", h_sub))

    story.extend([
        Spacer(1, 22),
        Table(
            [[
                Paragraph(f"Prepared by:<br/><br/><b>{xml_escape(prepared_by)}</b><br/>Barangay Official", sig_style),
                Paragraph("Noted by:<br/><br/><b>HON. NENITO S. ANONAT</b><br/>Punong Barangay", ParagraphStyle("RptSigR", parent=sig_style, alignment=TA_RIGHT)),
            ]],
            colWidths=[usable_w * 0.5, usable_w * 0.5],
        ),
    ])

    pdf.build(story)
    return buf.getvalue()


@app.get("/api/reports/<report_name>")
@module_required("Reports")
def download_report(report_name: str):
    if not supabase_repository.server_configured:
        return server_data_unavailable()
    categories = current_department_categories(current_user())
    out_format = str(request.args.get("format", "csv")).strip().lower()
    prepared_by = str(current_user().get("full_name") or current_user().get("email") or "Barangay Official")

    try:
        if report_name == "residents":
            all_rows = supabase_repository.list_residents()
            period = str(request.args.get("period", "all")).strip()
            purok = str(request.args.get("purok", "all")).strip()
            groups_raw = [g.strip().lower() for g in str(request.args.get("groups", "all")).split(",") if g.strip()]
            use_all_groups = (not groups_raw) or ("all" in groups_raw)

            filtered = []
            for r in all_rows:
                if str(r.get("status") or "").strip().lower() == "inactive":
                    continue
                if not _matches_report_period(r.get("resident_since") or r.get("created_at"), period):
                    continue
                if purok and purok.lower() != "all":
                    addr = str(r.get("address") or "").lower()
                    if purok.lower() not in addr:
                        continue
                try:
                    age_num = int(r.get("age") or 0)
                except (TypeError, ValueError):
                    age_num = 0
                sec = str(r.get("sector") or "").lower()
                is_head = bool(r.get("is_head")) or str(r.get("household_role") or "").strip().lower() == "head"
                is_senior = age_num >= 60 or bool(r.get("is_senior")) or "senior" in sec
                is_pwd = bool(r.get("is_pwd")) or "pwd" in sec
                is_lgbt = bool(r.get("is_lgbt")) or "lgbt" in sec
                is_solo_parent = bool(r.get("is_solo_parent")) or "solo parent" in sec
                is_pregnant = bool(r.get("is_pregnant")) or "pregnant" in sec
                is_young_parent = bool(r.get("is_young_parent")) or "young parent" in sec
                is_4ps = bool(r.get("is_4ps")) or "4ps" in sec

                if not use_all_groups:
                    matched_group = False
                    if "leader" in groups_raw and is_head:
                        matched_group = True
                    if ("kids" in groups_raw or "minors" in groups_raw) and 0 <= age_num <= 17:
                        matched_group = True
                    if "adults" in groups_raw and 18 <= age_num <= 59:
                        matched_group = True
                    if "seniors" in groups_raw and is_senior:
                        matched_group = True
                    if "pwd" in groups_raw and is_pwd:
                        matched_group = True
                    if "lgbt" in groups_raw and is_lgbt:
                        matched_group = True
                    if "solo_parent" in groups_raw and is_solo_parent:
                        matched_group = True
                    if "pregnant" in groups_raw and is_pregnant:
                        matched_group = True
                    if "young_parent" in groups_raw and is_young_parent:
                        matched_group = True
                    if "4ps" in groups_raw and is_4ps:
                        matched_group = True
                    if not matched_group:
                        continue

                sector_tags = []
                if is_head:
                    sector_tags.append("Household Head")
                if age_num < 18:
                    sector_tags.append("Kids / Minors (0-17)")
                elif is_senior:
                    sector_tags.append("Senior Citizen (60+)")
                else:
                    sector_tags.append("Adults (18-59)")
                if is_pwd:
                    sector_tags.append("PWD")
                if is_lgbt:
                    sector_tags.append("LGBT")
                if is_solo_parent:
                    sector_tags.append("Solo Parent")
                if is_pregnant:
                    sector_tags.append("Pregnant")
                if is_young_parent:
                    sector_tags.append("Young Parent (13-19 yrs. old)")
                if is_4ps:
                    sector_tags.append("4Ps Beneficiary")

                row_copy = dict(r)
                row_copy["sector"] = ", ".join(sector_tags)
                filtered.append(row_copy)

            group_labels_map = {
                "leader": "Household Heads",
                "kids": "Kids / Minors (0-17)",
                "minors": "Kids / Minors (0-17)",
                "adults": "Adults (18-59)",
                "seniors": "Senior Citizen (60+)",
                "pwd": "PWD",
                "lgbt": "LGBT",
                "solo_parent": "Solo Parent",
                "pregnant": "Pregnant",
                "young_parent": "Young Parent (13-19 yrs. old)",
                "4ps": "4Ps Beneficiary",
            }
            title = "Resident Registration Report"
            subtitle = "Residents by age group and sector"
            period_lbl = "All Time" if period.lower() == "all" else period
            grp_lbl = "All Sectors" if use_all_groups else ", ".join(group_labels_map.get(g, g.replace("_", " ").title()) for g in groups_raw)
            purok_lbl = "All Puroks" if purok.lower() == "all" else purok
            filters_summary = f"Period: {period_lbl} · Sector: {grp_lbl} · Purok: {purok_lbl}"
            columns = [
                ("resident_id", "Resident ID"),
                ("full_name", "Full Name"),
                ("age", "Age"),
                ("gender", "Sex"),
                ("marital_status", "Civil Status"),
                ("household", "Household Number"),
                ("address", "Address"),
                ("sector", "Sector"),
                ("status", "Status"),
            ]
            verified_cnt = sum(1 for r in filtered if str(r.get("status") or "").lower() == "verified")
            hh_cnt = len({str(r.get("household") or "").strip() for r in filtered if str(r.get("household") or "").strip()})
            summary_cards = [
                {"label": "Total Residents", "value": len(filtered)},
                {"label": "Verified", "value": verified_cnt},
                {"label": "Households", "value": hh_cnt},
            ]
            rows = filtered

        elif report_name == "documents":
            all_rows = supabase_repository.list_table("documents", "date_requested")
            period = str(request.args.get("period", "all")).strip()
            types_raw = [t.strip() for t in str(request.args.get("doc_types", "all")).split(",") if t.strip()]
            use_all_types = (not types_raw) or any(t.lower() == "all" for t in types_raw)
            types_lower = {t.lower() for t in types_raw}

            filtered = []
            for d in all_rows:
                if not _matches_report_period(d.get("date_requested") or d.get("created_at"), period):
                    continue
                if not use_all_types:
                    dtype = str(d.get("document_type") or "").strip().lower()
                    if dtype not in types_lower:
                        continue
                row_copy = dict(d)
                row_copy["approved_by"] = d.get("verified_by") or d.get("processor") or "—"
                row_copy["fee_display"] = f"PHP {float(d.get('amount_paid') or 0):.2f}" if d.get("amount_paid") not in (None, "") else "—"
                filtered.append(row_copy)

            title = "Document Issuance Summary"
            subtitle = "Clearances and certificates issued"
            period_lbl = "All Time" if period.lower() == "all" else period
            types_lbl = "All Types" if use_all_types else ", ".join(types_raw)
            filters_summary = f"Period: {period_lbl} · Document Type: {types_lbl}"
            columns = [
                ("request_id", "Request ID"),
                ("resident_name", "Resident Name"),
                ("document_type", "Document Type"),
                ("date_requested", "Date Requested"),
                ("fee_display", "Fee Paid"),
                ("approved_by", "Processed By"),
            ]
            total_fees = sum(
                float(r.get("amount_paid") or 0)
                for r in filtered
                if str(r.get("verification_status") or "").lower() != "rejected"
            )
            summary_cards = [
                {"label": "Total Requests", "value": len(filtered)},
                {"label": "Total Fees", "value": f"PHP {total_fees:,.2f}"},
            ]
            rows = filtered

        elif report_name == "concerns":
            all_rows = supabase_repository.list_table("concerns", "submitted_at", categories=categories)
            month_param = str(request.args.get("month", "all")).strip()
            year_param = str(request.args.get("year", "all")).strip()

            filtered = []
            for c in all_rows:
                dt = _parse_iso_date_prefix(c.get("submitted_at") or c.get("created_at"))
                if year_param and year_param.lower() != "all":
                    if not dt or str(dt.year) != year_param:
                        continue
                if month_param and month_param.lower() != "all":
                    if not dt or str(dt.month) != str(int(month_param)):
                        continue
                row_copy = dict(c)
                desc = str(c.get("description") or "").strip()
                row_copy["description"] = desc if desc else "No additional description provided."
                filtered.append(row_copy)

            month_names = ["", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
            m_lbl = month_names[int(month_param)] if month_param.isdigit() and 1 <= int(month_param) <= 12 else "All Months"
            y_lbl = "All Years" if year_param.lower() == "all" else year_param
            title = "Community Concerns Monitoring"
            subtitle = "Submitted community concerns by month and year"
            filters_summary = f"Month: {m_lbl} · Year: {y_lbl}"
            columns = [
                ("concern_id", "Concern ID"),
                ("title", "Title"),
                ("description", "Description"),
                ("submitted_by", "Submitted By"),
                ("submitted_at", "Date Submitted"),
            ]
            summary_cards = [
                {"label": "Total Concerns", "value": len(filtered)},
            ]
            rows = filtered

        elif report_name == "weather-alerts":
            rows = supabase_repository.list_table("weather_alerts")
            title, subtitle, filters_summary = "Emergency Preparedness Report", "Weather and emergency alerts", "All records"
            columns = [("alert_type", "Type"), ("title", "Title"), ("severity", "Severity"), ("issued_at", "Issued At")]
            summary_cards = [{"label": "Total Alerts", "value": len(rows)}]
        elif report_name == "demographics":
            summary = supabase_repository.dashboard_summary(categories)
            rows = [{"metric": key, "value": value} for key, value in summary["age_groups"].items()]
            title, subtitle, filters_summary = "Demographic Profile Report", "Age demographics summary", "All residents"
            columns = [("metric", "Age Group"), ("value", "Count")]
            summary_cards = [{"label": "Total Residents", "value": summary["total_residents"]}]
        elif report_name == "audit":
            summary = supabase_repository.dashboard_summary(categories)
            rows = [
                {"metric": "Total Residents", "value": summary["total_residents"]},
                {"metric": "Total Documents", "value": summary["documents_total"]},
                {"metric": "Open Concerns", "value": summary["open_concerns"]},
            ]
            title, subtitle, filters_summary = "Governance Audit Summary", "Key operational metrics", "Current snapshot"
            columns = [("metric", "Metric"), ("value", "Value")]
            summary_cards = []
        else:
            return jsonify({"error": "Unknown report"}), 404
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
    except Exception as error:
        app.logger.exception("Could not generate report")
        return jsonify({"error": str(error)}), 502

    if out_format == "json":
        preview_rows = [
            {key: (r.get(key) if r.get(key) not in (None, "") else "—") for key, _ in columns}
            for r in rows
        ]
        return jsonify({
            "data": {
                "report_name": report_name,
                "title": title,
                "subtitle": subtitle,
                "filters_summary": filters_summary,
                "generated_at": manila_now().strftime("%B %d, %Y · %I:%M %p"),
                "total": len(preview_rows),
                "summary_cards": summary_cards,
                "columns": [{"key": k, "label": lbl} for k, lbl in columns],
                "rows": preview_rows,
            }
        })

    if out_format == "pdf":
        pdf_bytes = _build_report_pdf(title, subtitle, filters_summary, summary_cards, columns, rows, prepared_by)
        response = make_response(pdf_bytes)
        response.headers["Content-Type"] = "application/pdf"
        disposition = "inline" if request.args.get("preview") in ("1", "true") else "attachment"
        response.headers["Content-Disposition"] = f'{disposition}; filename="smartbarangay-{report_name}-report.pdf"'
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    # Excel-ready UTF-8 BOM CSV (opens directly in Microsoft Excel with proper columns and characters)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow([title])
    writer.writerow([f"Barangay Don Felipe Larrazabal, Ormoc City | {filters_summary}"])
    writer.writerow([])
    writer.writerow([lbl for _, lbl in columns])
    for r in rows:
        writer.writerow([r.get(key) if r.get(key) not in (None, "") else "" for key, _ in columns])
    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="smartbarangay-{report_name}-report.csv"'
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


DEFAULT_BARANGAY_WEATHER = {
    "current": {
        "temperature_2m": 29.5,
        "relative_humidity_2m": 74,
        "precipitation": 0.0,
        "wind_speed_10m": 10.5,
        "visibility": 40000.0,
        "weather_code": 1,
    },
    "location": "Barangay Don Felipe Larrazabal, Ormoc City",
    "fallback": True,
}


@app.get("/api/weather")
@login_required
def weather():
    default_lat = 11.0050
    default_lon = 124.6075
    try:
        lat_val = request.args.get("lat")
        lon_val = request.args.get("lon")
        latitude = float(lat_val) if lat_val is not None and str(lat_val).strip() else default_lat
        longitude = float(lon_val) if lon_val is not None and str(lon_val).strip() else default_lon
    except (ValueError, TypeError):
        latitude = default_lat
        longitude = default_lon

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,rain,weather_code,wind_speed_10m,wind_gusts_10m,wind_direction_10m,visibility,cloud_cover,is_day",
        "hourly": "temperature_2m,apparent_temperature,precipitation_probability,precipitation,weather_code,wind_speed_10m,uv_index",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,apparent_temperature_max,precipitation_sum,precipitation_probability_max,wind_speed_10m_max,uv_index_max,sunrise,sunset",
        "forecast_days": 7,
        "timezone": "Asia/Manila",
    }
    try:
        with httpx.Client(timeout=6.0) as client:
            resp = client.get("https://api.open-meteo.com/v1/forecast", params=params)
            resp.raise_for_status()
            weather_data = resp.json()
            is_default = (abs(latitude - default_lat) < 0.01 and abs(longitude - default_lon) < 0.01)
            weather_data["location"] = "Barangay Don Felipe Larrazabal, Ormoc City, Leyte" if is_default else "Detected Location"
            weather_data["source"] = "Open-Meteo Live Weather Telemetry (Verify PAGASA)"
            weather_data["source_url"] = "https://bagong.pagasa.dost.gov.ph/weather"
            weather_data["fetched_at"] = manila_now().isoformat()
            try:
                supabase_repository.offline_store.set_meta("last_weather_json", json.dumps(weather_data, default=str))
            except Exception:
                pass
            return jsonify({"data": weather_data})
    except Exception as error:
        cached_raw = supabase_repository.offline_store.get_meta("last_weather_json")
        if cached_raw:
            try:
                cached_wx = json.loads(cached_raw)
                cached_wx["source"] = "Offline Cached Telemetry (Last synchronized weather)"
                cached_wx["offline_cached"] = True
                return jsonify({"data": cached_wx, "offline_cached": True})
            except Exception:
                pass
        fallback = dict(DEFAULT_BARANGAY_WEATHER)
        fallback["fetched_at"] = manila_now().isoformat()
        fallback["source"] = "Fallback Telemetry (Live feed unavailable)"
        fallback["error_note"] = str(error)
        return jsonify({"data": fallback, "fallback": True})


@app.post("/api/weather/ai-assistant")
@login_required
def weather_ai_assistant():
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("message") or "").strip()
    language = str(payload.get("language") or "auto").strip().lower()
    weather_ctx = payload.get("weather") or {}
    advisory_ctx = payload.get("advisory") or {}
    history = payload.get("history") or []

    if not question:
        return jsonify({"error": "Please enter a question"}), 400

    low_q = question.lower()
    if any(w in low_q for w in ("unsa", "pasabot", "kusog", "buhaton", "karon", "ugma", "diri", "salamat", "palihug", "kanus-a", "linog", "kilat", "dalugdog", "pwede", "muulan", "mag-ulan", "mag ulan", "unya", "init", "tugnaw", "bahaan", "laba", "byahe", "dagat", "pila")):
        language = "cebuano"
    elif any(w in low_q for w in ("ano", "ibig sabihin", "gawin", "ligtas", "babala", "ngayon", "bukas", "paano", "kailan", "lindol", "kidlat", "kulog", "uulan", "mamaya", "mainit", "malamig", "klase", "biyahe", "maglaba")):
        language = "filipino"
    elif any(w in low_q for w in ("what", "when", "will", "should", "how", "is it", "can i", "forecast", "tomorrow", "today", "tonight", "heat index", "humidity", "visibility", "typhoon", "rainfall", "umbrella", "safe")):
        language = "english"
    elif language not in ("cebuano", "filipino", "english"):
        language = "english"

    def _num(val, default=0.0):
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    temp_f = _num(weather_ctx.get("temperature_2m"), 0.0)
    feels_f = _num(weather_ctx.get("apparent_temperature"), temp_f)
    rain_24h = _num(weather_ctx.get("rainfall_24h", weather_ctx.get("precipitation", 0)), 0.0)
    wind_f = _num(weather_ctx.get("wind_speed_10m"), 0.0)
    gust_f = _num(weather_ctx.get("wind_gusts_10m"), wind_f)
    wind_deg = _num(weather_ctx.get("wind_direction_10m"), 0.0)
    hum_f = _num(weather_ctx.get("relative_humidity_2m"), 0.0)
    vis_km = weather_ctx.get("visibility_km", "N/A")
    cloud_f = _num(weather_ctx.get("cloud_cover"), 0.0)
    uv_now = _num(weather_ctx.get("uv_index"), 0.0)
    cond = str(weather_ctx.get("condition") or "Current weather")
    next6 = weather_ctx.get("next6") or []
    hourly24 = weather_ctx.get("hourly24") or next6
    daily7 = weather_ctx.get("daily7") or []
    adv_source = advisory_ctx.get("source") or "Open-Meteo / PAGASA"
    updated_at = weather_ctx.get("fetched_at") or manila_now().isoformat()

    compass_dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    wind_dir_label = compass_dirs[int(((wind_deg + 22.5) % 360) // 45)]

    def _heat_category(hi_c: float, lang: str) -> str:
        if hi_c >= 52:
            return {"cebuano": "Extreme Danger (grabe ka peligro sa heat stroke)", "filipino": "Extreme Danger (napakataas na panganib ng heat stroke)"}.get(lang, "Extreme Danger (heat stroke imminent)")
        if hi_c >= 42:
            return {"cebuano": "Danger (posible ang heat cramps ug heat exhaustion)", "filipino": "Danger (posible ang heat cramps at heat exhaustion)"}.get(lang, "Danger (heat cramps/exhaustion likely)")
        if hi_c >= 33:
            return {"cebuano": "Extreme Caution (pag-inom kanunay og tubig)", "filipino": "Extreme Caution (uminom palagi ng tubig)"}.get(lang, "Extreme Caution (stay hydrated & limit midday sun)")
        if hi_c >= 27:
            return {"cebuano": "Caution (medyo init kon magdugay sa gawas)", "filipino": "Caution (bahagyang mainit sa labas)"}.get(lang, "Caution (fatigue possible with prolonged exposure)")
        return {"cebuano": "Komportable / Normal", "filipino": "Komportable / Normal"}.get(lang, "Comfortable / Normal")

    system_instruction = (
        "You are the SmartBarangay AI Weather Assistant for Barangay Don Felipe Larrazabal, Ormoc City, Leyte, Philippines. "
        f"Respond naturally, accurately, and thoroughly in {language.title()} to ANY question the user asks—whether about specific times, "
        "tomorrow/weekly forecasts, heat index, UV, rain/flooding, typhoons, travel, laundry, farming, fishing, class suspensions (DepEd DO 37), "
        "safety preparedness, or general weather science. "
        "Ground all local answers in the LIVE telemetry provided below and cite exact numbers when helpful. "
        "Strict responsibility rules: NEVER fabricate official PAGASA, PHIVOLCS, DENR-MGB, or NDRRMC warnings; never claim AI can predict exact earthquakes or landslides; "
        "always remind users to verify formal warnings directly on official government portals when relevant. "
        "Keep responses clear, structured, and easy to read (3–6 concise sentences or short bullet points).\n\n"
        f"LIVE TELEMETRY (Barangay Don Felipe Larrazabal, Ormoc City, Leyte | Updated {updated_at}):\n"
        f"- Current: {temp_f:.1f}°C (Feels like / Heat Index: {feels_f:.1f}°C), Condition: {cond}, Humidity: {hum_f:.0f}%, "
        f"Wind: {wind_f:.1f} km/h (gusts {gust_f:.1f} km/h from {wind_dir_label}), Cloud cover: {cloud_f:.0f}%, Visibility: {vis_km} km, UV: {uv_now:.1f}, 24h Rainfall: {rain_24h:.1f} mm.\n"
        f"- Next 24h Hourly Forecast: {json.dumps(hourly24[:16])}\n"
        f"- 7-Day Daily Forecast: {json.dumps(daily7[:7])}\n"
    )

    answer = ""
    ai_provider = None

    # 1. Try Gemini API if configured
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if gemini_key and not answer:
        chat_turns = []
        for h in history[-6:]:
            r = "model" if h.get("role") == "bot" else "user"
            txt = str(h.get("text") or "").strip()
            if txt:
                chat_turns.append({"role": r, "parts": [{"text": txt}]})
        chat_turns.append({"role": "user", "parts": [{"text": question}]})
        for model_name in ("gemini-2.5-flash", "gemini-2.0-flash"):
            try:
                with httpx.Client(timeout=8.0) as client:
                    g_resp = client.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
                        params={"key": gemini_key},
                        json={
                            "system_instruction": {"parts": [{"text": system_instruction}]},
                            "contents": chat_turns,
                            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 450},
                        },
                    )
                    if g_resp.is_success:
                        g_json = g_resp.json()
                        cand = (g_json.get("candidates") or [{}])[0]
                        parts = (cand.get("content") or {}).get("parts") or [{}]
                        answer = (parts[0].get("text") or "").strip()
                        if answer:
                            ai_provider = f"Gemini AI + Open-Meteo / PAGASA"
                            break
            except Exception:
                continue

    # 2. Try Groq or OpenAI if configured
    if not answer:
        for env_key, base_url, model_id, label in (
            ("GROQ_API_KEY", "https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile", "Groq AI + Open-Meteo / PAGASA"),
            ("OPENAI_API_KEY", "https://api.openai.com/v1/chat/completions", "gpt-4o-mini", "OpenAI + Open-Meteo / PAGASA"),
        ):
            api_key = os.getenv(env_key, "").strip()
            if not api_key:
                continue
            try:
                msgs = [{"role": "system", "content": system_instruction}]
                for h in history[-6:]:
                    r = "assistant" if h.get("role") == "bot" else "user"
                    txt = str(h.get("text") or "").strip()
                    if txt:
                        msgs.append({"role": r, "content": txt})
                msgs.append({"role": "user", "content": question})
                with httpx.Client(timeout=8.0) as client:
                    o_resp = client.post(
                        base_url,
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={"model": model_id, "messages": msgs, "temperature": 0.4, "max_tokens": 450},
                    )
                    if o_resp.is_success:
                        o_json = o_resp.json()
                        answer = (((o_json.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
                        if answer:
                            ai_provider = label
                            break
            except Exception:
                continue

    # 3. Deep Data-Driven Local Intelligence Engine (accurate live hourly/daily reasoning even without external LLM key)
    if not answer:
        peak_rain_hr = max(hourly24[:12], key=lambda h: (_num(h.get("rain"), 0), _num(h.get("prob"), 0)), default=None)
        max_prob_12h = max((_num(h.get("prob"), 0) for h in hourly24[:12]), default=0)
        total_rain_12h = sum(_num(h.get("rain"), 0) for h in hourly24[:12])
        today_daily = daily7[0] if len(daily7) > 0 else {}
        tomorrow_daily = daily7[1] if len(daily7) > 1 else {}

        # Check specific clock time in question (e.g., "3pm", "alas 4", "15:00")
        time_match = re.search(r"\b(?:alas\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|sa\s+buntag|sa\s+hapon|sa\s+gabii|ng\s+umaga|ng\s+hapon|ng\s+gabi)?\b", low_q)
        matched_hour_entry = None
        if time_match and any(k in low_q for k in ("am", "pm", "alas", ":", "hapon", "gabii", "gabi", "buntag", "umaga", "time", "oras")):
            hr_num = int(time_match.group(1))
            meridiem = (time_match.group(3) or "").strip()
            if 1 <= hr_num <= 23:
                if meridiem in ("pm", "sa hapon", "sa gabii", "ng hapon", "ng gabi") and hr_num < 12:
                    hr_num += 12
                elif meridiem in ("am", "sa buntag", "ng umaga") and hr_num == 12:
                    hr_num = 0
                for h in hourly24:
                    iso = str(h.get("iso") or "")
                    if len(iso) >= 13 and iso[11:13].isdigit() and int(iso[11:13]) == hr_num:
                        matched_hour_entry = h
                        break

        if matched_hour_entry:
            hl = matched_hour_entry.get("label", "")
            ht = matched_hour_entry.get("temp", f"{temp_f:.1f}°C")
            hp = int(_num(matched_hour_entry.get("prob"), 0))
            hr = _num(matched_hour_entry.get("rain"), 0)
            hw = _num(matched_hour_entry.get("wind"), wind_f)
            if language == "cebuano":
                answer = (
                    f"Sa alas {hl} sa Barangay Don Felipe Larrazabal:\n"
                    f"• Temperatura: {ht} · Tsansa sa ulan: {hp}% ({hr:.1f} mm) · Hangin: {hw:.1f} km/h.\n"
                    f"{'Posible ang ulan anang orasa—pagdala og payong o raincoat.' if hp >= 50 or hr >= 0.5 else 'Ubos ang tsansa sa ulan anang orasa, pero bantayi gihapon ang kalit nga pagbag-o sa panahon.'}"
                )
            elif language == "filipino":
                answer = (
                    f"Pagdating ng {hl} sa Barangay Don Felipe Larrazabal:\n"
                    f"• Temperatura: {ht} · Tsansa ng ulan: {hp}% ({hr:.1f} mm) · Hangin: {hw:.1f} km/h.\n"
                    f"{'May mataas na tsansang umulan sa oras na iyan—magdala ng payong o kapote.' if hp >= 50 or hr >= 0.5 else 'Mababa ang tsansa ng ulan sa oras na iyan, ngunit manatiling handa sa biglaang pagbabago ng panahon.'}"
                )
            else:
                answer = (
                    f"Forecast for {hl} in Barangay Don Felipe Larrazabal:\n"
                    f"• Temperature: {ht} · Rain chance: {hp}% ({hr:.1f} mm) · Wind: {hw:.1f} km/h.\n"
                    f"{'Rain is likely around that hour—bring an umbrella or rain gear.' if hp >= 50 or hr >= 0.5 else 'Rain risk is relatively low at that hour, though brief local showers can still form.'}"
                )
        elif any(k in low_q for k in ("tomorrow", "ugma", "bukas", "next week", "7 day", "7-day", "semana", "weekend", "sabado", "dominggo", "linggo")):
            if any(k in low_q for k in ("7 day", "7-day", "week", "semana", "weekend", "sabado", "dominggo", "linggo")) and len(daily7) >= 3:
                lines = [
                    f"• {d.get('day')}: {d.get('min')}–{d.get('max')}°C, rain {d.get('prob')}% ({d.get('rain')} mm), wind {d.get('wind')} km/h"
                    for d in daily7[:5]
                ]
                header = {"cebuano": "5-Day Outlook sa Ormoc City:", "filipino": "5-Day Outlook sa Ormoc City:"}.get(language, "5-Day Weather Outlook for Ormoc City:")
                answer = header + "\n" + "\n".join(lines)
            elif tomorrow_daily:
                if language == "cebuano":
                    answer = (
                        f"Forecast ugma ({tomorrow_daily.get('day')}) sa Barangay Don Felipe Larrazabal:\n"
                        f"• Temperatura: {tomorrow_daily.get('min')}°C hangtod {tomorrow_daily.get('max')}°C (Heat Index: {tomorrow_daily.get('feels_max')}°C)\n"
                        f"• Tsansa sa ulan: {tomorrow_daily.get('prob')}% ({tomorrow_daily.get('rain')} mm) · Max Hangin: {tomorrow_daily.get('wind')} km/h · UV: {tomorrow_daily.get('uv')}."
                    )
                elif language == "filipino":
                    answer = (
                        f"Forecast bukas ({tomorrow_daily.get('day')}) sa Barangay Don Felipe Larrazabal:\n"
                        f"• Temperatura: {tomorrow_daily.get('min')}°C hanggang {tomorrow_daily.get('max')}°C (Heat Index: {tomorrow_daily.get('feels_max')}°C)\n"
                        f"• Tsansa ng ulan: {tomorrow_daily.get('prob')}% ({tomorrow_daily.get('rain')} mm) · Max Hangin: {tomorrow_daily.get('wind')} km/h · UV: {tomorrow_daily.get('uv')}."
                    )
                else:
                    answer = (
                        f"Tomorrow's outlook ({tomorrow_daily.get('day')}) for Barangay Don Felipe Larrazabal:\n"
                        f"• Temperature: {tomorrow_daily.get('min')}°C to {tomorrow_daily.get('max')}°C (Peak Heat Index: {tomorrow_daily.get('feels_max')}°C)\n"
                        f"• Rain chance: {tomorrow_daily.get('prob')}% ({tomorrow_daily.get('rain')} mm) · Max Wind: {tomorrow_daily.get('wind')} km/h · Max UV: {tomorrow_daily.get('uv')}."
                    )
            else:
                answer = f"Current reading: {temp_f:.1f}°C ({cond}), 24h rain {rain_24h:.1f} mm."
        elif any(k in low_q for k in ("laba", "laundry", "sampay", "dry clothes", "manglaba", "maglaba")):
            good_dry = max_prob_12h < 50 and total_rain_12h < 1.0
            peak_txt = f"{peak_rain_hr.get('label')} ({int(_num(peak_rain_hr.get('prob')))}%)" if peak_rain_hr else "later"
            if language == "cebuano":
                answer = (
                    f"{'Oo, maayo pa maglaba ug magbulad karon!' if good_dry else 'Pag-amping sa pagbulad sa gawas karon.'} "
                    f"Karon {temp_f:.1f}°C ({cond}), humidity {hum_f:.0f}%, ug ang kinatas-ang tsansa sa ulan sa sunod 12 ka oras kay {int(max_prob_12h)}% (bandang {peak_txt}). "
                    f"{' dali ra mauga ang sinina.' if good_dry else 'Mas maayo nga adunay silong o bantayan ang sinina inig ' + peak_txt + '.'}"
                )
            elif language == "filipino":
                answer = (
                    f"{'Oo, maganda ang panahon para maglaba at magsampay ngayon!' if good_dry else 'Mag-ingat sa pagsasampay sa labas ngayon.'} "
                    f"Kasalukuyang {temp_f:.1f}°C ({cond}), humidity {hum_f:.0f}%, at pinakamataas na tsansa ng ulan sa susunod na 12 oras ay {int(max_prob_12h)}% (bandang {peak_txt})."
                )
            else:
                answer = (
                    f"{'Yes, conditions are good for doing laundry and line-drying right now.' if good_dry else 'Caution with outdoor drying today.'} "
                    f"It is currently {temp_f:.1f}°C ({cond}) with {hum_f:.0f}% humidity, and peak rain probability over the next 12 hours is {int(max_prob_12h)}% around {peak_txt}."
                )
        elif any(k in low_q for k in ("byahe", "biyahe", "travel", "motor", "drive", "commute", "boat", "dagat", "fishing", "fish", "pangisda", "lakaw", "jog", "outdoor", "picnic")):
            is_sea = any(k in low_q for k in ("boat", "dagat", "fishing", "fish", "pangisda", "sea"))
            wind_risk = gust_f >= 35 or wind_f >= 25
            rain_risk = max_prob_12h >= 65 or total_rain_12h >= 5.0
            if language == "cebuano":
                if is_sea:
                    answer = f"Para sa paglawig/pangisda: Hangin karon {wind_f:.1f} km/h (gusts {gust_f:.1f} km/h gikan sa {wind_dir_label}), visibility {vis_km} km, max tsansa sa ulan {int(max_prob_12h)}%. {'Medyo kusog ang hangin—susiha una ang PAGASA Gale Warning ug PCG advisory sa Ormoc Bay.' if wind_risk else 'Kasarangan ang hangin karon, pero susiha gihapon ang opisyal nga Coast Guard/PAGASA marine advisory sa dili pa molawig.'}"
                else:
                    answer = f"Para sa byahe/gawas sa balay: {temp_f:.1f}°C ({cond}), visibility {vis_km} km, hangin {wind_f:.1f} km/h, pinakataas nga tsansa sa ulan {int(max_prob_12h)}%. {'Pagdala og raincoat/payong ug paghinay sa danlog nga dalan.' if rain_risk else 'Maayo ang kahimtang sa dalan karon; pagdala lang og tubig kon init.'}"
            elif language == "filipino":
                if is_sea:
                    answer = f"Para sa paglalayag/pangingisda: Hangin ngayon {wind_f:.1f} km/h (gusts {gust_f:.1f} km/h mula {wind_dir_label}), visibility {vis_km} km. {'Malakas ang bugso ng hangin—suriin muna ang PAGASA Gale Warning at PCG advisory.' if wind_risk else 'Katamtaman ang hangin ngayon, ngunit tiyakin pa rin ang abiso ng Coast Guard at PAGASA bago pumalaot.'}"
                else:
                    answer = f"Para sa biyahe/paglabas: {temp_f:.1f}°C ({cond}), visibility {vis_km} km, hangin {wind_f:.1f} km/h, max tsansa ng ulan {int(max_prob_12h)}%. {'Magdala ng kapote/payong at mag-ingat sa madulas na kalsada.' if rain_risk else 'Maayos ang panahon para bumiyahe ngayon; uminom ng tubig kung mainit.'}"
            else:
                if is_sea:
                    answer = f"Marine/fishing outlook: Surface wind is {wind_f:.1f} km/h (gusts {gust_f:.1f} km/h from {wind_dir_label}), visibility {vis_km} km, 12h peak rain chance {int(max_prob_12h)}%. {'Winds are gusty—verify PAGASA Gale Warnings and Philippine Coast Guard advisories before heading out.' if wind_risk else 'Winds are moderate right now, though small craft should always check PCG & PAGASA marine bulletins first.'}"
                else:
                    answer = f"Travel/outdoor outlook: Currently {temp_f:.1f}°C ({cond}), visibility {vis_km} km, wind {wind_f:.1f} km/h, peak 12h rain chance {int(max_prob_12h)}%. {'Carry rain gear and allow extra travel time for wet roads.' if rain_risk else 'Generally favorable conditions—stay hydrated if outdoors during midday heat.'}"
        elif any(k in low_q for k in ("klase", "class", "school", "suspend", "suspension", "deped", "eskwela")):
            if language == "cebuano":
                answer = (
                    f"Base sa DepEd Order No. 37, s. 2022: Awtomatiko ang suspension sa klase kon adunay opisyal nga PAGASA Tropical Cyclone Wind Signal (#1: Kindergarten; #2: K–Grade 10; #3+: tanang lebel) o Orange/Red Rainfall Warning, o kon ideklara sa LGU Ormoc City tungod sa taas nga Heat Index (≥42°C). "
                    f"Karon sa Ormoc City: {temp_f:.1f}°C (Heat Index {feels_f:.1f}°C), hangin {wind_f:.1f} km/h. Bantayi ang opisyal nga anunsyo sa Ormoc City LGU ug DepEd."
                )
            elif language == "filipino":
                answer = (
                    f"Batay sa DepEd Order No. 37, s. 2022: Awtomatiko ang class suspension kapag may opisyal na PAGASA Wind Signal (#1: Kinder; #2: K–Grade 10; #3+: lahat ng antas) o Orange/Red Rainfall Warning, o kapag idineklara ng LGU Ormoc City dahil sa mataas na Heat Index (≥42°C). "
                    f"Ngayon: {temp_f:.1f}°C (Heat Index {feels_f:.1f}°C), hangin {wind_f:.1f} km/h. Laging sundin ang opisyal na abiso ng LGU at DepEd."
                )
            else:
                answer = (
                    f"Under DepEd Order No. 37, s. 2022, automatic class suspensions follow official PAGASA Wind Signals (TCWS #1: Kindergarten; #2: K–Grade 10; #3+: all levels) or Orange/Red Rainfall Warnings, while heat-related shifts/suspensions (typically ≥42°C Heat Index) are decided by Ormoc City LGU/DepEd. "
                    f"Current reading: {temp_f:.1f}°C (Heat Index {feels_f:.1f}°C), wind {wind_f:.1f} km/h. Always confirm with official Ormoc City LGU announcements."
                )
        elif any(k in low_q for k in ("heat", "init", "hot", "feels like", "uv", "sun", "tugnaw", "cold", "temp", "pila ka degrees", "sunrise", "sunset")):
            hi_cat = _heat_category(feels_f, language)
            sr = str(today_daily.get("sunrise") or "")[-5:] or "—"
            ss = str(today_daily.get("sunset") or "")[-5:] or "—"
            if language == "cebuano":
                answer = (
                    f"Temperatura ug Heat Index karon sa Barangay Don Felipe Larrazabal:\n"
                    f"• Aktwal nga temperatura: {temp_f:.1f}°C · Gibati nga kainit (Heat Index): {feels_f:.1f}°C — {hi_cat}\n"
                    f"• UV Index: {uv_now:.1f} · Humidity: {hum_f:.0f}% · Sunrise: {sr} / Sunset: {ss}.\n"
                    f"{'Likayi ang dugay nga pagpainit gikan alas 10 AM hangtod 3 PM ug pag-inom og daghang tubig.' if feels_f >= 33 else 'Kasarangan ang temperatura karon.'}"
                )
            elif language == "filipino":
                answer = (
                    f"Temperatura at Heat Index ngayon sa Barangay Don Felipe Larrazabal:\n"
                    f"• Aktwal na temperatura: {temp_f:.1f}°C · Heat Index (Feels Like): {feels_f:.1f}°C — {hi_cat}\n"
                    f"• UV Index: {uv_now:.1f} · Humidity: {hum_f:.0f}% · Sunrise: {sr} / Sunset: {ss}.\n"
                    f"{'Iwasan ang matagal na pagbibilad mula 10 AM hanggang 3 PM at uminom ng maraming tubig.' if feels_f >= 33 else 'Katamtaman ang temperatura ngayon.'}"
                )
            else:
                answer = (
                    f"Temperature & Heat Index in Barangay Don Felipe Larrazabal:\n"
                    f"• Air Temperature: {temp_f:.1f}°C · Feels Like (Heat Index): {feels_f:.1f}°C — {hi_cat}\n"
                    f"• UV Index: {uv_now:.1f} · Humidity: {hum_f:.0f}% · Sunrise: {sr} / Sunset: {ss}.\n"
                    f"{'Limit prolonged direct sun exposure between 10 AM and 3 PM and stay well hydrated.' if feels_f >= 33 else 'Thermal conditions are currently moderate.'}"
                )
        elif any(k in low_q for k in ("heavy rain", "rainfall", "yellow", "orange", "red", "baha", "flood")):
            peak_txt = f"{peak_rain_hr.get('label')} ({int(_num(peak_rain_hr.get('prob')))}%, {_num(peak_rain_hr.get('rain')):.1f} mm)" if peak_rain_hr else "none expected"
            if language == "cebuano":
                answer = (
                    f"PAGASA Rainfall Warning Guide:\n"
                    f"• Yellow (7.5–15 mm/hr): Posible ang baha sa ubos nga lugar.\n"
                    f"• Orange (15–30 mm/hr): Peligro sa baha, pag-andam sa evacuation.\n"
                    f"• Red (>30 mm/hr): Grabe nga baha, pag-evacuate dayon.\n"
                    f"Live data karon: 24h ulan {rain_24h:.1f} mm, peak rain window sa sunod 12h kay {peak_txt}."
                )
            elif language == "filipino":
                answer = (
                    f"PAGASA Rainfall Warning Guide:\n"
                    f"• Yellow (7.5–15 mm/hr): Posibleng pagbaha sa mabababang lugar.\n"
                    f"• Orange (15–30 mm/hr): Nagbabantang pagbaha, maghanda sa paglikas.\n"
                    f"• Red (>30 mm/hr): Matinding pagbaha, lumikas agad.\n"
                    f"Live data ngayon: 24h ulan {rain_24h:.1f} mm, pinakamataas na tsansa ng ulan sa 12h: {peak_txt}."
                )
            else:
                answer = (
                    f"PAGASA Rainfall Warning Levels:\n"
                    f"• Yellow (7.5–15 mm/hr): Flooding possible in low-lying areas.\n"
                    f"• Orange (15–30 mm/hr): Flooding threatening; prepare to evacuate.\n"
                    f"• Red (>30 mm/hr): Serious flooding expected; evacuate immediately.\n"
                    f"Live Ormoc reading: 24h rainfall {rain_24h:.1f} mm; highest 12h rain window is {peak_txt}."
                )
        elif any(k in low_q for k in ("ulan", "uwan", "rain", "umbrella", "payong", "forecast", "later", "tonight", "afternoon", "hapon", "gabii", "gabi", "buntag", "morning", "unya", "mamaya", "next", "hour")):
            summary_6h = " · ".join(f"{h.get('label')}: {h.get('temp')} ({int(_num(h.get('prob')))}%)" for h in next6[:6]) if next6 else f"{temp_f:.1f}°C"
            peak_txt = f"{peak_rain_hr.get('label')} ({int(_num(peak_rain_hr.get('prob')))}%)" if peak_rain_hr else "—"
            if language == "cebuano":
                answer = (
                    f"Forecast sa sunod nga 6 ka oras sa Barangay Don Felipe Larrazabal:\n{summary_6h}\n"
                    f"• Pinakataas nga tsansa sa ulan (12h): {int(max_prob_12h)}% bandang {peak_txt} · 24h total: {rain_24h:.1f} mm.\n"
                    f"{'Mas maayo magdala og payong kon mogawas.' if max_prob_12h >= 45 else 'Ubos ang tsansa sa kusog nga ulan sa sunod nga mga oras.'}"
                )
            elif language == "filipino":
                answer = (
                    f"Susunod na 6-hour forecast sa Barangay Don Felipe Larrazabal:\n{summary_6h}\n"
                    f"• Pinakamataas na tsansa ng ulan (12h): {int(max_prob_12h)}% bandang {peak_txt} · 24h total: {rain_24h:.1f} mm.\n"
                    f"{'Magdala ng payong kung lalabas.' if max_prob_12h >= 45 else 'Mababa ang tsansa ng malakas na ulan sa susunod na mga oras.'}"
                )
            else:
                answer = (
                    f"Next 6-hour outlook for Barangay Don Felipe Larrazabal:\n{summary_6h}\n"
                    f"• Peak 12h rain probability: {int(max_prob_12h)}% around {peak_txt} · 24h rainfall total: {rain_24h:.1f} mm.\n"
                    f"{'Bringing an umbrella is recommended if heading out.' if max_prob_12h >= 45 else 'Heavy rain risk is low over the next few hours.'}"
                )
        elif any(k in low_q for k in ("typhoon", "bagyo", "signal", "tcws", "wind", "hangin", "storm", "gust")):
            if language == "cebuano":
                answer = (
                    f"Kahimtang sa hangin karon sa Barangay Don Felipe Larrazabal: {wind_f:.1f} km/h (gusts hangtod {gust_f:.1f} km/h gikan sa {wind_dir_label}).\n"
                    f"PAGASA Wind Signals: #1 (39–61 km/h), #2 (62–88 km/h), #3 (89–117 km/h), #4 (118–184 km/h), #5 (≥185 km/h).\n"
                    "Susiha kanunay ang opisyal nga PAGASA Severe Weather Bulletin para sa eksaktong track sa bagyo."
                )
            elif language == "filipino":
                answer = (
                    f"Lakas ng hangin ngayon sa Barangay Don Felipe Larrazabal: {wind_f:.1f} km/h (bugso hanggang {gust_f:.1f} km/h mula {wind_dir_label}).\n"
                    f"PAGASA Wind Signals: #1 (39–61 km/h), #2 (62–88 km/h), #3 (89–117 km/h), #4 (118–184 km/h), #5 (≥185 km/h).\n"
                    "Suriin ang opisyal na PAGASA Severe Weather Bulletin para sa aktwal na track ng bagyo."
                )
            else:
                answer = (
                    f"Current wind in Barangay Don Felipe Larrazabal: {wind_f:.1f} km/h (gusts up to {gust_f:.1f} km/h from {wind_dir_label}).\n"
                    f"PAGASA TCWS thresholds: #1 (39–61), #2 (62–88), #3 (89–117), #4 (118–184), #5 (≥185 km/h).\n"
                    "Verify official PAGASA Tropical Cyclone Bulletins for active storm tracks."
                )
        elif any(k in low_q for k in ("humidity", "alisngaw", "visibility", "fog", "cloud", "panganod")):
            if language == "cebuano":
                answer = f"Live atmospheric readings sa Ormoc City: Humidity {hum_f:.0f}%, Visibility {vis_km} km, Cloud Cover {cloud_f:.0f}%, Hangin {wind_f:.1f} km/h ({wind_dir_label}). {'Medyo alimuot tungod sa taas nga humidity.' if hum_f >= 75 else 'Komportable ang lebel sa humidity karon.'}"
            elif language == "filipino":
                answer = f"Live atmospheric readings sa Ormoc City: Humidity {hum_f:.0f}%, Visibility {vis_km} km, Cloud Cover {cloud_f:.0f}%, Hangin {wind_f:.1f} km/h ({wind_dir_label}). {'Medyo maalinsangan dahil sa mataas na humidity.' if hum_f >= 75 else 'Komportable ang antas ng humidity ngayon.'}"
            else:
                answer = f"Live atmospheric readings for Ormoc City: Humidity {hum_f:.0f}%, Visibility {vis_km} km, Cloud Cover {cloud_f:.0f}%, Wind {wind_f:.1f} km/h ({wind_dir_label}). {'Air may feel muggy due to elevated humidity.' if hum_f >= 75 else 'Humidity and visibility are currently comfortable.'}"
        elif any(k in low_q for k in ("thunder", "lightning", "kidlat", "kilat", "dalugdog", "kulog")):
            if language == "cebuano":
                answer = "Safety sa Kilat ug Dalugdog: Sulod dayon sa lig-on nga balay, i-unplug ang sensitibong appliances, likayi ang mga punoan sa kahoy, poste, ug open water, ug paghulat og 30 minutos human ang kataposang dalugdog sa dili pa mogawas."
            elif language == "filipino":
                answer = "Kaligtasan sa Kidlat at Kulog: Pumasok agad sa matibay na gusali, i-unplug ang appliances, umiwas sa matataas na puno at open water, at maghintay ng 30 minuto pagkatapos ng huling kulog bago lumabas."
            else:
                answer = "Thunderstorm & Lightning Safety: Move indoors immediately, unplug sensitive electronics, avoid tall trees, metal poles, and open water, and wait 30 minutes after the last thunder before resuming outdoor activity."
        elif any(k in low_q for k in ("landslide", "earthquake", "linog", "lindol", "tsunami", "volcan")):
            if language == "cebuano":
                answer = "Responsableng Pahinumdom: Ang AI dili makatagna sa eksaktong linog o landslide. Kon maglinog, buhata ang Duck, Cover, and Hold ug palayo sa mga bildo o taas nga estruktura. Para sa opisyal nga updates, susiha ang PHIVOLCS ug DENR-MGB."
            elif language == "filipino":
                answer = "Responsableng Paalala: Hindi kayang hulaan ng AI ang eksaktong lindol o landslide. Kapag lumindol, gawin ang Duck, Cover, and Hold at lumayo sa mabibigat na bagay. Suriin ang opisyal na PHIVOLCS at DENR-MGB updates."
            else:
                answer = "Responsible Notice: AI cannot predict exact earthquakes or landslides. During shaking, perform Duck, Cover, and Hold and move away from falling hazards. Check official PHIVOLCS and DENR-MGB bulletins for verified seismic and slope alerts."
        elif any(k in low_q for k in ("safety", "tip", "tips", "prepare", "buhaton", "gawin", "evacuate", "bakwit", "kit", "go bag", "emergency", "hotline")):
            if language == "cebuano":
                answer = (
                    f"Mga Safety Tips karon ({cond}, {temp_f:.1f}°C, Heat Index {feels_f:.1f}°C):\n"
                    "1) I-andam ang Emergency Go-Bag (tubig, flashlight, first aid, tambal, importanteng IDs, powerbank).\n"
                    "2) Ayaw paglabang sa bahaan nga dalan o kusog nga sapa; paminaw sa BDRRMC Barangay Don Felipe Larrazabal.\n"
                    "3) Kon init kaayo, pag-inom og daghang tubig ug likayi ang dugay nga pagpainit."
                )
            elif language == "filipino":
                answer = (
                    f"Mga Safety Tips ngayon ({cond}, {temp_f:.1f}°C, Heat Index {feels_f:.1f}°C):\n"
                    "1) Ihanda ang Emergency Go-Bag (tubig, flashlight, first aid, gamot, mahahalagang ID, powerbank).\n"
                    "2) Huwag tumawid sa binabahang kalsada o rumaragasang ilog; makinig sa BDRRMC Barangay Don Felipe Larrazabal.\n"
                    "3) Kapag mainit, uminom ng sapat na tubig at iwasan ang matagal na pagbibilad."
                )
            else:
                answer = (
                    f"Preparedness & Safety Tips ({cond}, {temp_f:.1f}°C, Heat Index {feels_f:.1f}°C):\n"
                    "1) Keep an Emergency Go-Bag ready (water, flashlight, first aid kit, medicines, IDs, power bank).\n"
                    "2) Never cross flooded roads or swollen waterways; coordinate with BDRRMC Barangay Don Felipe Larrazabal.\n"
                    "3) Hydrate regularly during peak heat hours and verify severe alerts with PAGASA."
                )
        elif any(k in low_q for k in ("hi", "hello", "hey", "kumusta", "maayong", "help", "tabang", "tulong")) and len(low_q) < 35:
            if language == "cebuano":
                answer = f"Maayong adlaw! Live sa Barangay Don Felipe Larrazabal karon: {temp_f:.1f}°C ({cond}, Heat Index {feels_f:.1f}°C). Pwede ka mangutana bisan unsa bahin sa panahon—sama sa oras sa ulan (e.g. 'muulan ba alas 3?'), forecast ugma, paglaba, byahe, klase, bagyo, o safety tips!"
            elif language == "filipino":
                answer = f"Magandang araw! Live sa Barangay Don Felipe Larrazabal ngayon: {temp_f:.1f}°C ({cond}, Heat Index {feels_f:.1f}°C). Maaari kang magtanong kahit ano tungkol sa panahon—oras ng ulan (hal. 'uulan ba alas 3?'), bukas/7-day forecast, paglalaba, biyahe, klase, bagyo, o safety tips!"
            else:
                answer = f"Hello! Live in Barangay Don Felipe Larrazabal: {temp_f:.1f}°C ({cond}, feels like {feels_f:.1f}°C). Ask me anything about the weather—exact hours ('will it rain at 3 PM?'), tomorrow or 5-day outlook, heat index, laundry, travel/fishing, class suspensions, typhoons, or safety tips!"
        else:
            hi_cat = _heat_category(feels_f, language)
            peak_txt = f"{peak_rain_hr.get('label')} ({int(_num(peak_rain_hr.get('prob')))}%)" if peak_rain_hr else "low risk"
            tmr_txt = f"{tomorrow_daily.get('min')}–{tomorrow_daily.get('max')}°C, {tomorrow_daily.get('prob')}% rain" if tomorrow_daily else "N/A"
            if language == "cebuano":
                answer = (
                    f"Base sa live nga weather telemetry sa Barangay Don Felipe Larrazabal, Ormoc City:\n"
                    f"• Karon: {temp_f:.1f}°C ({cond}), Heat Index {feels_f:.1f}°C ({hi_cat})\n"
                    f"• Hangin: {wind_f:.1f} km/h ({wind_dir_label}) · Humidity: {hum_f:.0f}% · Visibility: {vis_km} km\n"
                    f"• Ulan (24h): {rain_24h:.1f} mm (peak 12h chance: {peak_txt}) · Ugma: {tmr_txt}.\n"
                    "Pwede pud ko nimo pangutan-on og espesipikong oras (e.g. 'alas 4'), ugma/semana, paglaba, byahe, klase, o safety tips!"
                )
            elif language == "filipino":
                answer = (
                    f"Batay sa live weather telemetry sa Barangay Don Felipe Larrazabal, Ormoc City:\n"
                    f"• Ngayon: {temp_f:.1f}°C ({cond}), Heat Index {feels_f:.1f}°C ({hi_cat})\n"
                    f"• Hangin: {wind_f:.1f} km/h ({wind_dir_label}) · Humidity: {hum_f:.0f}% · Visibility: {vis_km} km\n"
                    f"• Ulan (24h): {rain_24h:.1f} mm (peak 12h chance: {peak_txt}) · Bukas: {tmr_txt}.\n"
                    "Maaari mo rin akong tanungin ng eksaktong oras (hal. 'alas 4'), bukas/linggo, paglalaba, biyahe, klase, o safety tips!"
                )
            else:
                answer = (
                    f"Live weather analysis for Barangay Don Felipe Larrazabal, Ormoc City:\n"
                    f"• Now: {temp_f:.1f}°C ({cond}), Heat Index {feels_f:.1f}°C ({hi_cat})\n"
                    f"• Wind: {wind_f:.1f} km/h ({wind_dir_label}) · Humidity: {hum_f:.0f}% · Visibility: {vis_km} km\n"
                    f"• 24h Rain: {rain_24h:.1f} mm (peak 12h chance: {peak_txt}) · Tomorrow: {tmr_txt}.\n"
                    "You can also ask about a specific hour (e.g. '3 PM'), tomorrow or 5-day forecast, laundry, travel/sea conditions, class suspensions, or emergency safety."
                )

    return jsonify(
        {
            "data": {
                "reply": answer,
                "language": language,
                "source": ai_provider or adv_source,
                "source_url": "https://bagong.pagasa.dost.gov.ph/weather",
                "last_updated": updated_at,
                "disclaimer": "AI-assisted explanation only — not an official PAGASA warning.",
            }
        }
    )


def _resolve_household_for_resident(
    full_name: str,
    raw_household: str | None,
    is_head: bool,
    current_id: int | None = None,
) -> str:
    cleaned_hh = (raw_household or "").strip()
    if cleaned_hh and cleaned_hh != "UNASSIGNED":
        return cleaned_hh

    parts = [p for p in (full_name or "").strip().split() if p]
    if len(parts) < 2 and not is_head:
        return "UNASSIGNED"

    surname = parts[-1].lower() if parts else ""
    try:
        all_residents = supabase_repository.list_residents()
    except Exception:
        all_residents = []

    if not is_head and len(parts) >= 2:
        for r in all_residents:
            if current_id is not None and r.get("id") == current_id:
                continue
            r_hh = (r.get("household") or "").strip()
            if not r_hh or r_hh == "UNASSIGNED":
                continue
            r_parts = [p for p in str(r.get("full_name") or "").strip().split() if p]
            if r_parts and r_parts[-1].lower() == surname:
                return r_hh

    existing_hhs = {
        str(r.get("household") or "").strip()
        for r in all_residents
        if r.get("household")
    }
    next_num = 100 + (current_id or (len(all_residents) + 1))
    while f"HH-{next_num:04d}" in existing_hhs:
        next_num += 1
    return f"HH-{next_num:04d}"


def _build_resident_sector_string(
    *,
    is_head: bool,
    is_pwd: bool,
    is_senior: bool,
    is_lgbt: bool,
    is_solo_parent: bool,
    is_pregnant: bool,
    is_young_parent: bool,
    is_4ps: bool,
    age: int,
) -> str:
    sectors: list[str] = []
    if is_head:
        sectors.append("Leader")
    if is_pwd:
        sectors.append("PWD")
    if is_senior or age >= 60:
        sectors.append("Senior Citizen")
    if is_lgbt:
        sectors.append("LGBT")
    if is_solo_parent:
        sectors.append("Solo Parent")
    if is_pregnant:
        sectors.append("Pregnant")
    if is_young_parent:
        sectors.append("Young Parent")
    if is_4ps:
        sectors.append("4Ps")
    if age < 18:
        sectors.append("Minor")
    elif age < 60:
        sectors.append("Adult")
    return ", ".join(sectors)


@app.post("/api/residents")
@module_required("Residents")
def create_resident():
    payload = request.get_json(silent=True) or {}
    if "sex" in payload and not payload.get("gender"):
        payload["gender"] = payload["sex"]

    date_of_birth = str(payload.get("date_of_birth") or "").strip() or None
    if date_of_birth and not str(payload.get("age") or "").strip():
        try:
            dob = date.fromisoformat(date_of_birth)
            today = manila_today()
            calc_age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
            payload["age"] = max(1, calc_age)
        except ValueError:
            pass

    required_fields = ("full_name", "address", "age", "gender")
    missing_fields = [field for field in required_fields if not str(payload.get(field, "")).strip()]
    if missing_fields:
        return jsonify({"error": f"Missing fields: {', '.join(missing_fields)}"}), 400
    try:
        age = int(payload["age"])
        gender = str(payload["gender"]).strip()
        if age < 0 or age > 120:
            return jsonify({"error": "Age must be between 0 and 120"}), 400
        if age == 0:
            age = 1
        if gender not in {"Female", "Male", "Other"}:
            return jsonify({"error": "Choose a valid sex / gender"}), 400
        is_head = bool(payload.get("is_head", False))
        household_role = str(payload.get("household_role", "Head" if is_head else "Member")).strip() or ("Head" if is_head else "Member")
        if household_role.lower() in ("head", "head of the family", "household head", "household leader", "leader"):
            is_head = True
            household_role = "Head"
        is_pwd = bool(payload.get("is_pwd", False))
        is_senior = bool(payload.get("is_senior", False)) or (age >= 60)
        is_lgbt = bool(payload.get("is_lgbt", False))
        is_solo_parent = bool(payload.get("is_solo_parent", False))
        is_pregnant = bool(payload.get("is_pregnant", False))
        is_young_parent = bool(payload.get("is_young_parent", False))
        is_4ps = bool(payload.get("is_4ps", False))
        sector = str(payload.get("sector", "")).strip() or None
        if not sector:
            sector = _build_resident_sector_string(
                is_head=is_head,
                is_pwd=is_pwd,
                is_senior=is_senior,
                is_lgbt=is_lgbt,
                is_solo_parent=is_solo_parent,
                is_pregnant=is_pregnant,
                is_young_parent=is_young_parent,
                is_4ps=is_4ps,
                age=age,
            )

        full_name_clean = str(payload["full_name"]).strip()
        raw_hh = str(payload.get("household", "")).strip()
        resolved_hh = _resolve_household_for_resident(full_name_clean, raw_hh, is_head)
        family_no = str(payload.get("family_no") or "").strip() or None
        marital_status = str(payload.get("marital_status") or "").strip() or None
        raw_years_living_in = payload.get("years_living_in")
        years_living_in = None
        if raw_years_living_in not in (None, ""):
            try:
                years_living_in = max(0, int(raw_years_living_in))
            except (ValueError, TypeError):
                years_living_in = None
        family_planning = str(payload.get("family_planning") or "").strip() or None
        educational_attainment = str(payload.get("educational_attainment") or "").strip() or None
        residence_type = str(payload.get("residence_type") or "").strip() or None

        req_status = str(payload.get("status", "Pending")).strip()
        if req_status not in {"Verified", "Pending", "Inactive"}:
            req_status = "Pending"

        resident = supabase_repository.add_resident({
            "full_name": full_name_clean,
            "address": str(payload["address"]).strip(),
            "household": resolved_hh,
            "family_no": family_no,
            "household_role": household_role,
            "date_of_birth": date_of_birth,
            "marital_status": marital_status,
            "years_living_in": years_living_in,
            "family_planning": family_planning,
            "educational_attainment": educational_attainment,
            "residence_type": residence_type,
            "is_head": is_head,
            "is_pwd": is_pwd,
            "is_senior": is_senior,
            "is_lgbt": is_lgbt,
            "is_solo_parent": is_solo_parent,
            "is_pregnant": is_pregnant,
            "is_young_parent": is_young_parent,
            "is_4ps": is_4ps,
            "sector": sector,
            "email": str(payload.get("email", "")).strip() or None,
            "phone": str(payload.get("phone", "")).strip() or None,
            "age": age,
            "gender": gender,
            "resident_since": manila_today().isoformat(),
            "status": req_status,
        })
        return jsonify({"data": resident}), 201
    except ValueError:
        return jsonify({"error": "Age must be a whole number"}), 400
    except RuntimeError as error:
        return jsonify({"error": str(error), "configured": supabase_repository.server_configured}), 503
    except Exception as error:
        app.logger.exception("Could not create resident in Supabase")
        return jsonify({"error": str(error)}), 502


@app.patch("/api/residents/<int:resident_id>")
@module_required("Residents")
def update_resident(resident_id: int):
    payload = request.get_json(silent=True) or {}
    if "sex" in payload and "gender" not in payload:
        payload["gender"] = payload["sex"]
    existing = supabase_repository.get_row("residents", resident_id) or {}
    updates: dict[str, object] = {}
    for field in (
        "full_name",
        "address",
        "email",
        "phone",
        "gender",
        "household_role",
        "family_no",
        "date_of_birth",
        "marital_status",
        "family_planning",
        "educational_attainment",
        "residence_type",
        "sector",
    ):
        if field in payload:
            value = str(payload[field] or "").strip()
            updates[field] = value or None
    if "years_living_in" in payload:
        raw_yli = payload.get("years_living_in")
        if raw_yli in (None, ""):
            updates["years_living_in"] = None
        else:
            try:
                updates["years_living_in"] = max(0, int(raw_yli))
            except (ValueError, TypeError):
                updates["years_living_in"] = None
    if "age" in payload:
        try:
            parsed_age = int(payload["age"])
            updates["age"] = max(1, parsed_age)
        except (ValueError, TypeError):
            pass
    elif "date_of_birth" in updates and updates["date_of_birth"]:
        try:
            dob = date.fromisoformat(str(updates["date_of_birth"]))
            today = manila_today()
            calc_age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
            updates["age"] = max(1, calc_age)
        except ValueError:
            pass
    if "is_head" in payload:
        updates["is_head"] = bool(payload["is_head"])
        if updates["is_head"] and not updates.get("household_role"):
            updates["household_role"] = "Head"
    if "household_role" in payload:
        role_val = str(payload["household_role"]).strip()
        updates["household_role"] = role_val
        if role_val.lower() in ("head", "head of the family", "household head", "household leader", "leader"):
            updates["is_head"] = True
            updates["household_role"] = "Head"
    for bool_col in ("is_pwd", "is_senior", "is_lgbt", "is_solo_parent", "is_pregnant", "is_young_parent", "is_4ps"):
        if bool_col in payload:
            updates[bool_col] = bool(payload[bool_col])

    effective_name = str(updates.get("full_name") or existing.get("full_name") or "").strip()
    effective_is_head = bool(updates.get("is_head", existing.get("is_head", False)))

    if "household" in payload:
        raw_hh = str(payload.get("household") or "").strip()
        # Explicit unlink from family modal sends household="UNASSIGNED" without full_name
        if raw_hh == "UNASSIGNED" and "full_name" not in payload:
            updates["household"] = "UNASSIGNED"
        else:
            existing_hh = str(existing.get("household") or "").strip()
            candidate_hh = raw_hh or (existing_hh if existing_hh != "UNASSIGNED" else "")
            updates["household"] = _resolve_household_for_resident(
                effective_name, candidate_hh, effective_is_head, current_id=resident_id
            )
    elif str(existing.get("household") or "UNASSIGNED") == "UNASSIGNED":
        resolved_hh = _resolve_household_for_resident(
            effective_name, "", effective_is_head, current_id=resident_id
        )
        if resolved_hh != "UNASSIGNED":
            updates["household"] = resolved_hh

    # Recompute the sector string whenever any boolean classification changes
    sector_trigger_keys = (
        "is_head",
        "is_pwd",
        "is_senior",
        "is_lgbt",
        "is_solo_parent",
        "is_pregnant",
        "is_young_parent",
        "is_4ps",
        "age",
        "household_role",
    )
    if any(k in payload for k in sector_trigger_keys):
        _age = int(updates.get("age") or existing.get("age") or 0)
        _is_senior = bool(updates.get("is_senior", existing.get("is_senior", False))) or (_age >= 60)
        updates["is_senior"] = _is_senior
        updates["sector"] = _build_resident_sector_string(
            is_head=bool(updates.get("is_head", existing.get("is_head", False))),
            is_pwd=bool(updates.get("is_pwd", existing.get("is_pwd", False))),
            is_senior=_is_senior,
            is_lgbt=bool(updates.get("is_lgbt", existing.get("is_lgbt", False))),
            is_solo_parent=bool(updates.get("is_solo_parent", existing.get("is_solo_parent", False))),
            is_pregnant=bool(updates.get("is_pregnant", existing.get("is_pregnant", False))),
            is_young_parent=bool(updates.get("is_young_parent", existing.get("is_young_parent", False))),
            is_4ps=bool(updates.get("is_4ps", existing.get("is_4ps", False))),
            age=_age,
        )
    if "status" in payload:
        status = str(payload["status"]).strip()
        if status not in {"Verified", "Pending", "Inactive"}:
            return jsonify({"error": "Invalid resident status"}), 400
        updates["status"] = status
    if not updates:
        return jsonify({"error": "No resident changes were provided"}), 400
    if "full_name" in updates and not updates["full_name"]:
        return jsonify({"error": "Full name cannot be empty"}), 400
    if "address" in updates and not updates["address"]:
        return jsonify({"error": "Address cannot be empty"}), 400
    try:
        resident = supabase_repository.update_row("residents", resident_id, updates)
        return jsonify({"data": resident})
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
    except Exception as error:
        app.logger.exception("Could not update resident")
        return jsonify({"error": str(error)}), 502


@app.delete("/api/residents/<int:resident_id>")
@module_required("Residents")
def delete_resident(resident_id: int):
    try:
        supabase_repository.delete_row("residents", resident_id)
        return jsonify({"deleted": True, "id": resident_id})
    except Exception as error:
        app.logger.exception("Could not delete resident")
        return jsonify({"error": str(error)}), 502


@app.post("/api/documents")
@login_required
def create_document():
    payload = request.get_json(silent=True) or {}
    document_type = str(payload.get("document_type", "")).strip()
    if document_type not in DOCUMENT_TYPES:
        return jsonify({"error": "Choose a document type"}), 400
    try:
        user = current_user()
        if not user:
            return jsonify({"error": "Authentication required"}), 401
        is_resident = user["role"] == "resident"
        resident_name = str(payload.get("resident_name", "")).strip() if not is_resident else str(
            payload.get("full_name") or user.get("full_name") or user.get("email", "")
        ).strip()
        if not resident_name:
            return jsonify({"error": "Resident name is required"}), 400
        purpose = str(payload.get("purpose", "")).strip() or None
        applicant_address = str(payload.get("address", payload.get("applicant_address", ""))).strip() or None
        applicant_contact = str(payload.get("contact_number", payload.get("applicant_contact", ""))).strip() or None
        applicant_date_of_birth = str(payload.get("date_of_birth", payload.get("applicant_date_of_birth", ""))).strip() or None
        applicant_place_of_birth = str(payload.get("place_of_birth", payload.get("applicant_place_of_birth", ""))).strip() or None
        applicant_civil_status = str(payload.get("civil_status", payload.get("applicant_civil_status", ""))).strip() or None
        business_name = str(payload.get("business_name", "")).strip() or None
        business_address = str(payload.get("business_address", "")).strip() or None
        if is_resident:
            required_fields = {
                "Address": applicant_address,
                "Contact number": applicant_contact,
                "Purpose": purpose,
            }
            missing = [label for label, value in required_fields.items() if not value]
            if missing:
                return jsonify({"error": f"Complete the following fields: {', '.join(missing)}"}), 400
            if applicant_civil_status and applicant_civil_status not in CIVIL_STATUSES:
                return jsonify({"error": "Choose a valid civil status"}), 400
            if document_type == "Business Clearance" and (not business_name or not business_address):
                return jsonify({"error": "Business name and business address are required for a Business Clearance"}), 400
        if applicant_date_of_birth:
            try:
                parsed_birth_date = date.fromisoformat(applicant_date_of_birth)
            except ValueError:
                return jsonify({"error": "Date of birth must use a valid date"}), 400
            if parsed_birth_date > manila_today():
                return jsonify({"error": "Date of birth cannot be in the future"}), 400
        if purpose and len(purpose) > 500:
            return jsonify({"error": "Purpose must be 500 characters or fewer"}), 400
        for label, value, maximum in (
            ("Address", applicant_address, 500),
            ("Contact number", applicant_contact, 40),
            ("Place of birth", applicant_place_of_birth, 180),
            ("Business name", business_name, 180),
            ("Business address", business_address, 500),
        ):
            if value and len(value) > maximum:
                return jsonify({"error": f"{label} must be {maximum} characters or fewer"}), 400
        supporting_doc_type = str(payload.get("supporting_doc_type", "")).strip() or None
        submitted_email = str(payload.get("resident_email") or payload.get("email") or "").strip()
        resolved_email = (
            submitted_email
            or (str(user.get("email") or "").strip() if is_resident and user.get("email") else "")
            or supabase_repository._resolve_account_email(None, resident_name)
        )
        data = {
            "document_type": document_type,
            "resident_name": resident_name,
            "requester_id": user["id"] if is_resident else None,
            "resident_email": resolved_email,
            "purpose": purpose,
            "applicant_address": applicant_address,
            "applicant_contact": applicant_contact,
            "applicant_date_of_birth": applicant_date_of_birth,
            "applicant_place_of_birth": applicant_place_of_birth,
            "applicant_civil_status": applicant_civil_status,
            "business_name": business_name,
            "business_address": business_address,
            "supporting_doc_type": supporting_doc_type,
            "date_requested": manila_today().isoformat(),
            "status": "Pending",
        }
        if is_resident and not supabase_repository.can_create_document(user["id"], document_type):
            return jsonify({"error": "You already have an active request for this document"}), 429
        if not is_resident and user["role"] not in {"super_admin", "admin", "barangay_official"}:
            return jsonify({"error": "You do not have permission to create document requests"}), 403
        return jsonify({"data": supabase_repository.insert_row("documents", data)}), 201
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
    except Exception as error:
        app.logger.exception("Could not create document request")
        return jsonify({"error": str(error)}), 502


def _resolve_resident_email(doc: dict) -> str:
    for email_key in ("resident_email", "email", "applicant_email", "requester_email", "contact_email"):
        cur = str(doc.get(email_key) or "").strip()
        if cur:
            return cur
    return supabase_repository._resolve_account_email(
        doc.get("requester_id") or doc.get("resident_id"),
        doc.get("resident_name"),
    )


def _send_gmail_notification(
    to_email: str,
    subject: str,
    body: str,
    resident_name: str | None = None,
    attachments: list[tuple[str, bytes, str, str]] | None = None,
) -> dict:
    import html as html_lib
    import smtplib
    from email.message import EmailMessage
    from email.utils import formataddr, formatdate, make_msgid

    smtp_user = os.getenv("SMTP_EMAIL", "").strip()
    smtp_pass = os.getenv("SMTP_PASSWORD", "").replace(" ", "").strip()
    clean_subject = subject.replace("—", "-").replace("–", "-")
    clean_body = body.replace("•", "-").replace("—", "-")
    recipients = [addr.strip() for addr in str(to_email or "").split(",") if addr.strip()]
    delivery = "SmartBarangay Mobile App Notification (SMTP Not Configured)"
    smtp_delivered = False
    smtp_error = None
    attached_names: list[str] = []
    if smtp_user and smtp_pass and recipients:
        try:
            msg = EmailMessage()
            msg["Subject"] = clean_subject
            msg["From"] = formataddr(("SmartBarangay Official", smtp_user))
            msg["To"] = ", ".join(recipients)
            msg["Reply-To"] = smtp_user
            msg["Date"] = formatdate(localtime=True)
            domain = smtp_user.split("@")[1] if "@" in smtp_user else "smartbarangay.local"
            msg["Message-ID"] = make_msgid(domain=domain)
            msg.set_content(clean_body)

            html_lines = "<br>".join(html_lib.escape(line) for line in clean_body.splitlines())
            html_body = (
                f"<div style='font-family:Segoe UI,Arial,sans-serif;max-width:600px;margin:0 auto;"
                f"padding:20px;border:1px solid #e2e8f0;border-radius:10px;color:#0f172a;'>"
                f"<h2 style='color:#0b2545;margin-top:0;'>SmartBarangay Official Notification</h2>"
                f"<p style='font-size:14px;line-height:1.6;color:#334155;'>{html_lines}</p>"
                f"<hr style='border:none;border-top:1px solid #e2e8f0;margin:16px 0;'>"
                f"<small style='color:#64748b;'>Barangay Don Felipe Larrazabal, Ormoc City, Leyte</small>"
                f"</div>"
            )
            msg.add_alternative(html_body, subtype="html")

            for att_name, att_bytes, maintype, subtype in (attachments or []):
                if att_bytes:
                    msg.add_attachment(att_bytes, maintype=maintype, subtype=subtype, filename=att_name)
                    attached_names.append(att_name)

            with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(smtp_user, smtp_pass)
                refused = server.send_message(msg, from_addr=smtp_user, to_addrs=recipients)
                if refused:
                    raise RuntimeError(f"Recipient refused by server: {refused}")
            att_suffix = f" (Attached: {', '.join(attached_names)})" if attached_names else ""
            delivery = f"Sent from {smtp_user} to {', '.join(recipients)}{att_suffix}"
            smtp_delivered = True
            print(f"[SMTP DELIVERED] From {smtp_user} -> To {recipients}{att_suffix} | Subject: {clean_subject}", flush=True)
        except Exception as err:
            smtp_error = str(err)
            delivery = f"Notification Only (SMTP Error: {smtp_error})"
            print(f"[SMTP ERROR] {smtp_error}", flush=True)
    elif not recipients:
        smtp_error = "No resident email address on record."
        delivery = "No recipient email address"
    return {
        "from": smtp_user or "SmartBarangay System",
        "to": ", ".join(recipients) if recipients else to_email,
        "resident_name": resident_name or "Resident",
        "subject": clean_subject,
        "body": clean_body,
        "sent_at": manila_now().isoformat(),
        "delivery": delivery,
        "attachments": attached_names,
        "smtp_delivered": smtp_delivered,
        "smtp_configured": bool(smtp_user and smtp_pass),
        "smtp_error": smtp_error,
    }


@app.get("/api/settings/smtp")
@module_required("Settings")
def get_smtp_settings():
    smtp_user = os.getenv("SMTP_EMAIL", "").strip()
    smtp_pass = os.getenv("SMTP_PASSWORD", "").replace(" ", "").strip()
    return jsonify({
        "configured": bool(smtp_user and smtp_pass),
        "smtp_email": smtp_user,
    })


@app.post("/api/settings/smtp")
@module_required("Settings")
def save_smtp_settings():
    import smtplib

    payload = request.get_json(silent=True) or {}
    smtp_email = str(payload.get("smtp_email") or "").strip()
    smtp_password = str(payload.get("smtp_password") or "").replace(" ", "").strip()
    if not smtp_email or not smtp_password:
        return jsonify({"error": "Both Sender Gmail Address and 16-character Google App Password are required"}), 400
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as server:
            server.starttls()
            server.login(smtp_email, smtp_password)
    except Exception as err:
        return jsonify({
            "error": f"Gmail SMTP login failed: {err}. Make sure you use a 16-character Google App Password (from myaccount.google.com/apppasswords), not your regular Gmail password."
        }), 400

    os.environ["SMTP_EMAIL"] = smtp_email
    os.environ["SMTP_PASSWORD"] = smtp_password
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    lines = []
    if os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            lines = [
                ln.rstrip("\r\n")
                for ln in f.readlines()
                if not ln.startswith("SMTP_EMAIL=") and not ln.startswith("SMTP_PASSWORD=")
            ]
    lines.append(f"SMTP_EMAIL={smtp_email}")
    lines.append(f"SMTP_PASSWORD={smtp_password}")
    with open(env_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    return jsonify({"configured": True, "smtp_email": smtp_email})


def _evaluate_receipt_auto_checks(
    doc: dict,
    receipt_ref: str,
    receipt_amount: float,
    receipt_date: str,
    payment_method: str,
    has_file: bool,
) -> dict:
    expected_ref = str(doc.get("tx_ref") or "").strip()
    expected_amt = float(doc.get("amount_due") if doc.get("amount_due") is not None else 50.0)
    ref_clean = (receipt_ref or "").strip()

    ref_ok = len(ref_clean) >= 6
    ref_exact_tx = bool(expected_ref and ref_clean.upper() == expected_ref.upper())
    amt_ok = receipt_amount >= expected_amt and receipt_amount >= 0
    date_ok = bool(receipt_date and len(receipt_date.strip()) >= 8)
    file_ok = bool(has_file and payment_method)

    checks = [
        {
            "key": "ref",
            "label": "Transaction / Receipt Reference",
            "passed": ref_ok,
            "state": "green" if ref_ok else "red",
            "detail": f"Ref #{ref_clean} ({'Matches TXN Ref' if ref_exact_tx else 'Valid Receipt Reference Format'})" if ref_ok else "Missing or invalid transaction reference number",
        },
        {
            "key": "amount",
            "label": "Payment Amount Match",
            "passed": amt_ok,
            "state": "green" if amt_ok else "red",
            "detail": f"Submitted ₱{receipt_amount:.2f} matches required ₱{expected_amt:.2f}" if amt_ok else f"Submitted ₱{receipt_amount:.2f} is less than required ₱{expected_amt:.2f}",
        },
        {
            "key": "date",
            "label": "Payment Date Validity",
            "passed": date_ok,
            "state": "green" if date_ok else "red",
            "detail": f"Payment date recorded: {receipt_date}" if date_ok else "Missing payment date on receipt",
        },
        {
            "key": "proof",
            "label": "Receipt Proof & Payment Method",
            "passed": file_ok,
            "state": "green" if file_ok else "yellow",
            "detail": f"Uploaded receipt image/proof via {payment_method}" if file_ok else "Receipt attachment requires manual official inspection",
        },
    ]
    passed_count = sum(1 for c in checks if c["passed"])
    return {
        "checks": checks,
        "passed_count": passed_count,
        "total": len(checks),
        "all_passed": passed_count == len(checks),
        "summary": f"{passed_count}/{len(checks)} Receipt Checks Passed",
    }


@app.post("/api/documents/<int:document_id>/send-payment-instructions")
@module_required("Documents")
def send_document_payment_instructions(document_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        doc = supabase_repository.get_row("documents", document_id)
        if not doc:
            return jsonify({"error": "Document request not found"}), 404
        official_name = current_user().get("full_name", current_user()["email"])
        doc_type = doc.get("document_type", "Barangay Certificate")
        default_amt = 0.0 if doc_type == "Certificate of Indigency" else (150.0 if doc_type == "Business Clearance" else 50.0)
        try:
            amount_due = float(payload.get("amount_due", doc.get("amount_due", default_amt)))
        except (ValueError, TypeError):
            amount_due = default_amt

        tx_ref = doc.get("tx_ref") or f"TXN-SB-{manila_today().year}-{document_id:04d}"
        resident_name = str(doc.get("resident_name") or "Resident").strip()
        to_email = str(_resolve_resident_email(doc) or payload.get("resident_email") or "").strip()
        if not to_email:
            return jsonify({"error": "No resident email address is associated with this document request."}), 400

        subject = f"[SmartBarangay] Payment Instructions for {resident_name} — {doc_type} ({tx_ref})"
        body = (
            f"Dear {resident_name},\n\n"
            f"Your document request for {doc_type} has passed initial verification by {official_name}.\n\n"
            f"PAYMENT DETAILS & INSTRUCTIONS:\n"
            f"• Applicant / Resident Name: {resident_name}\n"
            f"• Transaction Reference No.: {tx_ref}\n"
            f"• Document Requested: {doc_type}\n"
            f"• Required Fee Amount: PHP {amount_due:.2f}\n"
            f"• Accepted Payment Methods:\n"
            f"   1. GCash: 0917-123-4567 (Brgy. Don Felipe Larrazabal Treasury)\n"
            f"   2. Maya: 0918-765-4321 (Brgy. Don Felipe Larrazabal)\n"
            f"   3. Over-the-Counter: Barangay Hall Treasurer's Office\n\n"
            f"NEXT STEP:\n"
            f"Once payment is completed, please open the SmartBarangay Mobile App and upload your payment receipt / proof of payment referencing {tx_ref}.\n\n"
            f"Thank you,\n"
            f"Office of the Punong Barangay — Hon. Nenito S. Anonat\n"
            f"Barangay Don Felipe Larrazabal, Ormoc City, Leyte"
        )
        email_entry = _send_gmail_notification(to_email, subject, body, resident_name=resident_name)
        email_logs = list(doc.get("email_logs") or [])
        email_logs.insert(0, email_entry)

        tx_history = list(doc.get("transaction_history") or [])
        tx_history.insert(0, {
            "timestamp": manila_now().isoformat(),
            "actor": official_name,
            "event": "Payment Instructions Sent via Gmail",
            "details": f"Sent payment instructions for {resident_name} (₱{amount_due:.2f}, Ref: {tx_ref}) to {to_email}",
        })

        updates: dict[str, object] = {
            "status": "Processing",
            "processor": official_name,
            "tx_ref": tx_ref,
            "amount_due": amount_due,
            "resident_email": to_email,
            "payment_status": "Awaiting Payment",
            "verification_status": "Verified",
            "checklist_summary": payload.get("checklist_summary") or "4/4 Verified",
            "verified_by": official_name,
            "verified_at": manila_now().isoformat(),
            "action_performed": "Checklist Verified & Payment Email Sent",
            "manual_checks": payload.get("manual_checks") or ["registration", "address_match", "completeness", "supporting_docs"],
            "email_logs": email_logs,
            "transaction_history": tx_history,
        }
        if payload.get("verification_remarks"):
            updates["verification_remarks"] = str(payload["verification_remarks"]).strip()

        updated = supabase_repository.update_row("documents", document_id, updates)
        return jsonify({"data": updated, "email_sent": email_entry})
    except Exception as error:
        app.logger.exception("Could not send payment instructions")
        return jsonify({"error": str(error)}), 502


@app.post("/api/documents/<int:document_id>/receipt")
@login_required
def upload_document_receipt(document_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        doc = supabase_repository.get_row("documents", document_id)
        if not doc:
            return jsonify({"error": "Document request not found"}), 404
        user = current_user()
        payment_method = str(payload.get("payment_method") or "GCash").strip()
        receipt_ref_no = str(payload.get("receipt_ref_no") or "").strip()
        receipt_date = str(payload.get("receipt_date") or manila_today().isoformat()).strip()
        receipt_filename = str(payload.get("receipt_filename") or "payment-receipt.png").strip()
        receipt_file_url = str(payload.get("receipt_file_url") or "").strip()
        try:
            receipt_amount = float(payload.get("receipt_amount") or 0)
        except (ValueError, TypeError):
            return jsonify({"error": "Enter a valid payment amount"}), 400
        if not receipt_ref_no:
            return jsonify({"error": "Transaction / Receipt Reference Number is required"}), 400

        auto_checks = _evaluate_receipt_auto_checks(
            doc=doc,
            receipt_ref=receipt_ref_no,
            receipt_amount=receipt_amount,
            receipt_date=receipt_date,
            payment_method=payment_method,
            has_file=bool(receipt_file_url or receipt_filename),
        )

        tx_history = list(doc.get("transaction_history") or [])
        tx_history.insert(0, {
            "timestamp": manila_now().isoformat(),
            "actor": user.get("full_name") or user.get("email") or doc.get("resident_name", "Resident"),
            "event": "Payment Receipt Uploaded by Resident",
            "details": f"Method: {payment_method} · Ref: {receipt_ref_no} · Amount: ₱{receipt_amount:.2f} · Auto-check: {auto_checks['summary']}",
        })

        updates: dict[str, object] = {
            "payment_status": "Pending Verification",
            "payment_method": payment_method,
            "receipt_ref_no": receipt_ref_no,
            "receipt_amount": receipt_amount,
            "receipt_date": receipt_date,
            "receipt_filename": receipt_filename,
            "receipt_file_url": receipt_file_url,
            "receipt_submitted_at": manila_now().isoformat(),
            "receipt_auto_checks": auto_checks,
            "action_performed": "Receipt Submitted — Pending Official Verification",
            "transaction_history": tx_history,
        }
        updated = supabase_repository.update_row("documents", document_id, updates)
        return jsonify({"data": updated, "auto_checks": auto_checks})
    except Exception as error:
        app.logger.exception("Could not upload payment receipt")
        return jsonify({"error": str(error)}), 502


@app.patch("/api/documents/<int:document_id>")
@module_required("Documents")
def update_document(document_id):
    payload = request.get_json(silent=True) or {}
    status = payload.get("status")
    if status and status not in DOCUMENT_STATUSES:
        return jsonify({"error": "Invalid document status"}), 400
    official_name = current_user().get("full_name", current_user()["email"])
    doc = supabase_repository.get_row("documents", document_id) or {}
    updates: dict[str, object] = {"processor": official_name}
    if status:
        updates["status"] = status
    if "scheduled_date" in payload:
        updates["scheduled_date"] = payload["scheduled_date"] or None
    if "scheduled_time" in payload:
        updates["scheduled_time"] = payload["scheduled_time"] or None
    if "amount_paid" in payload and payload["amount_paid"] not in (None, ""):
        try:
            updates["amount_paid"] = float(payload["amount_paid"])
        except (ValueError, TypeError):
            pass
    for meta_field in (
        "tx_ref",
        "amount_due",
        "resident_email",
        "payment_status",
        "payment_method",
        "receipt_ref_no",
        "receipt_amount",
        "receipt_date",
        "supporting_doc_type",
        "verification_status",
        "checklist_summary",
        "action_performed",
        "verification_remarks",
        "manual_checks",
    ):
        if meta_field in payload:
            updates[meta_field] = payload[meta_field]
    updates["verified_by"] = official_name
    updates["verified_at"] = manila_now().isoformat()

    tx_history = list(doc.get("transaction_history") or [])
    email_logs = list(doc.get("email_logs") or [])
    to_email = str(updates.get("resident_email") or doc.get("resident_email") or _resolve_resident_email(doc)).strip()
    tx_ref = str(updates.get("tx_ref") or doc.get("tx_ref") or f"TXN-SB-{manila_today().year}-{document_id:04d}")
    updates.setdefault("tx_ref", tx_ref)
    updates.setdefault("resident_email", to_email)

    resident_name = str(doc.get("resident_name") or "Resident").strip()
    if status == "Approved" or payload.get("payment_status") == "Verified":
        updates["payment_status"] = "Verified"
        updates["status"] = "Approved"
        paid_val = updates.get("amount_paid", doc.get("receipt_amount", doc.get("amount_due", 50.0)))
        try:
            paid_float = float(paid_val or 0.0)
        except (ValueError, TypeError):
            paid_float = 50.0
        updates["amount_paid"] = paid_float

        doc_type = str(doc.get("document_type") or "Barangay Certificate")
        merged_for_pdf = {**doc, **updates}
        pdf_bytes = None
        pdf_filename = ""
        try:
            pdf_bytes = _certificate_pdf(merged_for_pdf)
            req_slug = re.sub(r"[^A-Za-z0-9_-]+", "-", str(merged_for_pdf.get("request_id") or tx_ref or document_id))
            doc_slug = re.sub(r"[^A-Za-z0-9_-]+", "-", doc_type.lower())
            pdf_filename = f"{doc_slug}-{req_slug}.pdf"
        except Exception:
            app.logger.exception("Could not generate PDF attachment for approval email")

        subject = f"[SmartBarangay] Official {doc_type} Released for {resident_name} ({tx_ref})"
        body = (
            f"Dear {resident_name},\n\n"
            f"We are pleased to inform you that your payment has been verified and your requested {doc_type} has been officially approved and released by {official_name}.\n\n"
            f"RELEASE & PAYMENT SUMMARY:\n"
            f"• Applicant / Resident Name: {resident_name}\n"
            f"• Document Type: {doc_type}\n"
            f"• Transaction Reference: {tx_ref}\n"
            f"• Receipt Reference: {doc.get('receipt_ref_no') or 'Verified'}\n"
            f"• Amount Verified: PHP {paid_float:.2f}\n"
            f"• Status: APPROVED & RELEASED\n\n"
            f"Attached to this email is your official {doc_type} PDF issued by Barangay Don Felipe Larrazabal, Ormoc City. You may also access it anytime on the SmartBarangay Mobile App.\n\n"
            f"Thank you,\n"
            f"Office of the Punong Barangay — Hon. Nenito S. Anonat\n"
            f"Barangay Don Felipe Larrazabal, Ormoc City, Leyte"
        )
        attachments = [(pdf_filename, pdf_bytes, "application", "pdf")] if pdf_bytes else None
        email_entry = _send_gmail_notification(
            to_email,
            subject,
            body,
            resident_name=resident_name,
            attachments=attachments,
        )
        email_logs.insert(0, email_entry)
        tx_history.insert(0, {
            "timestamp": manila_now().isoformat(),
            "actor": official_name,
            "event": "Document Released & Official PDF Emailed" if email_entry.get("smtp_delivered") else "Document Released (Email Pending)",
            "details": (
                f"Verified ₱{paid_float:.2f} for {resident_name} (Ref: {doc.get('receipt_ref_no') or tx_ref}). "
                f"{'Sent official PDF to ' + to_email if email_entry.get('smtp_delivered') else (email_entry.get('smtp_error') or 'Email not delivered')}"
            ),
        })
        updates["email_logs"] = email_logs
        updates["transaction_history"] = tx_history
    elif status == "Rejected":
        updates["status"] = "Rejected"
        updates["verification_status"] = "Rejected"
        if doc.get("payment_status") == "Verified" or payload.get("payment_status") == "Rejected":
            updates["payment_status"] = "Rejected"
        reason = str(payload.get("verification_remarks") or "Document requirements not met").strip()
        tx_history.insert(0, {
            "timestamp": manila_now().isoformat(),
            "actor": official_name,
            "event": "Rejected Request",
            "details": reason,
        })
        updates["transaction_history"] = tx_history
    elif payload.get("payment_status") == "Rejected":
        updates["payment_status"] = "Rejected"
        reason = str(payload.get("verification_remarks") or "Submitted payment receipt could not be validated.").strip()
        subject = f"[SmartBarangay] Payment Receipt Update for {resident_name} — {doc.get('document_type', 'Certificate')} ({tx_ref})"
        body = (
            f"Dear {resident_name},\n\n"
            f"Your uploaded payment receipt for {doc.get('document_type', 'your document request')} ({tx_ref}) could not be verified.\n"
            f"Reason / Official Remarks: {reason}\n\n"
            f"Please open the SmartBarangay Mobile App and upload a valid proof of payment.\n\n"
            f"Office of the Punong Barangay — Hon. Nenito S. Anonat"
        )
        email_entry = _send_gmail_notification(to_email, subject, body, resident_name=resident_name)
        email_logs.insert(0, email_entry)
        tx_history.insert(0, {
            "timestamp": manila_now().isoformat(),
            "actor": official_name,
            "event": "Payment Receipt Rejected",
            "details": f"Rejected receipt for {resident_name}: {reason}",
        })
        updates["email_logs"] = email_logs
        updates["transaction_history"] = tx_history
    elif payload.get("action_performed"):
        tx_history.insert(0, {
            "timestamp": manila_now().isoformat(),
            "actor": official_name,
            "event": str(payload["action_performed"]),
            "details": str(payload.get("verification_remarks") or "Updated verification record"),
        })
        updates["transaction_history"] = tx_history

    try:
        return jsonify({"data": supabase_repository.update_row("documents", document_id, updates)})
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
    except Exception as error:
        app.logger.exception("Could not update document")
        return jsonify({"error": str(error)}), 502


def _norm_person_name(value: object) -> str:
    cleaned = re.sub(r"[^a-z0-9\s]+", " ", str(value or "").strip().lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _find_matching_resident(resident_name: str | None, resident_email: str | None = None) -> dict | None:
    target = _norm_person_name(resident_name)
    target_email = str(resident_email or "").strip().lower()
    if not target and not target_email:
        return None
    try:
        residents = supabase_repository.list_residents()
    except Exception:
        return None
    # Pass 1: exact normalized name match
    for r in residents:
        rname = _norm_person_name(r.get("full_name") or r.get("name"))
        if target and rname and rname == target:
            return r
    # Pass 2: email match
    if target_email:
        for r in residents:
            rem = str(r.get("email") or "").strip().lower()
            if rem and rem == target_email:
                return r
    # Pass 3: substring name match
    for r in residents:
        rname = _norm_person_name(r.get("full_name") or r.get("name"))
        if target and rname and (target in rname or rname in target):
            return r
    return None


def _format_dob_label(raw_dob: object) -> str:
    s = str(raw_dob or "").strip()
    if not s:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%B %d, %Y"):
        try:
            dt = datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s, fmt).date()
            return dt.strftime("%B %d, %Y").upper()
        except ValueError:
            continue
    try:
        dt = date.fromisoformat(s[:10])
        return dt.strftime("%B %d, %Y").upper()
    except ValueError:
        return s.upper()


def _compute_age_from_dob(raw_dob: object) -> int | None:
    s = str(raw_dob or "").strip()
    if not s:
        return None
    try:
        dob = date.fromisoformat(s[:10])
        today = manila_today()
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    except ValueError:
        return None


def _normalize_barangay_address(raw_addr: str) -> str:
    addr = re.sub(r"\s+", " ", str(raw_addr or "").strip()).strip(" ,")
    if not addr:
        return "Barangay Don Felipe Larrazabal, Ormoc City"
    # Fix common single-r spelling typo ("Larazabal" -> "Larrazabal")
    addr = re.sub(r"\blar+azabal\b", "Larrazabal", addr, flags=re.IGNORECASE)
    # Collapse accidental duplicate barangay suffixes
    addr = re.sub(
        r"(?:,?\s*(?:Barangay|Brgy\.?)\s+Don\s+Felipe\s+Larrazabal){2,}",
        ", Barangay Don Felipe Larrazabal",
        addr,
        flags=re.IGNORECASE,
    ).strip(" ,")
    has_brgy = bool(re.search(r"don\s+felipe\s+larrazabal", addr, re.IGNORECASE))
    has_city = bool(re.search(r"\bormoc(\s+city)?\b|\bo\.?c\.?\b", addr, re.IGNORECASE))
    if not has_brgy:
        addr = f"{addr}, Barangay Don Felipe Larrazabal"
    if not has_city:
        addr = f"{addr}, Ormoc City"
    return addr


def _certificate_pdf(document: dict) -> bytes:
    styles = getSampleStyleSheet()
    matched_res = _find_matching_resident(
        document.get("resident_name"),
        document.get("resident_email") or document.get("email"),
    ) or {}

    def clean_first(*candidates: object) -> str:
        invalid = {"", "n/a", "na", "none", "null", "not provided", "-", "--", "—", "none / n/a"}
        for val in candidates:
            if val is None:
                continue
            s = str(val).strip()
            if s and s.lower() not in invalid:
                return s
        return ""

    def text(value: object, fallback: str = "") -> str:
        s = clean_first(value)
        return xml_escape(s) if s else fallback

    document_type = str(document.get("document_type") or "Barangay Clearance").strip()
    full_name = text(clean_first(document.get("resident_name"), matched_res.get("full_name")), "RESIDENT NAME").upper()
    purpose_raw = clean_first(document.get("purpose"))
    purpose = xml_escape(purpose_raw) if purpose_raw else ""
    request_id = text(clean_first(document.get("request_id"), document.get("tx_ref"), f"DOC-{document.get('id', '0000')}"), "")
    tx_ref = text(clean_first(document.get("tx_ref"), document.get("receipt_ref_no")), "")
    processor_name = text(clean_first(document.get("verified_by"), document.get("processor")), "")

    raw_address = clean_first(
        document.get("applicant_address"),
        matched_res.get("address"),
    )
    full_address = _normalize_barangay_address(raw_address)
    full_address_escaped = xml_escape(full_address)

    raw_dob = clean_first(
        document.get("applicant_date_of_birth"),
        matched_res.get("date_of_birth"),
        matched_res.get("dob"),
        matched_res.get("birthdate"),
    )
    dob_label = text(_format_dob_label(raw_dob), "N/A")
    age_val = matched_res.get("age") or _compute_age_from_dob(raw_dob)
    age_display = f"{int(age_val)} years old" if age_val not in (None, "") and str(age_val).isdigit() else "of legal age"

    gender_raw = clean_first(
        document.get("applicant_gender"),
        matched_res.get("gender"),
        matched_res.get("sex"),
    )
    gender_display = text(gender_raw.upper(), "N/A")
    gender_prose = xml_escape(gender_raw.lower()) if gender_raw else ""

    civil_raw = clean_first(
        document.get("applicant_civil_status"),
        matched_res.get("marital_status"),
        matched_res.get("civil_status"),
    )
    civil_display = text(civil_raw.upper(), "N/A")
    civil_prose = xml_escape(civil_raw.lower()) if civil_raw else "single"

    birthplace_display = text(
        clean_first(document.get("applicant_place_of_birth"), matched_res.get("place_of_birth"), "ORMOC CITY"),
        "ORMOC CITY",
    ).upper()

    resident_since_raw = str(matched_res.get("resident_since") or "").strip()
    since_year = resident_since_raw[:4] if len(resident_since_raw) >= 4 and resident_since_raw[:4].isdigit() else ""

    today = manila_today()
    day = today.day
    ord_suffix = "th" if 11 <= (day % 100) <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    month_upper = today.strftime("%B").upper()
    month_title = today.strftime("%B")
    issue_date_long = f"{day}<sup>{ord_suffix}</sup> day of {month_upper}, {today.year}"

    # Typography aligned to the official Barangay Don Felipe Larrazabal templates
    title_style = ParagraphStyle(
        "OfficialCertTitle",
        parent=styles["Normal"],
        fontName="Times-BoldItalic",
        fontSize=21,
        leading=26,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#0F172A"),
    )
    title_upper_style = ParagraphStyle(
        "OfficialCertTitleUpper",
        parent=styles["Normal"],
        fontName="Times-Bold",
        fontSize=19,
        leading=24,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#0F172A"),
    )
    to_whom_style = ParagraphStyle(
        "OfficialToWhom",
        parent=styles["Normal"],
        fontName="Times-Bold",
        fontSize=11.5,
        leading=16,
        alignment=TA_LEFT,
        textColor=colors.black,
    )
    body_style = ParagraphStyle(
        "OfficialBody",
        parent=styles["Normal"],
        fontName="Times-Roman",
        fontSize=11.5,
        leading=18.5,
        alignment=TA_JUSTIFY,
        firstLineIndent=34,
        textColor=colors.black,
    )
    body_plain_style = ParagraphStyle(
        "OfficialBodyPlain",
        parent=styles["Normal"],
        fontName="Times-Roman",
        fontSize=11.5,
        leading=17.5,
        alignment=TA_LEFT,
        textColor=colors.black,
    )
    field_label_style = ParagraphStyle(
        "OfficialFieldLabel",
        parent=styles["Normal"],
        fontName="Times-Bold",
        fontSize=11,
        leading=16,
        alignment=TA_LEFT,
        textColor=colors.black,
    )
    field_val_style = ParagraphStyle(
        "OfficialFieldVal",
        parent=styles["Normal"],
        fontName="Times-Roman",
        fontSize=11,
        leading=16,
        alignment=TA_LEFT,
        textColor=colors.black,
    )
    captain_name_style = ParagraphStyle(
        "OfficialCaptainName",
        parent=styles["Normal"],
        fontName="Times-Bold",
        fontSize=11.5,
        leading=15,
        alignment=TA_CENTER,
        textColor=colors.black,
    )
    captain_sub_style = ParagraphStyle(
        "OfficialCaptainSub",
        parent=styles["Normal"],
        fontName="Times-Roman",
        fontSize=10.5,
        leading=14,
        alignment=TA_CENTER,
        textColor=colors.black,
    )
    meta_style = ParagraphStyle(
        "OfficialMeta",
        parent=styles["Normal"],
        fontName="Times-Roman",
        fontSize=9.5,
        leading=14,
        alignment=TA_LEFT,
        textColor=colors.black,
    )
    seal_notice_style = ParagraphStyle(
        "OfficialSealNotice",
        parent=styles["Normal"],
        fontName="Times-BoldItalic",
        fontSize=9.5,
        leading=13,
        alignment=TA_RIGHT,
        textColor=colors.HexColor("#334155"),
    )

    buffer = io.BytesIO()
    # Margins positioned cleanly inside the official Barangay Don Felipe Larrazabal letterhead artwork
    left_m = 1.28 * inch
    right_m = 0.80 * inch
    top_m = 2.18 * inch
    bottom_m = 0.85 * inch
    usable_width = A4[0] - left_m - right_m

    pdf = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=right_m,
        leftMargin=left_m,
        topMargin=top_m,
        bottomMargin=bottom_m,
        title=f"{document_type} - {request_id}",
        author="Barangay Don Felipe Larrazabal, Ormoc City",
    )

    letterhead_path = os.path.join(app.root_path, "public", "static", "barangay_letterhead.png")

    def _draw_official_letterhead(canvas, _doc):
        canvas.saveState()
        if os.path.exists(letterhead_path):
            canvas.drawImage(
                letterhead_path,
                0,
                0,
                width=A4[0],
                height=A4[1],
                preserveAspectRatio=False,
                mask="auto",
            )
        else:
            canvas.setFont("Times-Bold", 12)
            canvas.drawCentredString(A4[0] / 2, A4[1] - 48, "Republic of the Philippines")
            canvas.drawCentredString(A4[0] / 2, A4[1] - 64, "OFFICE OF THE SANGGUNIANG BARANGAY")
            canvas.drawCentredString(A4[0] / 2, A4[1] - 80, "Barangay Don Felipe Larrazabal, Ormoc City")
        canvas.restoreState()

    def _signature_block(name_line: str = "NENITO S. ANONAT", title_line: str = "Punong Barangay", include_resident_sig: bool = False, attested_by: str | None = None):
        captain_tbl = Table(
            [
                [Paragraph(f"<u><b>{name_line}</b></u>", captain_name_style)],
                [Paragraph(title_line, captain_sub_style)],
            ],
            colWidths=[200],
            hAlign="RIGHT",
        )
        captain_tbl.setStyle(TableStyle([
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ]))

        if include_resident_sig:
            res_tbl = Table(
                [
                    [Paragraph("___________________________", captain_name_style)],
                    [Paragraph("Signature of Resident", captain_sub_style)],
                ],
                colWidths=[190],
                hAlign="LEFT",
            )
            res_tbl.setStyle(TableStyle([
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]))
            grid = Table([[res_tbl, captain_tbl]], colWidths=[usable_width * 0.48, usable_width * 0.52])
            grid.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]))
            return grid

        if attested_by:
            att_tbl = Table(
                [
                    [Paragraph("Attested by:", ParagraphStyle("AttLbl", parent=captain_sub_style, alignment=TA_LEFT, fontName="Times-Italic"))],
                    [Spacer(1, 14)],
                    [Paragraph(f"<u><b>{attested_by.upper()}</b></u>", captain_name_style)],
                    [Paragraph("Barangay Official on Duty", captain_sub_style)],
                ],
                colWidths=[210],
                hAlign="LEFT",
            )
            att_tbl.setStyle(TableStyle([
                ("ALIGN", (0, 2), (-1, -1), "CENTER"),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]))
            grid = Table([["", captain_tbl], [att_tbl, ""]], colWidths=[usable_width * 0.50, usable_width * 0.50])
            grid.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
            ]))
            return grid

        grid = Table([["", captain_tbl]], colWidths=[usable_width * 0.45, usable_width * 0.55])
        grid.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ]))
        return grid

    has_purpose = bool(purpose_raw and purpose_raw.lower() not in ("not provided", "none", "general", "whatever legal purposes it may serve"))
    story: list[object] = []

    if document_type == "Barangay Clearance":
        # Exact format matching EMILY T. MANGAHOC Barangay Clearance template
        purpose_display = text(purpose_raw.upper(), "WHATEVER LEGAL PURPOSES IT MAY SERVE")
        info_rows = [
            [Paragraph("Full Name", field_label_style), Paragraph(f":  <b>{full_name}</b>", field_val_style)],
            [Paragraph("Age", field_label_style), Paragraph(f":  {age_display}", field_val_style)],
            [Paragraph("Address", field_label_style), Paragraph(f":  {full_address_escaped}", field_val_style)],
            [Paragraph("Gender", field_label_style), Paragraph(f":  {gender_display}", field_val_style)],
            [Paragraph("Civil Status", field_label_style), Paragraph(f":  {civil_display}", field_val_style)],
            [Paragraph("Date of Birth", field_label_style), Paragraph(f":  {dob_label}", field_val_style)],
            [Paragraph("Place of Birth", field_label_style), Paragraph(f":  {birthplace_display}", field_val_style)],
            [Paragraph("Purpose", field_label_style), Paragraph(f":  <b>{purpose_display}</b>", field_val_style)],
        ]
        info_table = Table(info_rows, colWidths=[110, usable_width - 110], hAlign="LEFT")
        info_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        footer_meta = Table(
            [[
                Paragraph(
                    f"Comm. Tax : ________________<br/>"
                    f"Issued On : {month_title} {day}, {today.year}<br/>"
                    f"Issued At : Ormoc City<br/>"
                    f"O.R Number : {tx_ref or '________________'}",
                    meta_style,
                ),
                Paragraph("NOT VALID WITHOUT A SEAL", seal_notice_style),
            ]],
            colWidths=[usable_width * 0.62, usable_width * 0.38],
        )
        footer_meta.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ]))
        story = [
            Paragraph("Barangay Clearance", title_style),
            Spacer(1, 18),
            Paragraph("To whom it may concern:", to_whom_style),
            Spacer(1, 10),
            Paragraph("This is to CERTIFY that according to the record available in this Office that,", body_plain_style),
            Spacer(1, 10),
            info_table,
            Spacer(1, 14),
            Paragraph(
                f"Is a bona-fide resident of Barangay Don Felipe Larrazabal, Ormoc City. "
                f"This certifies further that, <b>{full_name}</b> has no derogatory/criminal record in our barangay. "
                f"This certification is being issued upon the request of said party.",
                body_style,
            ),
            Spacer(1, 14),
            Paragraph(
                f"ISSUED THIS {issue_date_long}, at Barangay Don Felipe Larrazabal, Ormoc City, Leyte, Philippines.",
                body_style,
            ),
            Spacer(1, 34),
            _signature_block("NENITO S. ANONAT", "Punong Barangay", include_resident_sig=True),
            Spacer(1, 24),
            footer_meta,
        ]

    elif document_type == "Certificate of Residency":
        # Exact format matching NOR HISHAM R. IBRA CERTIFICATION OF RESIDENCY template
        desc_bits = [bit for bit in (age_display, civil_prose, gender_prose, "Filipino") if bit]
        since_clause = f", since {since_year} up to the present" if since_year else " up to the present"
        purpose_clause = f" in support of his/her requirement for <b>{purpose}</b> and as proof of his/her residency purposes." if has_purpose else " in support of his/her requirement and as proof of his/her residency purposes."
        story = [
            Paragraph("CERTIFICATION OF RESIDENCY", title_upper_style),
            Spacer(1, 26),
            Paragraph("To Whom it may concern:", to_whom_style),
            Spacer(1, 18),
            Paragraph(
                f"THIS IS TO CERTIFY that, <b>{full_name}</b>, {', '.join(desc_bits)}, "
                f"bona-fide resident at Barangay Don Felipe Larrazabal.",
                body_style,
            ),
            Spacer(1, 16),
            Paragraph(
                f"THIS IS TO CERTIFY that the person mentioned above is a bona-fide resident at "
                f"<b>{full_address_escaped}</b>{since_clause}.",
                body_style,
            ),
            Spacer(1, 16),
            Paragraph(
                f"THIS CERTIFICATION is issued upon the request of the aforementioned{purpose_clause}",
                body_style,
            ),
            Spacer(1, 18),
            Paragraph(
                f"Issued this {issue_date_long} at Barangay Don Felipe Larrazabal, Ormoc City.",
                body_style,
            ),
            Spacer(1, 56),
            _signature_block("NENITO S. ANONAT", "Punong Barangay"),
        ]

    elif document_type == "Certificate of Indigency":
        # Exact format matching Certificate of Indigency template (MAE LORAINE G. YU / ARIEL RIVERA)
        desc_bits = [bit for bit in (age_display, gender_prose, civil_prose) if bit]
        purpose_clause = f"for <b>{purpose}</b> purposes." if has_purpose else "for whatever legal or medical/financial assistance purposes it may serve."
        attested_name = processor_name if (processor_name and "anonat" not in processor_name.lower()) else "Barangay Secretary / Official on Duty"
        story = [
            Paragraph("Certificate of Indigency", title_style),
            Spacer(1, 24),
            Paragraph("TO WHOM IT MAY CONCERN:", to_whom_style),
            Spacer(1, 18),
            Paragraph(
                f"This is to certify that, <b>{full_name}</b>, {', '.join(desc_bits)}, "
                f"a bona-fide resident of {full_address_escaped}.",
                body_style,
            ),
            Spacer(1, 16),
            Paragraph(
                "Certifies further that, the above-named person earns a living as a low income "
                "which is not sufficient for their daily needs that government interventions are necessary.",
                body_style,
            ),
            Spacer(1, 16),
            Paragraph(
                f"THIS CERTIFICATION is issued upon the request of the above person {purpose_clause}",
                body_style,
            ),
            Spacer(1, 18),
            Paragraph(
                f"Done this {issue_date_long}, at Barangay Don Felipe Larrazabal, Ormoc City, Leyte.",
                body_style,
            ),
            Spacer(1, 44),
            _signature_block("Hon. Nenito S. Anonat", "Punong Barangay", attested_by=attested_name),
        ]

    elif document_type == "Business Clearance":
        # Exact format matching SHAWNIX BARBERSHOP Barangay Business Clearance template
        business_name = text(document.get("business_name"), "BUSINESS ESTABLISHMENT").upper()
        business_addr_raw = str(document.get("business_address") or full_address).strip()
        if "don felipe larrazabal" in business_addr_raw.lower():
            business_addr_full = business_addr_raw if "ormoc" in business_addr_raw.lower() else f"{business_addr_raw}, Ormoc City"
        else:
            business_addr_full = f"{business_addr_raw}, Barangay Don Felipe Larrazabal, Ormoc City"
        business_addr = text(business_addr_raw, "Barangay Don Felipe Larrazabal, Ormoc City")
        business_addr_full_escaped = text(business_addr_full, "Barangay Don Felipe Larrazabal, Ormoc City")
        nature_business = text(purpose_raw.upper(), "GENERAL MERCHANDISE / SERVICES")
        biz_rows = [
            [Paragraph("Business Name:", field_label_style), Paragraph(f"<b>{business_name}</b>", field_val_style)],
            [Paragraph("Business Address:", field_label_style), Paragraph(business_addr, field_val_style)],
            [Paragraph("Nature of Business:", field_label_style), Paragraph(f"<b>{nature_business}</b>", field_val_style)],
        ]
        biz_table = Table(biz_rows, colWidths=[125, usable_width - 125], hAlign="LEFT")
        biz_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        series_no = f"{today.year}-{int(document.get('id') or 0):03d}"
        biz_meta = Paragraph(
            f"<b>Series No.:</b> &nbsp;&nbsp;&nbsp;&nbsp;{series_no}<br/>"
            f"<b>OR No.:</b> &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;{tx_ref or '________________'}<br/>"
            f"<b>CTC No.:</b> &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;________________<br/>"
            f"<b>Date Issued:</b> &nbsp;{month_upper} {day}, {today.year}<br/>"
            f"<b>Valid until:</b> &nbsp;&nbsp;December 31, {today.year}",
            meta_style,
        )
        story = [
            Paragraph("Barangay Business Clearance", title_style),
            Spacer(1, 22),
            biz_table,
            Spacer(1, 20),
            Paragraph(
                f"This is to certify that <b>{full_name}</b> of legal age, and engaging a business "
                f"in our barangay situated at {business_addr_full_escaped}.",
                body_style,
            ),
            Spacer(1, 16),
            Paragraph(
                "This Business Clearance is issued upon the request of the above mentioned name "
                "for whatever legal purposes it may serve and avail.",
                body_style,
            ),
            Spacer(1, 16),
            Paragraph(
                f"Issued this {issue_date_long} at Barangay Don Felipe Larrazabal, Ormoc City.",
                body_style,
            ),
            Spacer(1, 44),
            _signature_block("NENITO S. ANONAT", "Punong Barangay"),
            Spacer(1, 28),
            biz_meta,
        ]

    else:
        # Generic Barangay Certification template
        desc_bits = [bit for bit in (age_display, gender_prose, civil_prose, "Filipino citizen") if bit]
        purpose_clause = f"for <b>{purpose}</b> purposes." if has_purpose else "for whatever legal purposes it may serve."
        story = [
            Paragraph("Barangay Certification", title_style),
            Spacer(1, 24),
            Paragraph("TO WHOM IT MAY CONCERN:", to_whom_style),
            Spacer(1, 18),
            Paragraph(
                f"THIS IS TO CERTIFY that <b>{full_name}</b>, {', '.join(desc_bits)}, "
                f"is a bona-fide resident of {full_address_escaped}.",
                body_style,
            ),
            Spacer(1, 16),
            Paragraph(
                f"This certification is issued upon the request of the name above mentioned {purpose_clause}",
                body_style,
            ),
            Spacer(1, 18),
            Paragraph(
                f"Issued this {issue_date_long} at Barangay Don Felipe Larrazabal, Ormoc City, Leyte, Philippines.",
                body_style,
            ),
            Spacer(1, 52),
            _signature_block("NENITO S. ANONAT", "Punong Barangay"),
        ]

    pdf.build(story, onFirstPage=_draw_official_letterhead, onLaterPages=_draw_official_letterhead)
    return buffer.getvalue()


@app.post("/api/documents/<int:document_id>/send-document-email")
@module_required("Documents")
def send_released_document_email(document_id: int):
    try:
        doc = supabase_repository.get_row("documents", document_id)
        if not doc:
            return jsonify({"error": "Document request not found"}), 404
        if doc.get("status") not in {"Approved", "Resolved"}:
            return jsonify({"error": "Only approved/released documents can be emailed to residents"}), 409

        official_name = current_user().get("full_name", current_user()["email"])
        resident_name = str(doc.get("resident_name") or "Resident").strip()
        doc_type = str(doc.get("document_type") or "Barangay Certificate").strip()
        tx_ref = str(doc.get("tx_ref") or f"TXN-SB-{manila_today().year}-{document_id:04d}")
        to_email = str(_resolve_resident_email(doc) or "").strip()
        if not to_email:
            return jsonify({"error": "No resident email address is associated with this document request."}), 400

        pdf_bytes = _certificate_pdf(doc)
        req_slug = re.sub(r"[^A-Za-z0-9_-]+", "-", str(doc.get("request_id") or tx_ref or document_id))
        doc_slug = re.sub(r"[^A-Za-z0-9_-]+", "-", doc_type.lower())
        pdf_filename = f"{doc_slug}-{req_slug}.pdf"

        subject = f"[SmartBarangay] Official {doc_type} Released for {resident_name} ({tx_ref})"
        body = (
            f"Dear {resident_name},\n\n"
            f"Your requested {doc_type} from Barangay Don Felipe Larrazabal, Ormoc City has been officially approved and released by {official_name}.\n\n"
            f"DOCUMENT SUMMARY:\n"
            f"• Resident Name: {resident_name}\n"
            f"• Document Type: {doc_type}\n"
            f"• Transaction Reference: {tx_ref}\n"
            f"• Status: APPROVED & RELEASED\n\n"
            f"Attached to this email is your official {doc_type} PDF document.\n\n"
            f"Thank you,\n"
            f"Office of the Punong Barangay — Hon. Nenito S. Anonat\n"
            f"Barangay Don Felipe Larrazabal, Ormoc City, Leyte"
        )
        email_entry = _send_gmail_notification(
            to_email,
            subject,
            body,
            resident_name=resident_name,
            attachments=[(pdf_filename, pdf_bytes, "application", "pdf")],
        )
        email_logs = list(doc.get("email_logs") or [])
        email_logs.insert(0, email_entry)
        tx_history = list(doc.get("transaction_history") or [])
        tx_history.insert(0, {
            "timestamp": manila_now().isoformat(),
            "actor": official_name,
            "event": "Official PDF Document Emailed to Resident" if email_entry.get("smtp_delivered") else "Document Email Delivery Failed",
            "details": email_entry.get("delivery") or f"Target: {to_email}",
        })
        updated = supabase_repository.update_row(
            "documents",
            document_id,
            {
                "tx_ref": tx_ref,
                "resident_email": to_email,
                "email_logs": email_logs,
                "transaction_history": tx_history,
            },
        )
        if not email_entry.get("smtp_delivered"):
            return jsonify({
                "error": email_entry.get("smtp_error") or "SMTP is not configured or failed to deliver email",
                "data": updated,
                "email_sent": email_entry,
            }), 502
        return jsonify({"data": updated, "email_sent": email_entry})
    except Exception as error:
        app.logger.exception("Could not send released document PDF email")
        return jsonify({"error": str(error)}), 502


@app.get("/api/documents/<int:document_id>/pdf")
@app.get("/api/documents/<int:document_id>/certificate.pdf")
@login_required
def download_certificate(document_id: int):
    try:
        user = current_user()
        if not user:
            return jsonify({"error": "Authentication required"}), 401
        document = supabase_repository.get_row("documents", document_id)
        if not document:
            return jsonify({"error": "Document request not found"}), 404
        if user.get("role") == "resident":
            owns_by_id = str(document.get("requester_id") or "") == str(user.get("id") or "")
            owns_by_name = str(document.get("resident_name") or "").strip().lower() == str(user.get("full_name") or "").strip().lower()
            if not (owns_by_id or owns_by_name):
                return jsonify({"error": "Forbidden"}), 403
        if document.get("status") not in {"Approved", "Resolved"}:
            return jsonify({"error": "The certificate can only be downloaded after payment is verified and approved"}), 409
        pdf_bytes = _certificate_pdf(document)
        request_id = re.sub(r"[^A-Za-z0-9_-]+", "-", str(document.get("request_id") or document_id))
        document_slug = re.sub(r"[^A-Za-z0-9_-]+", "-", str(document.get("document_type") or "certificate").lower())
        response = make_response(pdf_bytes)
        response.headers["Content-Type"] = "application/pdf"
        disposition = "attachment" if request.args.get("download") in ("1", "true") else "inline"
        response.headers["Content-Disposition"] = f'{disposition}; filename="{document_slug}-{request_id}.pdf"'
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
    except Exception as error:
        app.logger.exception("Could not generate certificate PDF")
        return jsonify({"error": str(error)}), 502


@app.post("/api/concerns")
@roles_required("admin", "barangay_official", "resident")
def create_concern():
    payload = request.get_json(silent=True) or {}
    required = ("category", "priority", "title", "latitude", "longitude")
    if any(payload.get(field) in (None, "") for field in required):
        return jsonify({"error": "Category, priority, title, and real-time location are required"}), 400
    category = str(payload["category"]).strip()
    priority = str(payload["priority"]).strip()
    title = str(payload["title"]).strip()
    description = str(payload.get("description", "")).strip() or None
    if category not in {"Infrastructure", "Health & Sanitation", "Peace & Order", "Social Services", "Other"}:
        return jsonify({"error": "Choose a valid concern category"}), 400
    if priority not in CONCERN_PRIORITIES:
        return jsonify({"error": "Choose a valid concern priority"}), 400
    if not title or len(title) > 180:
        return jsonify({"error": "Concern subject must be between 1 and 180 characters"}), 400
    if description and len(description) > 2000:
        return jsonify({"error": "Concern description must be 2000 characters or fewer"}), 400
    user = current_user()
    department_categories = current_department_categories(user)
    if department_categories and category not in department_categories:
        return jsonify({"error": "This concern is outside your assigned department"}), 403
    try:
        data = {
            "category": category,
            "priority": priority,
            "title": title,
            "description": description,
            "latitude": float(payload["latitude"]),
            "longitude": float(payload["longitude"]),
            "submitted_by": user.get("full_name", user["email"]),
            "reporter_id": user["id"] if user["role"] == "resident" else None,
            "submitted_at": manila_today().isoformat(),
            "status": "Open",
        }
        if not -90 <= data["latitude"] <= 90 or not -180 <= data["longitude"] <= 180:
            return jsonify({"error": "Location coordinates are outside the valid range"}), 400
        return jsonify({"data": supabase_repository.create_concern(data, enforce_limits=user["role"] == "resident")}), 201
    except ValueError:
        return jsonify({"error": "Location coordinates must be numeric"}), 400
    except RepositoryRateLimitError as error:
        return jsonify({"error": str(error)}), 429
    except RepositoryConflictError as error:
        return jsonify({"error": str(error)}), 409
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
    except Exception as error:
        app.logger.exception("Could not create concern")
        return jsonify({"error": str(error)}), 502


@app.patch("/api/concerns/<int:concern_id>")
@module_required("Concerns")
def update_concern(concern_id):
    payload = request.get_json(silent=True) or {}
    status = payload.get("status")
    if status and status not in CONCERN_STATUSES:
        return jsonify({"error": "Invalid concern status"}), 400
    try:
        user = current_user()
        concern = supabase_repository.get_row("concerns", concern_id)
        if not concern:
            return jsonify({"error": "Concern not found"}), 404
        categories = current_department_categories(user)
        if categories and concern.get("category") not in categories:
            return jsonify({"error": "This concern is outside your assigned department"}), 403
        updates: dict[str, object] = {}
        if status:
            updates["status"] = status
        if "assigned_to" in payload:
            updates["assigned_to"] = str(payload.get("assigned_to") or "").strip() or None
        if "official_feedback" in payload:
            fb = str(payload.get("official_feedback") or "").strip()
            updates["official_feedback"] = fb or None
            if fb:
                updates["feedback_by"] = user.get("full_name") or user.get("email") or "Barangay Official"
                updates["feedback_at"] = manila_now().isoformat()
        if "appointment_date" in payload:
            appt_date = str(payload.get("appointment_date") or "").strip()
            updates["appointment_date"] = appt_date or None
            updates["appointment_time"] = str(payload.get("appointment_time") or "").strip() or None
            updates["appointment_notes"] = str(payload.get("appointment_notes") or "").strip() or None
            if appt_date:
                # If official proposes a new date, set status to Proposed unless specified
                updates["appointment_status"] = str(payload.get("appointment_status") or "Proposed").strip()
            else:
                updates["appointment_status"] = None
        if "resolution_remarks" in payload:
            updates["resolution_remarks"] = str(payload.get("resolution_remarks") or "").strip() or None
        updates["updated_at"] = manila_now().isoformat()
        return jsonify({"data": supabase_repository.update_row("concerns", concern_id, updates)})
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
    except Exception as error:
        app.logger.exception("Could not update concern")
        return jsonify({"error": str(error)}), 502


@app.patch("/api/concerns/<int:concern_id>/appointment")
@login_required
def respond_concern_appointment(concern_id: int):
    payload = request.get_json(silent=True) or {}
    decision = str(payload.get("appointment_status") or "").strip()
    if decision not in {"Confirmed", "Reschedule Requested"}:
        return jsonify({"error": "Choose Confirmed or Reschedule Requested"}), 400
    try:
        concern = supabase_repository.get_row("concerns", concern_id)
        if not concern:
            return jsonify({"error": "Concern not found"}), 404
        updates: dict[str, object] = {
            "appointment_status": decision,
            "updated_at": manila_now().isoformat(),
        }
        if decision == "Reschedule Requested":
            updates["reschedule_note"] = str(payload.get("reschedule_note") or "").strip() or "Resident requested a different schedule"
        else:
            updates["reschedule_note"] = None
        return jsonify({"data": supabase_repository.update_row("concerns", concern_id, updates)})
    except Exception as error:
        app.logger.exception("Could not update concern appointment")
        return jsonify({"error": str(error)}), 502


@app.post("/api/admin/register")
@module_required("Manage Officials")
def register_account():
    payload = request.get_json(silent=True) or {}
    required = ("email", "password", "full_name")
    missing = [field for field in required if not payload.get(field)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400
    position = str(payload.get("position", "")).strip() or None
    requested_role = str(payload.get("role", "barangay_official")).strip().lower()
    if requested_role not in ("admin", "barangay_official"):
        return jsonify({"error": "Super Admin can register Barangay Officials and Barangay Secretary only."}), 400
    # Barangay Secretary receives Admin role; all other registered officials use barangay_official
    assigned_role = "admin" if (position and "secretary" in position.lower()) else requested_role
    raw_password = str(payload.get("password") or "")
    if (
        len(raw_password) < 8
        or not re.search(r"[A-Z]", raw_password)
        or not re.search(r"[a-z]", raw_password)
        or not re.search(r"[0-9]", raw_password)
        or not re.search(r"[^A-Za-z0-9]", raw_password)
    ):
        return jsonify({"error": "Password must be at least 8 characters and include uppercase, lowercase, number, and special character."}), 400
    department = str(payload.get("department", "")).strip() or None
    if department not in DEPARTMENT_CATEGORIES:
        department = "Social Services"
    phone = str(payload.get("phone", "")).strip() or None
    try:
        user = supabase_repository.register_account(
            email=str(payload["email"]).strip().lower(),
            password=str(payload["password"]),
            full_name=str(payload["full_name"]).strip(),
            role=assigned_role,
            department=department,
            position=position,
            phone=phone,
        )
        return jsonify({"data": user}), 201
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
    except Exception as error:
        app.logger.exception("Account registration failed")
        return jsonify({"error": str(error)}), 502

if __name__ == "__main__":
    import threading

    def _startup_sync_warmup():
        try:
            supabase_repository.sync_now()
        except Exception:
            pass

    threading.Thread(target=_startup_sync_warmup, daemon=True).start()
    app.run(
        host=os.getenv("FLASK_HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
    )
