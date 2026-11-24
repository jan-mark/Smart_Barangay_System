
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
    "super_admin": {"Dashboard", "Manage Officials", "Residents", "Documents", "Concerns", "Announcements", "Weather & Alerts", "Reports", "Settings"},
    "admin": {"Dashboard", "Residents", "Documents", "Concerns", "Announcements", "Weather & Alerts", "Reports", "Settings"},
    "barangay_official": {"Dashboard", "Residents", "Documents", "Concerns", "Announcements"},
    "official": {"Dashboard", "Residents", "Documents", "Concerns", "Announcements"},
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
    if user.get("role") not in {"super_admin", "admin", "barangay_official"}:
        session.clear()
        return redirect(url_for("login"))
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
@roles_required("super_admin")
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


@app.post("/api/weather/auto-alert")
@roles_required("admin", "barangay_official")
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
    for email_key in ("resident_email", "email", "applicant_email", "requester_email", "contact_email"):
        cur = str(doc.get(email_key) or "").strip()
        if cur and cur.lower() not in fake_demo_emails:
            return cur
    return supabase_repository._resolve_account_email(
        doc.get("requester_id") or doc.get("resident_id"),
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
    delivery = "SmartBarangay Mobile App Notification (SMTP Not Configured)"
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
            f"You may now open the SmartBarangay Mobile App and download your official {doc.get('document_type', 'Barangay Certificate')}.\n\n"
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
@roles_required("super_admin")
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
    # Barangay Secretary receives Admin role; all other registered officials are Authorized Users (barangay_official)
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
