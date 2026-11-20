
import os
import csv
import io
import re
from datetime import date, timedelta
from functools import wraps
from urllib.parse import urlencode
from urllib.request import urlopen
import json
import httpx
from xml.sax.saxutils import escape as xml_escape

from flask import Flask, jsonify, make_response, redirect, render_template, request, session, url_for
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Image as RLImage, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

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

ROLE_ACCESS = {
    "admin": {"Dashboard", "Manage Officials", "Residents", "Documents", "Concerns", "Announcements", "Weather & Alerts", "Reports", "Settings"},
    "barangay_official": {"Dashboard", "Residents", "Documents", "Concerns", "Announcements"},
    "official": {"Dashboard", "Residents", "Documents", "Concerns", "Announcements"},
    "resident": {"Dashboard", "Documents", "Concerns", "Announcements", "Weather & Alerts"},
}
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
    if user and user.get("role") == "barangay_official":
        return DEPARTMENT_CATEGORIES.get(user.get("department"))
    return None

    
def current_user() -> dict | None:
    user = session.get("user")
    if user and user.get("role") == "official":
        user = {**user, "role": "barangay_official"}
        session["user"] = user
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
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                return jsonify({"error": "Authentication required"}), 401
            if user.get("role") not in roles:
                return jsonify({"error": "You do not have permission for this action"}), 403
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
        session["user"] = user
        session.permanent = True
        return jsonify({"data": user})
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
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
    return jsonify({"user": user, "access": sorted(ROLE_ACCESS.get(user["role"], set()))})


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
    if user.get("role") == "resident":
        return render_template("resident_dashboard.html")
    return render_template(
        "dashboard.html",
        dashboard_role=user.get("role"),
        dashboard_department=user.get("department"),
        official_mode=user.get("role") == "barangay_official",
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
@roles_required("admin")
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
@roles_required("admin", "barangay_official")
def create_announcement():
    payload = request.get_json(silent=True) or {}
    required = ("category", "title", "audience")
    if any(not payload.get(field) for field in required):
        return jsonify({"error": "Category, title, and audience are required"}), 400
    category = str(payload["category"]).strip()
    title = str(payload["title"]).strip()
    description = str(payload.get("description", "")).strip() or None
    audience = str(payload["audience"]).strip()
    if category not in ANNOUNCEMENT_CATEGORIES:
        return jsonify({"error": "Choose a valid announcement category"}), 400
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
@roles_required("admin", "barangay_official")
def update_announcement(announcement_id: int):
    payload = request.get_json(silent=True) or {}
    status = str(payload.get("status", "")).strip()
    if status not in {"Active", "Expired"}:
        return jsonify({"error": "Invalid announcement status"}), 400
    try:
        return jsonify({"data": supabase_repository.update_row("announcements", announcement_id, {"status": status})})
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
@roles_required("admin", "barangay_official")
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


@app.get("/api/reports/<report_name>")
@roles_required("admin", "barangay_official")
def download_report(report_name: str):
    if not supabase_repository.server_configured:
        return server_data_unavailable()
    categories = current_department_categories(current_user())
    try:
        if report_name == "residents":
            rows = supabase_repository.list_residents()
        elif report_name == "documents":
            rows = supabase_repository.list_table("documents")
        elif report_name == "concerns":
            rows = supabase_repository.list_table("concerns", categories=categories)
        elif report_name == "weather-alerts":
            rows = supabase_repository.list_table("weather_alerts")
        elif report_name == "demographics":
            summary = supabase_repository.dashboard_summary(categories)
            rows = [{"metric": key, "value": value} for key, value in summary["age_groups"].items()]
        elif report_name == "audit":
            summary = supabase_repository.dashboard_summary(categories)
            rows = [
                {"metric": "total_residents", "value": summary["total_residents"]},
                {"metric": "documents_total", "value": summary["documents_total"]},
                {"metric": "open_concerns", "value": summary["open_concerns"]},
            ]
        else:
            return jsonify({"error": "Unknown report"}), 404
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
    except Exception as error:
        app.logger.exception("Could not generate report")
        return jsonify({"error": str(error)}), 502

    output = io.StringIO()
    if rows:
        fieldnames = list(rows[0].keys())
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    else:
        output.write("No records found\n")
    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="smartbarangay-{report_name}.csv"'
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
        "current": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,visibility,weather_code",
        "hourly": "precipitation_probability,precipitation",
        "timezone": "auto",
    }
    try:
        with httpx.Client(timeout=6.0) as client:
            resp = client.get("https://api.open-meteo.com/v1/forecast", params=params)
            resp.raise_for_status()
            weather_data = resp.json()
            is_default = (abs(latitude - default_lat) < 0.01 and abs(longitude - default_lon) < 0.01)
            weather_data["location"] = "Barangay Don Felipe Larrazabal, Ormoc City" if is_default else "Detected Location"
            return jsonify({"data": weather_data})
    except Exception as error:
        fallback = dict(DEFAULT_BARANGAY_WEATHER)
        fallback["error_note"] = str(error)
        return jsonify({"data": fallback, "fallback": True})


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
@roles_required("admin", "barangay_official")
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
@roles_required("admin", "barangay_official")
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
@roles_required("admin", "barangay_official")
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
        resolved_email = (
            str(user.get("email") or "").strip()
            if is_resident and user.get("email")
            else supabase_repository._resolve_account_email(None, resident_name)
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
        if not is_resident and user["role"] not in {"admin", "barangay_official"}:
            return jsonify({"error": "You do not have permission to create document requests"}), 403
        return jsonify({"data": supabase_repository.insert_row("documents", data)}), 201
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
    except Exception as error:
        app.logger.exception("Could not create document request")
        return jsonify({"error": str(error)}), 502


def _resolve_resident_email(doc: dict) -> str:
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
    cur = str(doc.get("resident_email") or "").strip()
    if cur and cur.lower() not in fake_demo_emails:
        return cur
    return supabase_repository._resolve_account_email(
        doc.get("requester_id"),
        doc.get("resident_name"),
    )


def _send_gmail_notification(to_email: str, subject: str, body: str, resident_name: str | None = None) -> dict:
    import html as html_lib
    import smtplib
    from email.message import EmailMessage
    from email.utils import formataddr, formatdate, make_msgid

    smtp_user = os.getenv("SMTP_EMAIL", "").strip()
    smtp_pass = os.getenv("SMTP_PASSWORD", "").replace(" ", "").strip()
    clean_subject = subject.replace("—", "-").replace("–", "-")
    clean_body = body.replace("•", "-").replace("—", "-")
    recipients = [addr.strip() for addr in str(to_email or "").split(",") if addr.strip()]
    delivery = "Resident Portal Notification (SMTP Not Configured)"
    smtp_delivered = False
    smtp_error = None
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

            with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(smtp_user, smtp_pass)
                refused = server.send_message(msg, from_addr=smtp_user, to_addrs=recipients)
                if refused:
                    raise RuntimeError(f"Recipient refused by server: {refused}")
            delivery = f"Sent from {smtp_user} to {', '.join(recipients)}"
            smtp_delivered = True
            print(f"[SMTP DELIVERED] From {smtp_user} -> To {recipients} | Subject: {clean_subject}", flush=True)
        except Exception as err:
            smtp_error = str(err)
            delivery = f"Portal Notification Only (SMTP Error: {smtp_error})"
            print(f"[SMTP ERROR] {smtp_error}", flush=True)
    return {
        "from": smtp_user or "SmartBarangay System",
        "to": ", ".join(recipients) if recipients else to_email,
        "resident_name": resident_name or "Resident",
        "subject": clean_subject,
        "body": clean_body,
        "sent_at": manila_now().isoformat(),
        "delivery": delivery,
        "smtp_delivered": smtp_delivered,
        "smtp_configured": bool(smtp_user and smtp_pass),
        "smtp_error": smtp_error,
    }


@app.get("/api/settings/smtp")
@roles_required("admin", "barangay_official")
def get_smtp_settings():
    smtp_user = os.getenv("SMTP_EMAIL", "").strip()
    smtp_pass = os.getenv("SMTP_PASSWORD", "").replace(" ", "").strip()
    return jsonify({
        "configured": bool(smtp_user and smtp_pass),
        "smtp_email": smtp_user,
    })


@app.post("/api/settings/smtp")
@roles_required("admin", "barangay_official")
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
@roles_required("admin", "barangay_official")
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
        to_email = str(payload.get("resident_email") or _resolve_resident_email(doc)).strip()

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
            f"Once payment is completed, please log in to your SmartBarangay Resident Portal and upload your payment receipt / proof of payment referencing {tx_ref}.\n\n"
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
@roles_required("admin", "barangay_official")
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

        subject = f"[SmartBarangay] Payment Verified for {resident_name} — {doc.get('document_type', 'Certificate')} Approved ({tx_ref})"
        body = (
            f"Dear {resident_name},\n\n"
            f"We are pleased to inform you that your payment has been verified by {official_name}.\n\n"
            f"PAYMENT CONFIRMATION SUMMARY:\n"
            f"• Applicant / Resident Name: {resident_name}\n"
            f"• Transaction Reference: {tx_ref}\n"
            f"• Receipt Reference: {doc.get('receipt_ref_no') or 'Verified'}\n"
            f"• Amount Verified: PHP {paid_float:.2f}\n"
            f"• Payment Verification Status: VERIFIED\n"
            f"• Document Status: APPROVED & READY FOR DOWNLOAD\n\n"
            f"You may now log in to your SmartBarangay Resident Portal and click 'Download PDF' to get your official {doc.get('document_type', 'Barangay Certificate')}.\n\n"
            f"Thank you,\n"
            f"Office of the Punong Barangay — Hon. Nenito S. Anonat\n"
            f"Barangay Don Felipe Larrazabal, Ormoc City, Leyte"
        )
        email_entry = _send_gmail_notification(to_email, subject, body, resident_name=resident_name)
        email_logs.insert(0, email_entry)
        tx_history.insert(0, {
            "timestamp": manila_now().isoformat(),
            "actor": official_name,
            "event": "Payment Verified & Confirmation Gmail Sent",
            "details": f"Verified ₱{paid_float:.2f} for {resident_name} (Ref: {doc.get('receipt_ref_no') or tx_ref}). Sent confirmation email to {to_email}.",
        })
        updates["email_logs"] = email_logs
        updates["transaction_history"] = tx_history
    elif payload.get("payment_status") == "Rejected":
        updates["payment_status"] = "Rejected"
        reason = str(payload.get("verification_remarks") or "Submitted payment receipt could not be validated.").strip()
        subject = f"[SmartBarangay] Payment Receipt Update for {resident_name} — {doc.get('document_type', 'Certificate')} ({tx_ref})"
        body = (
            f"Dear {resident_name},\n\n"
            f"Your uploaded payment receipt for {doc.get('document_type', 'your document request')} ({tx_ref}) could not be verified.\n"
            f"Reason / Official Remarks: {reason}\n\n"
            f"Please log in to the SmartBarangay Resident Portal and upload a valid proof of payment.\n\n"
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


def _certificate_pdf(document: dict) -> bytes:
    styles = getSampleStyleSheet()

    header_style = ParagraphStyle(
        "CertificateHeader",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10.5,
        leading=14.5,
        alignment=TA_CENTER,
        textColor=colors.black,
    )
    office_style = ParagraphStyle(
        "CertificateOffice",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=16,
        alignment=TA_CENTER,
        textColor=colors.black,
    )
    title_style = ParagraphStyle(
        "CertificateTitle",
        parent=styles["Normal"],
        fontName="Helvetica-BoldOblique",
        fontSize=14,
        leading=18,
        alignment=TA_CENTER,
        textColor=colors.black,
    )
    to_whom_style = ParagraphStyle(
        "CertificateToWhom",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=16,
        alignment=TA_LEFT,
        textColor=colors.black,
    )
    body_style = ParagraphStyle(
        "CertificateBody",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=11,
        leading=18,
        alignment=TA_JUSTIFY,
        firstLineIndent=36,
        textColor=colors.black,
    )
    captain_name_style = ParagraphStyle(
        "CertificateCaptainName",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=15,
        alignment=TA_CENTER,
        textColor=colors.black,
    )
    captain_sub_style = ParagraphStyle(
        "CertificateCaptainSub",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10.5,
        leading=14,
        alignment=TA_CENTER,
        textColor=colors.black,
    )
    sig_label_style = ParagraphStyle(
        "CertificateSigLabel",
        parent=styles["Normal"],
        fontName="Helvetica-Oblique",
        fontSize=9,
        leading=12,
        alignment=TA_CENTER,
        textColor=colors.black,
    )
    bottom_left_style = ParagraphStyle(
        "CertificateBottomLeft",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=15,
        alignment=TA_LEFT,
        textColor=colors.black,
    )
    dry_seal_style = ParagraphStyle(
        "CertificateDrySeal",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        alignment=TA_CENTER,
        textColor=colors.black,
    )

    def text(value: object, fallback: str = "") -> str:
        return xml_escape(str(value).strip()) if value not in (None, "") else fallback

    document_type = str(document.get("document_type", "Barangay Clearance"))
    full_name = text(document.get("resident_name"), "RESIDENT NAME").upper()
    purpose = text(document.get("purpose"), "").strip()
    request_id = text(document.get("request_id"), "")

    today = manila_today()
    day = today.day
    if 11 <= (day % 100) <= 13:
        ord_suffix = "th"
    else:
        ord_suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    issue_date_phrase = f"Issued this {day}<sup>{ord_suffix}</sup> day of {today.strftime('%B')} {today.year} at Barangay Don Felipe Larrazabal, Ormoc City, Leyte."

    if document_type == "Barangay Clearance":
        cert_title = "BARANGAY CLEARANCE"
        p1 = (
            f"This is to certify that <u><b>{full_name}</b></u> of legal age, "
            "male/female and a bonafide resident of Barangay Don Felipe Larrazabal, Ormoc City, Leyte and "
            "he/she is known to me and is a person of good moral character, peaceful and "
            "law-abiding citizen."
        )
        if purpose and purpose.lower() not in ("not provided", "none", "general", "whatever legal purposes it may serve"):
            p2 = f"This certification is issued upon the request of above-mentioned name for {purpose} and whatever legal purposes it may serve."
        else:
            p2 = "This certification is issued upon the request of above-mentioned name for whatever legal purposes it may serve."
    elif document_type == "Certificate of Residency":
        cert_title = "CERTIFICATE OF RESIDENCY"
        p1 = (
            f"This is to certify that <u><b>{full_name}</b></u> of legal age, "
            "male/female is a bona fide resident of Barangay Don Felipe Larrazabal, Ormoc City, Leyte."
        )
        if purpose and purpose.lower() not in ("not provided", "none", "general", "whatever legal purposes it may serve"):
            p2 = f"This certification is issued upon the request of above-mentioned name for {purpose}."
        else:
            p2 = "This certification is issued upon the request of above-mentioned name for whatever legal purposes it may serve."
    elif document_type == "Certificate of Indigency":
        cert_title = "CERTIFICATE OF INDIGENCY"
        p1 = (
            f"This is to certify that <u><b>{full_name}</b></u> of legal age, "
            "male/female is a bona fide resident of Barangay Don Felipe Larrazabal, Ormoc City, Leyte, "
            "and belongs to an indigent family in this barangay."
        )
        if purpose and purpose.lower() not in ("not provided", "none", "general", "whatever legal purposes it may serve"):
            p2 = f"This certification is issued upon the request of above-mentioned name for {purpose}."
        else:
            p2 = "This certification is issued upon the request of above-mentioned name for whatever legal assistance it may serve."
    else:
        cert_title = "BARANGAY BUSINESS CLEARANCE"
        business = text(document.get("business_name"), "BUSINESS NAME").upper()
        business_address = text(document.get("business_address"), "Barangay Don Felipe Larrazabal, Ormoc City, Leyte").upper()
        p1 = (
            f"This is to certify that <u><b>{full_name}</b></u> is hereby granted clearance to operate or register "
            f"the business <b>{business}</b> located at <b>{business_address}</b>, subject to applicable laws, "
            "ordinances, and barangay regulations."
        )
        if purpose and purpose.lower() not in ("not provided", "none", "general", "whatever legal purposes it may serve"):
            p2 = f"This certification is issued upon the request of above-mentioned name for {purpose}."
        else:
            p2 = "This certification is issued upon the request of above-mentioned name for business clearance requirements."

    buffer = io.BytesIO()
    pdf = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        topMargin=0.65 * inch,
        bottomMargin=0.65 * inch,
        title=f"{cert_title} - {request_id}",
        author="SmartBarangay",
    )

    usable_width = A4[0] - 1.5 * inch

    barangay_logo_path = os.path.join(app.root_path, "public", "static", "barangay_logo.png")
    municipality_logo_path = os.path.join(app.root_path, "public", "static", "municipality_logo.png")

    b_logo = RLImage(barangay_logo_path, width=68, height=68) if os.path.exists(barangay_logo_path) else ""
    m_logo = RLImage(municipality_logo_path, width=68, height=68) if os.path.exists(municipality_logo_path) else ""

    header_text = Paragraph(
        "Republic of the Philippines<br/>"
        "Province of Leyte<br/>"
        "City of Ormoc<br/>"
        "Barangay Don Felipe Larrazabal",
        header_style,
    )

    header_table = Table(
        [[b_logo, header_text, m_logo]],
        colWidths=[78, usable_width - 156, 78],
        hAlign="CENTER",
    )
    header_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (0, 0), "LEFT"),
                ("ALIGN", (1, 0), (1, 0), "CENTER"),
                ("ALIGN", (2, 0), (2, 0), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )

    captain_table = Table(
        [
            [Paragraph("<u><b>HON. NENITO S. ANONAT</b></u>", captain_name_style)],
            [Paragraph("Punong Barangay", captain_sub_style)],
        ],
        colWidths=[240],
        hAlign="RIGHT",
    )
    captain_table.setStyle(
        TableStyle(
            [
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]
        )
    )

    sig_table = Table(
        [
            [Paragraph("________________________", captain_name_style)],
            [Paragraph("Signature over printed name", sig_label_style)],
        ],
        colWidths=[200],
        hAlign="LEFT",
    )
    sig_table.setStyle(
        TableStyle(
            [
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]
        )
    )

    sig_grid = Table(
        [
            ["", captain_table],
            [sig_table, ""],
        ],
        colWidths=[usable_width * 0.48, usable_width * 0.52],
        hAlign="CENTER",
    )
    sig_grid.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )

    bottom_left = Paragraph(
        "Comm. Tax Cert. No.: __________<br/>"
        "Issued On: ____________________<br/>"
        "O.R. No.: _____________________<br/>"
        "Issued On: ____________________",
        bottom_left_style,
    )

    dry_seal = Paragraph(
        "Not valid<br/>without<br/>dry seal",
        dry_seal_style,
    )

    bottom_table = Table(
        [[bottom_left, dry_seal]],
        colWidths=[usable_width * 0.7, usable_width * 0.3],
        hAlign="CENTER",
    )
    bottom_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )

    story = [
        header_table,
        Spacer(1, 22),
        Paragraph("OFFICE OF THE PUNONG BARANGAY", office_style),
        Spacer(1, 16),
        Paragraph(f"<i><b>{cert_title}</b></i>", title_style),
        Spacer(1, 32),
        Paragraph("<b>TO WHOM IT MAY CONCERN:</b>", to_whom_style),
        Spacer(1, 18),
        Paragraph(p1, body_style),
        Spacer(1, 18),
        Paragraph(p2, body_style),
        Spacer(1, 18),
        Paragraph(issue_date_phrase, body_style),
        Spacer(1, 45),
        sig_grid,
        Spacer(1, 40),
        bottom_table,
    ]

    pdf.build(story)
    return buffer.getvalue()


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
        response.headers["Content-Disposition"] = f'attachment; filename="{document_slug}-{request_id}.pdf"'
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
@roles_required("admin", "barangay_official")
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
@roles_required("admin")
def register_account():
    payload = request.get_json(silent=True) or {}
    required = ("email", "password", "full_name", "role")
    missing = [field for field in required if not payload.get(field)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400
    if payload["role"] not in ("resident", "barangay_official"):
        return jsonify({"error": "Admin can register residents or barangay officials only"}), 400
    department = str(payload.get("department", "")).strip() or None
    if payload["role"] == "barangay_official" and department not in DEPARTMENT_CATEGORIES:
        return jsonify({"error": "Choose a valid barangay official department"}), 400
    position = str(payload.get("position", "")).strip() or None
    phone = str(payload.get("phone", "")).strip() or None
    try:
        user = supabase_repository.register_account(
            email=str(payload["email"]).strip().lower(),
            password=str(payload["password"]),
            full_name=str(payload["full_name"]).strip(),
            role=str(payload["role"]),
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
    app.run(
        host=os.getenv("FLASK_HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
    )
