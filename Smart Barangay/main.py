
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
    if response.content_type.startswith("text/html"):
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
CIVIL_STATUSES = {"Single", "Married", "Widowed", "Separated", "Divorced"}


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


@app.post("/api/residents")
@roles_required("admin", "barangay_official")
def create_resident():
    payload = request.get_json(silent=True) or {}
    required_fields = ("full_name", "address", "age", "gender")
    missing_fields = [field for field in required_fields if not str(payload.get(field, "")).strip()]
    if missing_fields:
        return jsonify({"error": f"Missing fields: {', '.join(missing_fields)}"}), 400
    try:
        age = int(payload["age"])
        gender = str(payload["gender"]).strip()
        if age < 1 or age > 120:
            return jsonify({"error": "Age must be between 1 and 120"}), 400
        if gender not in {"Female", "Male", "Other"}:
            return jsonify({"error": "Choose a valid gender"}), 400
        is_head = bool(payload.get("is_head", False))
        household_role = str(payload.get("household_role", "Head" if is_head else "Member")).strip()
        if household_role.lower() in ("head", "household leader", "leader"):
            is_head = True
            household_role = "Head"
        is_pwd = bool(payload.get("is_pwd", False))
        is_solo_parent = bool(payload.get("is_solo_parent", False))
        is_4ps = bool(payload.get("is_4ps", False))
        sector = str(payload.get("sector", "")).strip() or None
        if not sector:
            sectors = []
            if is_head: sectors.append("Leader")
            if is_pwd: sectors.append("PWD")
            if is_solo_parent: sectors.append("Solo Parent")
            if is_4ps: sectors.append("4Ps")
            if age >= 60: sectors.append("Senior")
            elif age < 18: sectors.append("Minor")
            else: sectors.append("Adult")
            sector = ", ".join(sectors)

        resident = supabase_repository.add_resident({
            "full_name": str(payload["full_name"]).strip(),
            "address": str(payload["address"]).strip(),
            "household": str(payload.get("household", "UNASSIGNED")).strip() or "UNASSIGNED",
            "household_role": household_role,
            "is_head": is_head,
            "is_pwd": is_pwd,
            "is_solo_parent": is_solo_parent,
            "is_4ps": is_4ps,
            "sector": sector,
            "email": str(payload.get("email", "")).strip() or None,
            "phone": str(payload.get("phone", "")).strip() or None,
            "age": age,
            "gender": gender,
            "resident_since": manila_today().isoformat(),
            "status": "Pending",
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
    updates: dict[str, object] = {}
    for field in ("full_name", "address", "household", "email", "phone", "gender", "household_role", "sector"):
        if field in payload:
            value = str(payload[field] or "").strip()
            updates[field] = value or None
    if "age" in payload:
        try:
            updates["age"] = int(payload["age"])
        except (ValueError, TypeError):
            pass
    if "is_head" in payload:
        updates["is_head"] = bool(payload["is_head"])
        if updates["is_head"] and not updates.get("household_role"):
            updates["household_role"] = "Head"
    if "household_role" in payload:
        role_val = str(payload["household_role"]).strip()
        updates["household_role"] = role_val
        if role_val.lower() in ("head", "household leader", "leader"):
            updates["is_head"] = True
    if "is_pwd" in payload:
        updates["is_pwd"] = bool(payload["is_pwd"])
    if "is_solo_parent" in payload:
        updates["is_solo_parent"] = bool(payload["is_solo_parent"])
    if "is_4ps" in payload:
        updates["is_4ps"] = bool(payload["is_4ps"])
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
        data = {
            "document_type": document_type,
            "resident_name": resident_name,
            "requester_id": user["id"] if is_resident else None,
            "purpose": purpose,
            "applicant_address": applicant_address,
            "applicant_contact": applicant_contact,
            "applicant_date_of_birth": applicant_date_of_birth,
            "applicant_place_of_birth": applicant_place_of_birth,
            "applicant_civil_status": applicant_civil_status,
            "business_name": business_name,
            "business_address": business_address,
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


@app.patch("/api/documents/<int:document_id>")
@roles_required("admin", "barangay_official")
def update_document(document_id):
    payload = request.get_json(silent=True) or {}
    status = payload.get("status")
    if status not in DOCUMENT_STATUSES:
        return jsonify({"error": "Invalid document status"}), 400
    updates = {"status": status, "processor": current_user().get("full_name", current_user()["email"])}
    if "scheduled_date" in payload:
        updates["scheduled_date"] = payload["scheduled_date"] or None
    if "scheduled_time" in payload:
        updates["scheduled_time"] = payload["scheduled_time"] or None
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
@roles_required("admin", "barangay_official")
def download_certificate(document_id: int):
    try:
        document = supabase_repository.get_row("documents", document_id)
        if not document:
            return jsonify({"error": "Document request not found"}), 404
        if document.get("status") not in {"Approved", "Resolved"}:
            return jsonify({"error": "The certificate can only be downloaded after the request is approved"}), 409
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
    status = (request.get_json(silent=True) or {}).get("status")
    if status not in CONCERN_STATUSES:
        return jsonify({"error": "Invalid concern status"}), 400
    try:
        user = current_user()
        concern = supabase_repository.get_row("concerns", concern_id)
        if not concern:
            return jsonify({"error": "Concern not found"}), 404
        categories = current_department_categories(user)
        if categories and concern.get("category") not in categories:
            return jsonify({"error": "This concern is outside your assigned department"}), 403
        return jsonify({"data": supabase_repository.update_row("concerns", concern_id, {"status": status})})
    except RepositoryConfigurationError as error:
        return jsonify({"error": str(error), "configured": False}), 503
    except Exception as error:
        app.logger.exception("Could not update concern")
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
