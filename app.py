import os
import json
import secrets
import tempfile
import uuid
import zipfile
from functools import wraps

import qrcode
import qrcode.image.svg
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file, g
from markupsafe import Markup
from supabase import create_client, Client
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta, timezone
from ml_models.forecaster import InventoryForecaster

# Load variables from .env
load_dotenv()


app = Flask(__name__)

# Flask session security
app.secret_key = os.getenv("FLASK_SECRET_KEY")


# =========================
# SUPABASE CONNECTION
# =========================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_PUBLISHABLE_KEY")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

app.secret_key = os.getenv("FLASK_SECRET_KEY")
APP_URL = os.getenv("APP_URL")
supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_KEY
)


supabase_admin: Client = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_ROLE_KEY
)

BACKUP_TABLES = (
    "profiles",
    "dental_cases",
    "case_teeth",
    "case_status_history",
    "case_files",
    "inventory",
    "inventory_logs",
    "material_requests",
    "notifications",
    "login_history",
)
BACKUP_STORAGE_BUCKETS = ("dental-case-files",)
LOGIN_HISTORY_PAGE_SIZE = 100
LOGIN_EVENT_TYPES = ("all", "success", "failed", "blocked", "logout")
LOGIN_HISTORY_PERIODS = {"all": None, "24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
LOGIN_REASON_TEXT = {
    "missing_credentials": "Email or password left blank",
    "invalid_credentials": "Wrong email or password",
    "authentication_error": "Sign-in service error",
    "profile_not_found": "No DentaLink profile for this account",
    "account_pending": "Account awaiting approval",
    "account_rejected": "Account application rejected",
    "account_not_approved": "Account not approved",
    "invalid_account_role": "Account has an invalid role",
}

# Case lifecycle after the bench stages:
# finishing_and_quality_check -> pending_admin_review -> ready_for_dispatch -> in_transit -> delivered
DISPATCH_STATUSES = ("ready_for_dispatch", "in_transit")
# "completed" is the terminal status used before QR dispatch existed.
DELIVERED_STATUSES = ("delivered", "completed")
CLOSED_STATUSES = ("draft", "submitted", "cancelled") + DELIVERED_STATUSES
# Stages a technician works through on the bench; only cases here can change stage or be returned.
BENCH_STATUSES = (
    "assigned", "in_progress", "cad_designing", "wax_up_or_try_in",
    "milling_or_printing", "packing_and_curing", "finishing_and_quality_check",
)
REASSIGNABLE_STATUSES = BENCH_STATUSES + ("needs_revision", "pending_admin_review")

# The lab and its clinics are in the Philippines (UTC+8, no daylight saving).
LAB_TZ = timezone(timedelta(hours=8))
ISSUE_REPORT_WINDOW_DAYS = 14
ISSUE_TYPES = {
    "poor_fit": "Poor fit",
    "wrong_shade": "Wrong shade",
    "broken": "Broken or chipped",
    "wrong_case": "Wrong case received",
    "other": "Other problem",
}
ISSUE_DECISIONS = {"remake_accepted": "Remake accepted", "no_action": "No remake needed"}

import urllib.request
import threading

def send_email_in_background(recipient_email, user_name, action_type):
    try:
        api_key = os.getenv("BREVO_SMTP_PASS") 
        sender_email = os.getenv("BREVO_SENDER_EMAIL", "jiclao24@gmail.com")
        app_url = os.getenv("APP_URL", "http://localhost:5000").rstrip("/")

        if not api_key or not recipient_email:
            print("⚠️ Brevo API key or recipient email missing.")
            return

        is_approved = (action_type == "approved")
        subject = "✅ Welcome to DentaLink LIS - Account Approved" if is_approved else "Notice Regarding Your DentaLink LIS Account Application"
        template_file = "email_approved.html" if is_approved else "email_rejected.html"

        with app.app_context():
            html_content = render_template(template_file, full_name=user_name, app_url=app_url)

        payload = {
            "sender": {"name": "DentaLink LIS", "email": sender_email},
            "to": [{"email": recipient_email, "name": user_name}],
            "subject": subject,
            "htmlContent": html_content
        }

        req = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "accept": "application/json",
                "api-key": api_key,
                "content-type": "application/json"
            },
            method="POST"
        )

        print(f"📧 [Background] Sending email via Brevo HTTP API to {recipient_email}...")
        with urllib.request.urlopen(req) as response:
            res_body = json.loads(response.read().decode("utf-8"))
            print(f"✅ [Background] Email successfully sent! Message ID: {res_body.get('messageId')}")

    except Exception as err:
        print(f"❌ [Background] Brevo HTTP API error: {str(err)}")


@app.before_request
def update_user_activity():
    user_id = session.get("user_id")
    if user_id:
        # Only update once every minute to prevent spamming database queries on every click
        last_update = session.get("last_activity_update")
        now = datetime.utcnow()
        
        if not last_update or (now - datetime.fromisoformat(last_update)) > timedelta(minutes=1):
            try:
                supabase_admin.table("profiles").update({"last_seen": now.isoformat()}).eq("id", user_id).execute()
                session["last_activity_update"] = now.isoformat()
            except Exception:
                pass
# =========================
# HOME PAGE
# =========================

@app.route("/")
def home():

    return redirect(url_for("login"))

# =========================
# ADMIN DASHBOARD
# =========================
# =========================
# ADMIN DASHBOARD
# =========================

# =========================
# ADMIN DASHBOARD
# =========================

# =========================
# ADMIN DASHBOARD
# =========================

# =========================
# ADMIN DASHBOARD (UPDATED)
# =========================
@app.route("/admin")
def admin_dashboard():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    try:
        # ==========================================
        # 1. VERIFY ADMIN ACCESS
        # ==========================================
        response = supabase_admin.table("profiles").select("*").eq("id", user_id).execute()
        profiles = response.data or []
        if not profiles:
            session.clear()
            return "Profile not found.", 404

        profile = profiles[0]

        if profile.get("role") != "admin" or profile.get("status") != "approved":
            return "Access denied.", 403

        # ==========================================
        # 2. UPDATE ACTIVE SESSION HEARTBEAT
        # ==========================================
        from datetime import datetime, timedelta
        now_iso = datetime.utcnow().isoformat()
        try:
            supabase_admin.table("profiles").update({"last_seen": now_iso}).eq("id", user_id).execute()
        except Exception:
            pass

        # ==========================================
        # 3. DIRECTORY USERS & ONLINE METRIC
        # ==========================================
        all_response = supabase_admin.table("profiles").select("*").order("created_at", desc=True).execute()
        all_users = all_response.data or []

        pending_users = [u for u in all_users if u.get("status") == "pending"]
        approved_users = [u for u in all_users if u.get("status") == "approved"]
        rejected_users = [u for u in all_users if u.get("status") == "rejected"]

        five_mins_ago = (datetime.utcnow() - timedelta(minutes=5)).isoformat()
        online_res = (
            supabase_admin
            .table("profiles")
            .select("id", count="exact")
            .gte("last_seen", five_mins_ago)
            .execute()
        )
        online_count = online_res.count or 0

        # Quick lookup map for user profiles (id -> profile)
        users_map = {u["id"]: u for u in all_users}

        # ==========================================
        # 4. RESTOCK REQUESTS (MATERIAL REQUESTS)
        # ==========================================
        restock_res = (
            supabase_admin
            .table("material_requests")
            .select("*, profiles(display_name), dental_cases(case_number)")
            .eq("status", "pending")
            .order("created_at", desc=True)
            .execute()
        )
        restock_requests = restock_res.data or []

        # ==========================================
        # 5. LAB PERFORMANCE METRICS
        # ==========================================
        now_str = datetime.now().strftime("%Y-%m-%d")
        month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        delivered_res = (
            supabase_admin
            .table("dental_cases")
            .select("id", count="exact")
            .eq("status", "delivered")
            .gte("delivered_at", month_start.isoformat())
            .execute()
        )
        delivered_this_month = delivered_res.count or 0

        overdue_query = (
            supabase_admin
            .table("dental_cases")
            .select("id", count="exact")
            .lt("due_date", now_str)
        )
        for closed_status in ("cancelled",) + DELIVERED_STATUSES:
            overdue_query = overdue_query.neq("status", closed_status)
        overdue_res = overdue_query.execute()
        overdue_count = overdue_res.count or 0

        # ==========================================
        # 6. UNASSIGNED CASES & CASE ATTACHMENTS
        # ==========================================
        unassigned_res = (
            supabase_admin
            .table("dental_cases")
            .select("*")
            .eq("status", "submitted")
            .order("created_at", desc=True)
            .execute()
        )
        unassigned_cases = unassigned_res.data or []
        unassigned_ids = [c["id"] for c in unassigned_cases]

        # Bulk fetch teeth & signed file attachments for unassigned cases
        if unassigned_ids:
            unassigned_teeth_res = supabase_admin.table("case_teeth").select("*").in_("case_id", unassigned_ids).execute()
            teeth_by_case = {}
            for t in (unassigned_teeth_res.data or []):
                teeth_by_case.setdefault(t["case_id"], []).append(t)
            for c in unassigned_cases:
                c["teeth"] = teeth_by_case.get(c["id"], [])

            files_res = supabase_admin.table("case_files").select("*").in_("case_id", unassigned_ids).execute()
            files_by_case = {}
            for f in (files_res.data or []):
                try:
                    signed_url_res = supabase_admin.storage.from_("dental-case-files").create_signed_url(f["file_path"], 3600)
                    f["download_url"] = signed_url_res.get("signedURL") or signed_url_res.get("signed_url")
                except Exception:
                    f["download_url"] = "#"
                files_by_case.setdefault(f["case_id"], []).append(f)

            for c in unassigned_cases:
                c["files"] = files_by_case.get(c["id"], [])
        else:
            for c in unassigned_cases:
                c["teeth"] = []
                c["files"] = []

        # ==========================================
        # 7. APPROVED TECHNICIANS & WORKLOADS
        # ==========================================
        technicians = [u for u in approved_users if u.get("role") == "technician"]
        tech_ids = [t["id"] for t in technicians]

        active_statuses = [
            "assigned", "in_progress", "cad_designing", "wax_up_or_try_in",
            "milling_or_printing", "packing_and_curing", "finishing_and_quality_check", "needs_revision"
        ]

        if tech_ids:
            workload_cases_res = (
                supabase_admin
                .table("dental_cases")
                .select("technician_id")
                .in_("technician_id", tech_ids)
                .in_("status", active_statuses)
                .execute()
            )
            workload_counts = {}
            for w in (workload_cases_res.data or []):
                t_id = w.get("technician_id")
                workload_counts[t_id] = workload_counts.get(t_id, 0) + 1

            for t in technicians:
                t["active_workload"] = workload_counts.get(t["id"], 0)
        else:
            for t in technicians:
                t["active_workload"] = 0

        # ==========================================
        # 8. ACTIVE WORKFLOW CASES WITH TEETH & CLINIC
        # ==========================================
        active_cases_query = supabase_admin.table("dental_cases").select("*")
        for closed_status in CLOSED_STATUSES:
            active_cases_query = active_cases_query.neq("status", closed_status)
        active_cases = active_cases_query.order("created_at", desc=True).execute().data or []
        active_case_ids = [c["id"] for c in active_cases]

        # Bulk fetch teeth for active cases
        if active_case_ids:
            active_teeth_res = supabase_admin.table("case_teeth").select("*").in_("case_id", active_case_ids).execute()
            active_teeth_by_case = {}
            for t in (active_teeth_res.data or []):
                active_teeth_by_case.setdefault(t["case_id"], []).append(t)
        else:
            active_teeth_by_case = {}

        for case in active_cases:
            case["teeth"] = active_teeth_by_case.get(case["id"], [])

            dentist_id = case.get("dentist_id")
            dentist_profile = users_map.get(dentist_id, {})

            tech_id = case.get("technician_id")
            tech_profile = users_map.get(tech_id, {})

            case["profiles"] = {
                "display_name": tech_profile.get("display_name") or tech_profile.get("first_name") or "Unassigned",
                "clinic_name": dentist_profile.get("clinic_name") or "Independent Practice",
                "dentist_name": dentist_profile.get("display_name") or "Clinical Partner"
            }

        # Stage slices for the workflow tabs, audio triggers and quick template bindings
        assigned_cases = [c for c in active_cases if c.get("status") == "assigned"]
        setup_cases = [c for c in active_cases if c.get("status") in ("in_progress", "cad_designing", "wax_up_or_try_in")]
        milling_cases = [c for c in active_cases if c.get("status") == "milling_or_printing"]
        packing_cases = [c for c in active_cases if c.get("status") == "packing_and_curing"]
        qc_cases = [c for c in active_cases if c.get("status") == "finishing_and_quality_check"]
        review_cases = [c for c in active_cases if c.get("status") == "pending_admin_review"]
        dispatch_cases = [c for c in active_cases if c.get("status") in DISPATCH_STATUSES]

        # Courier names for cases already handed off
        if dispatch_cases:
            dispatch_events = (
                supabase_admin
                .table("case_dispatch_events")
                .select("case_id, courier_name, created_at")
                .in_("case_id", [c["id"] for c in dispatch_cases])
                .eq("event_type", "dispatched")
                .order("created_at")
                .execute()
                .data
                or []
            )
            couriers_by_case = {e["case_id"]: e.get("courier_name") for e in dispatch_events}
            for c in dispatch_cases:
                c["courier_name"] = couriers_by_case.get(c["id"])

        # ==========================================
        # 9. RECENT NOTIFICATIONS
        # ==========================================
        notif_response = (
            supabase_admin
            .table("notifications")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(5)
            .execute()
        )
        notifications = notif_response.data or []

        return render_template(
            "admin_dashboard.html",
            profile=profile,
            pending_users=pending_users,
            approved_users=approved_users,
            rejected_users=rejected_users,
            pending_count=len(pending_users),
            approved_count=len(approved_users),
            rejected_count=len(rejected_users),
            total_count=len(all_users),
            delivered_this_month=delivered_this_month,
            overdue_count=overdue_count,
            unassigned_cases=unassigned_cases,
            technicians=technicians,
            active_cases=active_cases,
            assigned_cases=assigned_cases,
            setup_cases=setup_cases,
            milling_cases=milling_cases,
            packing_cases=packing_cases,
            qc_cases=qc_cases,
            review_cases=review_cases,
            dispatch_cases=dispatch_cases,
            notifications=notifications,
            online_count=online_count,
            open_issue_count=_open_issue_count(),
            restock_requests=restock_requests
        )

    except Exception as e:
        return f"Admin dashboard error: {str(e)}", 500


def _approved_admin_profile(user_id):
    profiles = (
        supabase_admin.table("profiles")
        .select("id, display_name, role, status")
        .eq("id", user_id)
        .execute()
        .data
        or []
    )
    if not profiles:
        return None

    profile = profiles[0]
    if profile.get("role") != "admin" or profile.get("status") != "approved":
        return None
    return profile


def _csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _valid_csrf_token():
    token = session.get("csrf_token")
    submitted = request.form.get("csrf_token", "")
    return bool(token and submitted and secrets.compare_digest(token, submitted))


@app.context_processor
def inject_csrf_token():
    if not session.get("user_id"):
        return {}
    return {"csrf_token": _csrf_token()}


def _safe_next_path(path):
    return (
        isinstance(path, str)
        and path.startswith("/")
        and not path.startswith("//")
        and "\\" not in path
    )


def role_required(*roles):
    """Require an approved account with one of `roles`; POSTs must carry a CSRF token.

    The approved profile is available to the view as `g.profile`.
    """
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user_id = session.get("user_id")
            if not user_id:
                next_path = request.full_path if request.query_string else request.path
                return redirect(url_for("login", next=next_path if request.method == "GET" else None))

            profiles = (
                supabase_admin.table("profiles")
                .select("id, display_name, first_name, last_name, clinic_name, role, status")
                .eq("id", user_id)
                .execute()
                .data
                or []
            )
            if not profiles or profiles[0].get("status") != "approved" or profiles[0].get("role") not in roles:
                return "Access denied.", 403
            if request.method == "POST" and not _valid_csrf_token():
                return "This form has expired. Reload the page and try again.", 400

            g.profile = profiles[0]
            return view(*args, **kwargs)
        return wrapped
    return decorator


def _record_login_event(event_type, email, user_id=None, reason=None):
    try:
        supabase_admin.table("login_history").insert({
            "user_id": user_id,
            "email": (email or "").strip().lower()[:320] or None,
            "event_type": event_type,
            "reason": reason,
            "session_id": session.get("login_session_id"),
            "ip_address": request.remote_addr,
            "user_agent": request.headers.get("User-Agent", "")[:512] or None,
        }).execute()
    except Exception:
        app.logger.exception("Unable to record login history event")


def _describe_user_agent(user_agent):
    """Summarize a browser user-agent string, e.g. "Chrome on Windows"."""
    if not user_agent:
        return None
    ua = user_agent.lower()
    if "edg/" in ua:
        browser = "Edge"
    elif "opr/" in ua or "opera" in ua:
        browser = "Opera"
    elif "samsungbrowser" in ua:
        browser = "Samsung Internet"
    elif "firefox/" in ua or "fxios" in ua:
        browser = "Firefox"
    elif "chrome/" in ua or "crios" in ua:
        browser = "Chrome"
    elif "safari/" in ua:
        browser = "Safari"
    elif "python" in ua or "curl" in ua or "postman" in ua:
        browser = "Script or tool"
    else:
        browser = "Unknown browser"
    if "iphone" in ua or "ipad" in ua:
        system = "iOS"
    elif "android" in ua:
        system = "Android"
    elif "windows" in ua:
        system = "Windows"
    elif "mac os x" in ua or "macintosh" in ua:
        system = "macOS"
    elif "linux" in ua:
        system = "Linux"
    else:
        return browser
    return f"{browser} on {system}"


def _backup_table_rows(table_name):
    rows = []
    offset = 0
    page_size = 1000
    while True:
        page = (
            supabase_admin.table(table_name)
            .select("*")
            .range(offset, offset + page_size - 1)
            .execute()
            .data
            or []
        )
        rows.extend(page)
        if len(page) < page_size:
            return rows
        offset += page_size


def _storage_objects(bucket, folder=""):
    offset = 0
    page_size = 100
    while True:
        entries = bucket.list(
            folder,
            {"limit": page_size, "offset": offset, "sortBy": {"column": "name", "order": "asc"}},
        )
        for entry in entries:
            name = entry.get("name")
            if not name:
                continue
            object_path = f"{folder}/{name}" if folder else name
            if entry.get("id") is None:
                yield from _storage_objects(bucket, object_path)
            else:
                yield object_path, entry
        if len(entries) < page_size:
            return
        offset += page_size


def _safe_archive_path(path):
    return (
        isinstance(path, str)
        and path
        and not path.startswith("/")
        and "\\" not in path
        and not any(ord(character) < 32 for character in path)
        and all(part not in ("", ".", "..") for part in path.split("/"))
    )


@app.route("/admin/backup")
def admin_backup():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    try:
        profile = _approved_admin_profile(user_id)
        if not profile:
            return "Access denied.", 403
        return render_template(
            "admin_backup.html",
            profile=profile,
            csrf_token=_csrf_token(),
        )
    except Exception:
        app.logger.exception("Unable to open the admin backup page")
        return "Unable to load backup page.", 500


@app.route("/admin/backup/download", methods=["POST"])
def admin_backup_download():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    try:
        if not _approved_admin_profile(user_id):
            return "Access denied.", 403
        if not _valid_csrf_token():
            return "Invalid backup request. Reload the page and try again.", 400

        backup_file = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024)
        created_at = datetime.utcnow().isoformat() + "Z"
        storage_manifest = []
        with zipfile.ZipFile(
            backup_file,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=1,
        ) as archive:
            for table_name in BACKUP_TABLES:
                rows = _backup_table_rows(table_name)
                archive.writestr(
                    f"database/{table_name}.json",
                    json.dumps(rows, ensure_ascii=False, default=str),
                )

            for bucket_name in BACKUP_STORAGE_BUCKETS:
                bucket = supabase_admin.storage.from_(bucket_name)
                for object_path, metadata in _storage_objects(bucket):
                    if not _safe_archive_path(object_path):
                        raise ValueError(f"Unsafe storage object path in {bucket_name}")
                    archive_path = f"storage/{bucket_name}/{object_path}"
                    archive.writestr(archive_path, bucket.download(object_path))
                    object_metadata = metadata.get("metadata") or {}
                    storage_manifest.append({
                        "bucket": bucket_name,
                        "path": object_path,
                        "content_type": object_metadata.get("mimetype")
                        or object_metadata.get("contentType")
                        or "application/octet-stream",
                    })

            manifest = {
                "format": "dentalink-app-backup",
                "version": 1,
                "created_at": created_at,
                "database_tables": list(BACKUP_TABLES),
                "storage_objects": storage_manifest,
                "configuration": {
                    "values": {
                        "APP_URL": APP_URL,
                        "SUPABASE_URL": SUPABASE_URL,
                    },
                    "redacted": [
                        "FLASK_SECRET_KEY",
                        "SUPABASE_PUBLISHABLE_KEY",
                        "SUPABASE_SERVICE_ROLE_KEY",
                        "BREVO_SMTP_PASS",
                    ],
                },
                "encrypted": False,
                "limitations": [
                    "This archive does not include database schema or Supabase Auth accounts.",
                    "Restore merges records into the existing schema and does not delete newer records.",
                ],
            }
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )

        backup_file.seek(0)
        filename = f"dentalink-backup-{datetime.utcnow():%Y%m%d-%H%M%S}.zip"
        response = send_file(
            backup_file,
            mimetype="application/zip",
            as_attachment=True,
            download_name=filename,
        )
        response.headers["Cache-Control"] = "no-store"
        return response
    except Exception:
        app.logger.exception("Unable to create an admin backup")
        return "Backup failed. Check the server logs for details.", 500


@app.route("/admin/backup/restore", methods=["POST"])
def admin_backup_restore():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    try:
        if not _approved_admin_profile(user_id):
            return "Access denied.", 403
        if not _valid_csrf_token():
            return "Invalid backup request. Reload the page and try again.", 400

        upload = request.files.get("backup_file")
        if not upload or not upload.filename:
            flash("Choose a DentaLink backup ZIP to restore.", "error")
            return redirect(url_for("admin_backup"))

        with zipfile.ZipFile(upload.stream) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or archive.testzip():
                raise ValueError("Backup archive is corrupt or contains duplicate files.")
            manifest = json.loads(archive.read("manifest.json"))
            manifest_tables = manifest.get("database_tables") if isinstance(manifest, dict) else None
            legacy_tables = [table for table in BACKUP_TABLES if table != "login_history"]
            if (
                not isinstance(manifest, dict)
                or manifest.get("format") != "dentalink-app-backup"
                or manifest.get("version") != 1
                or manifest_tables not in (list(BACKUP_TABLES), legacy_tables)
            ):
                raise ValueError("Unsupported or incomplete DentaLink backup.")

            database_rows = {}
            for table_name in manifest_tables:
                archive_path = f"database/{table_name}.json"
                if archive_path not in names:
                    raise ValueError(f"Backup is missing {archive_path}.")
                rows = json.loads(archive.read(archive_path))
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise ValueError(f"Invalid data in {archive_path}.")
                database_rows[table_name] = rows
            if "login_history" not in database_rows:
                database_rows["login_history"] = []

            storage_objects = manifest.get("storage_objects")
            if not isinstance(storage_objects, list):
                raise ValueError("Invalid storage manifest.")
            expected_storage_paths = set()
            for item in storage_objects:
                if not isinstance(item, dict):
                    raise ValueError("Invalid storage manifest entry.")
                bucket_name = item.get("bucket")
                object_path = item.get("path")
                content_type = item.get("content_type")
                if (
                    bucket_name not in BACKUP_STORAGE_BUCKETS
                    or not _safe_archive_path(object_path)
                    or (
                        content_type is not None
                        and (
                            not isinstance(content_type, str)
                            or "\r" in content_type
                            or "\n" in content_type
                        )
                    )
                ):
                    raise ValueError("Backup contains an invalid storage path.")
                archive_path = f"storage/{bucket_name}/{object_path}"
                if archive_path not in names or archive_path in expected_storage_paths:
                    raise ValueError("Backup storage listing does not match its files.")
                expected_storage_paths.add(archive_path)

            allowed_paths = {
                "manifest.json",
                *(f"database/{table}.json" for table in manifest_tables),
                *expected_storage_paths,
            }
            if set(names) != allowed_paths:
                raise ValueError("Backup contains unexpected files.")

            for table_name in BACKUP_TABLES:
                rows = database_rows[table_name]
                for start in range(0, len(rows), 500):
                    supabase_admin.table(table_name).upsert(rows[start:start + 500]).execute()

            for item in storage_objects:
                archive_path = f"storage/{item['bucket']}/{item['path']}"
                supabase_admin.storage.from_(item["bucket"]).upload(
                    item["path"],
                    archive.read(archive_path),
                    {
                        "upsert": "true",
                        "content-type": item.get("content_type") or "application/octet-stream",
                    },
                )

        flash(
            "Backup data restored. Existing records were merged; records absent from the backup were not deleted.",
            "success",
        )
        return redirect(url_for("admin_backup"))
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, ValueError) as error:
        flash(f"Backup could not be restored: {error}", "error")
        return redirect(url_for("admin_backup"))
    except Exception:
        app.logger.exception("Unable to restore an admin backup")
        flash(
            "Restore failed and may have applied some changes. Check server logs before retrying.",
            "error",
        )
        return redirect(url_for("admin_backup"))


@app.route("/admin/login-history")
def admin_login_history():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    try:
        profile = _approved_admin_profile(user_id)
        if not profile:
            return "Access denied.", 403

        try:
            page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            page = 1
        event_type = request.args.get("type", "all")
        if event_type not in LOGIN_EVENT_TYPES:
            event_type = "all"
        period = request.args.get("period", "all")
        if period not in LOGIN_HISTORY_PERIODS:
            period = "all"
        # Keep only characters that appear in email addresses; % would act as a wildcard.
        search = "".join(ch for ch in request.args.get("q", "").strip()[:100] if ch.isalnum() or ch in "@._+-")

        def with_filters(query):
            if search:
                query = query.ilike("email", f"%{search}%")
            if LOGIN_HISTORY_PERIODS[period]:
                since = datetime.now(timezone.utc) - LOGIN_HISTORY_PERIODS[period]
                query = query.gte("created_at", since.isoformat())
            return query

        # Per-type counts for the filter chips, within the same search and time range.
        type_counts = {}
        for kind in LOGIN_EVENT_TYPES[1:]:
            counted = with_filters(supabase_admin.table("login_history").select("id", count="exact")).eq("event_type", kind).limit(1).execute()
            type_counts[kind] = counted.count or 0
        type_counts["all"] = sum(type_counts.values())

        start = (page - 1) * LOGIN_HISTORY_PAGE_SIZE
        query = with_filters(supabase_admin.table("login_history").select("*", count="exact"))
        if event_type != "all":
            query = query.eq("event_type", event_type)
        result = query.order("created_at", desc=True).range(start, start + LOGIN_HISTORY_PAGE_SIZE - 1).execute()
        events = result.data or []
        total_count = result.count or 0
        page_count = max(1, (total_count + LOGIN_HISTORY_PAGE_SIZE - 1) // LOGIN_HISTORY_PAGE_SIZE)

        user_ids = list({e["user_id"] for e in events if e.get("user_id")})
        names = {}
        if user_ids:
            people = supabase_admin.table("profiles").select("id, display_name, first_name, last_name, role").in_("id", user_ids).execute().data or []
            names = {p["id"]: p for p in people}
        for event in events:
            person = names.get(event.get("user_id")) or {}
            event["display_name"] = person.get("display_name") or " ".join(filter(None, [person.get("first_name"), person.get("last_name")])) or None
            event["role"] = person.get("role")
            event["reason_text"] = LOGIN_REASON_TEXT.get(event.get("reason"), (event.get("reason") or "").replace("_", " ").capitalize() or None)
            event["device"] = _describe_user_agent(event.get("user_agent"))

        filters = {"type": event_type, "period": period, "q": search}
        return render_template(
            "admin_login_history.html",
            profile=profile,
            events=events,
            page=page,
            page_count=page_count,
            total_count=total_count,
            first_shown=start + 1 if events else 0,
            last_shown=start + len(events),
            type_counts=type_counts,
            filters=filters,
            filters_active=event_type != "all" or period != "all" or bool(search),
            page_url=lambda **changes: url_for("admin_login_history", **{k: v for k, v in {**filters, "page": page, **changes}.items() if v not in ("all", "", None, 1)}),
        )
    except Exception:
        app.logger.exception("Unable to load admin login history")
        return "Unable to load login history. Check the server logs for details.", 500


# =========================
# TECHNICIAN: UPDATE CASE STAGE
# =========================
# =========================
# TECHNICIAN: UPDATE CASE STAGE
# =========================
# =========================
# HELPER: AUTO-DEDUCT INVENTORY
# =========================

# =========================
# TECHNICIAN: UPDATE CASE STAGE
# =========================
@app.route("/technician/cases/<case_id>/update-status", methods=["POST"])
def update_case_status(case_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    try:
        # Verify Technician
        profile_res = supabase_admin.table("profiles").select("role, status, display_name").eq("id", user_id).execute()
        if not profile_res.data or profile_res.data[0]["role"] != "technician" or profile_res.data[0].get("status") != "approved":
            return "Access denied.", 403

        tech_name = profile_res.data[0]["display_name"]
        new_status = request.form.get("status")
        
        allowed_statuses = [
            "assigned",
            "in_progress", 
            "cad_designing", 
            "wax_up_or_try_in",
            "milling_or_printing", 
            "packing_and_curing",
            "finishing_and_quality_check",
        ]
        # Cases leave the bench through submit-for-QC, never straight to completed.

        if new_status not in allowed_statuses:
            return "Invalid status selection.", 400

        # Fetch Case info; only cases still on the bench can change stage (an old browser tab
        # must not pull a delivered or in-transit case back into production).
        case_res = supabase_admin.table("dental_cases").select("dentist_id, case_number, status").eq("id", case_id).eq("technician_id", user_id).execute()
        if not case_res.data:
            return "Case not found or not assigned to you.", 404
        if case_res.data[0].get("status") not in BENCH_STATUSES:
            return "This case has moved past the bench (for example to QC review or dispatch), so its stage can't be changed here. Reload your dashboard.", 409

        dentist_id = case_res.data[0]["dentist_id"]
        case_number = case_res.data[0]["case_number"]

        # Friendly labels for the notification message
        status_labels = {
            "assigned": "Assigned",
            "in_progress": "In Progress",
            "cad_designing": "in CAD Designing (exocad)",
            "wax_up_or_try_in": "in Wax-up / Tooth Setup",
            "milling_or_printing": "in Milling / 3D Printing",
            "packing_and_curing": "in Acrylic Packing & Curing",
            "finishing_and_quality_check": "in Finishing & Quality Check",
        }
        readable_status = status_labels.get(new_status, new_status)

        # Update status in database (only if it is still on the bench, in case it moved meanwhile)
        updated = supabase_admin.table("dental_cases").update({
            "status": new_status
        }).eq("id", case_id).in_("status", list(BENCH_STATUSES)).execute().data or []
        if not updated:
            return "This case moved past the bench while you were working. Reload your dashboard.", 409

        # ==========================================
        # LOG STATUS CHANGE TO TIMELINE HISTORY
        # ==========================================
        try:
            supabase_admin.table("case_status_history").insert({
                "case_id": case_id,
                "status": readable_status,
                "changed_by": user_id,
                "notes": f"Updated by technician {tech_name}"
            }).execute()
        except Exception as hist_err:
            print(f"History log error: {str(hist_err)}")

        # ==========================================
        # JIT MATERIAL INVENTORY DEDUCTION & AUTO-CREATE LOGIC
        # ==========================================
        logged_materials = request.form.getlist("logged_materials")
        logged_percentages = request.form.getlist("logged_percentages")

        if logged_materials and logged_percentages:
            for item_name, percent_str in zip(logged_materials, logged_percentages):
                try:
                    # Safely parse percentage as float (e.g., 25 -> 0.25)
                    deduct_amount = float(percent_str) / 100.0  
                    
                    # Check if the item exists in the inventory table (case-insensitive search)
                    inv_res = supabase_admin.table("inventory").select("*").ilike("item_name", item_name.strip()).execute()
                    
                    if inv_res.data:
                        # SCENARIO A: Item exists. Deduct exact decimal amount.
                        item = inv_res.data[0]
                        item_id = item["id"]
                        current_qty = float(item["quantity"])
                        new_qty = round(current_qty - deduct_amount, 2)
                        
                        # Update stock level safely (Requires NUMERIC column in Supabase)
                        supabase_admin.table("inventory").update({"quantity": new_qty}).eq("id", item_id).execute()
                        
                        # Log into audit logs
                        supabase_admin.table("inventory_logs").insert({
                            "inventory_id": item_id,
                            "user_id": user_id,
                            "action_type": "case_deduction",
                            "quantity_changed": -deduct_amount,
                            "previous_quantity": current_qty,
                            "new_quantity": new_qty,
                            "notes": f"Deducted {percent_str}% at [{new_status}] stage for Case {case_number}"
                        }).execute()
                    
                    else:
                        # SCENARIO B: Item is missing. Auto-create it cleanly!
                        initial_qty = 1.0
                        new_qty = round(initial_qty - deduct_amount, 2)
                        
                        new_item_res = supabase_admin.table("inventory").insert({
                            "item_name": item_name.strip(),
                            "category": "Auto-Added", 
                            "quantity": new_qty, 
                            "unit": "Piece"      
                        }).execute()
                        
                        if new_item_res.data:
                            new_item_id = new_item_res.data[0]["id"]
                            
                            supabase_admin.table("inventory_logs").insert({
                                "inventory_id": new_item_id,
                                "user_id": user_id,
                                "action_type": "auto_created_and_deducted",
                                "quantity_changed": -deduct_amount,
                                "previous_quantity": initial_qty,
                                "new_quantity": new_qty,
                                "notes": f"Missing item auto-created. Deducted {percent_str}% for Case {case_number}."
                            }).execute()

                except Exception as mat_err:
                    print(f"Material deduction error for {item_name}: {str(mat_err)}")

        # 1. Notify the Dentist
        create_notification(
            user_id=dentist_id,
            message=f"Case Status Update: Case {case_number} is now {readable_status} (Tech: {tech_name}).",
            link="/dentist"
        )

        # 2. Notify all Approved Admins
        admins_res = supabase_admin.table("profiles").select("id").eq("role", "admin").eq("status", "approved").execute()
        admins = admins_res.data or []

        for admin in admins:
            create_notification(
                user_id=admin["id"],
                message=f"Lab Progress: Case {case_number} updated to {readable_status} by {tech_name}.",
                link="/admin"
            )

        # Redirect back depending on where they triggered it from
        referer = request.referrer
        if referer and f"/technician/cases/{case_id}" in referer:
            return redirect(f"/technician/cases/{case_id}")
        return redirect(url_for("technician_dashboard"))

    except Exception as e:
        return f"Status update error: {str(e)}"

@app.route("/admin/cases/<case_id>/reassign", methods=["POST"])
def admin_reassign_case(case_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    try:
        # Verify Admin
        profile_res = supabase_admin.table("profiles").select("role, status").eq("id", user_id).execute()
        if not profile_res.data or profile_res.data[0]["role"] != "admin" or profile_res.data[0]["status"] != "approved":
            return "Access denied.", 403

        new_tech_id = request.form.get("technician_id")
        if not new_tech_id:
            return "No technician selected.", 400

        # Fetch case and new tech info for logging/notification
        case_res = supabase_admin.table("dental_cases").select("case_number, status").eq("id", case_id).execute()
        if not case_res.data:
            return "Case not found.", 404
        if case_res.data[0].get("status") not in REASSIGNABLE_STATUSES:
            return "Only cases still in production can be reassigned.", 409
        case_number = case_res.data[0]["case_number"]

        tech_res = supabase_admin.table("profiles").select("display_name, role, status").eq("id", new_tech_id).execute()
        if not tech_res.data or tech_res.data[0].get("role") != "technician" or tech_res.data[0].get("status") != "approved":
            return "Choose an approved technician.", 400
        tech_name = tech_res.data[0]["display_name"] or "New Technician"

        # UPDATE ONLY THE TECHNICIAN_ID (Status is intentionally preserved)
        supabase_admin.table("dental_cases").update({
            "technician_id": new_tech_id
        }).eq("id", case_id).in_("status", list(REASSIGNABLE_STATUSES)).execute()

        # Notify the newly assigned technician
        create_notification(
            user_id=new_tech_id,
            message=f"Case Reassigned: You have been assigned to take over Case {case_number}.",
            link="/technician"
        )

        return redirect(url_for("admin_dashboard"))

    except Exception as e:
        return f"Reassignment error: {str(e)}", 500
# =========================
# ADMIN: VIEW INVENTORY AUDIT LOGS
# =========================
# =========================
# ADMIN: INVENTORY AUDIT LOGS
# =========================
@app.route("/admin/inventory/logs")
def admin_inventory_logs():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    try:
        # 1. Verify admin
        profile_res = supabase_admin.table("profiles").select("*").eq("id", user_id).execute()
        profiles = profile_res.data or []
        if not profiles or profiles[0].get("role") != "admin" or profiles[0].get("status") != "approved":
            return "Access denied.", 403

        profile = profiles[0]

        # 2. Update session heartbeat
        from datetime import datetime
        now_iso = datetime.utcnow().isoformat()
        try:
            supabase_admin.table("profiles").update({"last_seen": now_iso}).eq("id", user_id).execute()
        except Exception:
            pass

        # 3. Date filters & fetch logs
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")

        query = (
            supabase_admin
            .table("inventory_logs")
            .select("*, inventory(item_name, unit), profiles(display_name, first_name)")
            .order("created_at", desc=True)
        )

        if start_date:
            query = query.gte("created_at", f"{start_date}T00:00:00")
        if end_date:
            query = query.lte("created_at", f"{end_date}T23:59:59")

        logs_res = query.execute()
        logs = logs_res.data or []

        # 4. Fetch notifications for header
        notif_res = (
            supabase_admin
            .table("notifications")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(5)
            .execute()
        )
        notifications = notif_res.data or []

        # 5. Fetch Restock Requests for sidebar badge & modal
        restock_res = (
            supabase_admin
            .table("material_requests")
            .select("*, profiles(display_name), dental_cases(case_number)")
            .eq("status", "pending")
            .order("created_at", desc=True)
            .execute()
        )
        restock_requests = restock_res.data or []

        # 6. Fetch Unassigned Cases for sidebar badge & modal
        unassigned_res = (
            supabase_admin
            .table("dental_cases")
            .select("*")
            .eq("status", "submitted")
            .order("created_at", desc=True)
            .execute()
        )
        unassigned_cases = unassigned_res.data or []

        # 7. Fetch Technicians & compute active workloads in bulk
        techs_res = (
            supabase_admin
            .table("profiles")
            .select("id, display_name, first_name")
            .eq("role", "technician")
            .eq("status", "approved")
            .execute()
        )
        technicians = techs_res.data or []
        tech_ids = [t["id"] for t in technicians]

        active_statuses = [
            "assigned", "in_progress", "cad_designing", "wax_up_or_try_in",
            "milling_or_printing", "packing_and_curing", "finishing_and_quality_check", "needs_revision"
        ]

        if tech_ids:
            workload_cases_res = (
                supabase_admin
                .table("dental_cases")
                .select("technician_id")
                .in_("technician_id", tech_ids)
                .in_("status", active_statuses)
                .execute()
            )
            workload_counts = {}
            for w in (workload_cases_res.data or []):
                t_id = w.get("technician_id")
                workload_counts[t_id] = workload_counts.get(t_id, 0) + 1

            for tech in technicians:
                tech["active_workload"] = workload_counts.get(tech["id"], 0)
        else:
            for tech in technicians:
                tech["active_workload"] = 0

        # 8. Fetch milling & finishing stages for audio chime triggers
        milling_res = (
            supabase_admin
            .table("dental_cases")
            .select("id")
            .eq("status", "milling_or_printing")
            .execute()
        )
        milling_cases = milling_res.data or []

        qc_res = (
            supabase_admin
            .table("dental_cases")
            .select("id")
            .eq("status", "finishing_and_quality_check")
            .execute()
        )
        qc_cases = qc_res.data or []

        return render_template(
            "admin_inventory_logs.html",
            profile=profile,
            logs=logs,
            notifications=notifications,
            restock_requests=restock_requests,
            unassigned_cases=unassigned_cases,
            technicians=technicians,
            milling_cases=milling_cases,
            qc_cases=qc_cases
        )
    except Exception as e:
        return f"Audit logs error: {str(e)}", 500


@app.route("/admin/users/<user_id>/approve", methods=["POST"])
def approve_user(user_id):
    admin_id = session.get("user_id")
    if not admin_id:
        return redirect(url_for("login"))

    try:
        admin_response = supabase_admin.table("profiles").select("id, role, status").eq("id", admin_id).execute()
        admin_profiles = admin_response.data or []
        if not admin_profiles or admin_profiles[0]["role"] != "admin" or admin_profiles[0]["status"] != "approved":
            return "Access denied.", 403

        user_response = supabase_admin.table("profiles").select("id, role, status, display_name, first_name").eq("id", user_id).execute()
        users = user_response.data or []
        if not users:
            return "User not found.", 404

        user = users[0]
        if user["role"] == "admin":
            return "Admin accounts cannot be approved here.", 403

        # Update status in database
        supabase_admin.table("profiles").update({"status": "approved"}).eq("id", user_id).execute()

        # Fire email asynchronously in background thread so Gunicorn doesn't timeout
        try:
            auth_user_res = supabase_admin.auth.admin.get_user_by_id(user_id)
            if auth_user_res and getattr(auth_user_res, "user", None):
                recipient_email = auth_user_res.user.email
                user_name = user.get("display_name") or user.get("first_name") or "Colleague"

                email_thread = threading.Thread(
                    target=send_email_in_background,
                    args=(recipient_email, user_name, "approved")
                )
                email_thread.start()
        except Exception as mail_err:
            print(f"Failed to start approval email thread: {str(mail_err)}")

        return redirect(url_for("admin_dashboard"))

    except Exception as e:
        return f"Approval error: {str(e)}", 500


@app.route("/admin/users/<user_id>/reject", methods=["POST"])
def reject_user(user_id):
    admin_id = session.get("user_id")
    if not admin_id:
        return redirect(url_for("login"))

    try:
        admin_response = supabase_admin.table("profiles").select("id, role, status").eq("id", admin_id).execute()
        admin_profiles = admin_response.data or []
        if not admin_profiles or admin_profiles[0]["role"] != "admin" or admin_profiles[0]["status"] != "approved":
            return "Access denied.", 403

        user_response = supabase_admin.table("profiles").select("id, role, status, display_name, first_name").eq("id", user_id).execute()
        users = user_response.data or []
        if not users:
            return "User not found.", 404

        user = users[0]
        if user["role"] == "admin":
            return "Admin accounts cannot be rejected here.", 403

        # Update status in database
        supabase_admin.table("profiles").update({"status": "rejected"}).eq("id", user_id).execute()

        # Fire email asynchronously in background thread
        try:
            auth_user_res = supabase_admin.auth.admin.get_user_by_id(user_id)
            if auth_user_res and getattr(auth_user_res, "user", None):
                recipient_email = auth_user_res.user.email
                user_name = user.get("display_name") or user.get("first_name") or "Colleague"

                email_thread = threading.Thread(
                    target=send_email_in_background,
                    args=(recipient_email, user_name, "rejected")
                )
                email_thread.start()
        except Exception as mail_err:
            print(f"Failed to start rejection email thread: {str(mail_err)}")

        return redirect(url_for("admin_dashboard"))

    except Exception as e:
        return f"Rejection error: {str(e)}", 500
# =========================
# DENTIST DASHBOARD
# =========================

# =========================
# DENTIST DASHBOARD
# =========================

# =========================
# DENTIST DASHBOARD
# =========================

@app.route("/dentist")
def dentist_dashboard():
    # =========================
    # 1. CHECK LOGIN
    # =========================
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    try:
        # =========================
        # 2. GET DENTIST PROFILE
        # =========================
        response = (
            supabase_admin
            .table("profiles")
            .select("*")
            .eq("id", user_id)
            .execute()
        )
        profiles = response.data or []

        if not profiles:
            session.clear()
            return "Profile not found.", 404

        profile = profiles[0]

        # Check Role
        if profile.get("role") != "dentist":
            return "Access denied.", 403

        # Check Approval Status
        if profile.get("status") != "approved":
            status = profile.get("status")
            if status == "pending":
                return "Your account is still waiting for administrator approval.", 403
            if status == "rejected":
                return "Your account application was rejected.", 403
            return "Your account is not approved.", 403

        # Initials fallback guard to prevent template avatar IndexError
        if not profile.get("first_name"):
            profile["first_name"] = (profile.get("display_name") or "Dentist")[:1]
        if not profile.get("last_name"):
            profile["last_name"] = ""

        # =========================
        # 3. UPDATE USER HEARTBEAT
        # =========================
        now_iso = datetime.utcnow().isoformat()
        try:
            supabase_admin.table("profiles").update({"last_seen": now_iso}).eq("id", user_id).execute()
        except Exception:
            pass

        # =========================
        # 4. GET DENTIST CASES (SAFE QUERY)
        # =========================
        cases_response = (
            supabase_admin
            .table("dental_cases")
            .select("*")
            .eq("dentist_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        dental_cases = cases_response.data or []
        case_ids = [c["id"] for c in dental_cases]

        # =========================
        # 5. BULK FETCH TEETH (ELIMINATES N+1 LOOP)
        # =========================
        if case_ids:
            teeth_res = (
                supabase_admin
                .table("case_teeth")
                .select("*")
                .in_("case_id", case_ids)
                .order("created_at")
                .execute()
            )
            teeth_data = teeth_res.data or []

            teeth_by_case = {}
            for tooth in teeth_data:
                teeth_by_case.setdefault(tooth["case_id"], []).append(tooth)

            for case in dental_cases:
                case["teeth"] = teeth_by_case.get(case["id"], [])
        else:
            for case in dental_cases:
                case["teeth"] = []

        # ========================================================
        # 6. STAGE SLICES FOR SUMMARY STRIP & SOUND ALERT TRIGGERS
        # ========================================================
        action_cases = [c for c in dental_cases if c.get("status") == "needs_revision"]
        pending_cases = [c for c in dental_cases if c.get("status") in ["submitted", "assigned"]]
        prod_cases = [
            c for c in dental_cases 
            if c.get("status") in [
                "in_progress", "cad_designing", "wax_up_or_try_in",
                "milling_or_printing", "packing_and_curing"
            ]
        ]
        milling_cases = [c for c in dental_cases if c.get("status") == "milling_or_printing"]
        qc_cases = [c for c in dental_cases if c.get("status") in ["finishing_and_quality_check", "pending_admin_review"]]
        transit_cases = [c for c in dental_cases if c.get("status") in DISPATCH_STATUSES]
        completed_cases = [c for c in dental_cases if c.get("status") in DELIVERED_STATUSES]
        draft_cases = [c for c in dental_cases if c.get("status") == "draft"]
        ready_cases = [c for c in dental_cases if c.get("status") in ["ready_for_delivery", *DELIVERED_STATUSES]]

        # Active label tokens so the clinic can confirm receipt without scanning
        if transit_cases:
            labels = (
                supabase_admin
                .table("case_dispatches")
                .select("case_id, token")
                .in_("case_id", [c["id"] for c in transit_cases])
                .eq("status", "active")
                .execute()
                .data
                or []
            )
            tokens_by_case = {label["case_id"]: label["token"] for label in labels}
            for c in transit_cases:
                c["dispatch_token"] = tokens_by_case.get(c["id"])

        # Delivery receipt and open issue for each delivered case
        if completed_cases:
            completed_ids = [c["id"] for c in completed_cases]
            delivered_events = (
                supabase_admin.table("case_dispatch_events")
                .select("case_id, event_type, received_by, courier_name, created_at")
                .in_("case_id", completed_ids)
                .eq("event_type", "delivered")
                .order("created_at")
                .execute()
                .data
                or []
            )
            events_by_case = {}
            for e in delivered_events:
                events_by_case.setdefault(e["case_id"], []).append(e)
            open_issues = {r["case_id"] for r in _issue_reports_for(completed_ids) if r["status"] == "open"}
            for c in completed_cases:
                c["receipt"] = _delivery_receipt(c, events_by_case.get(c["id"], []))
                c["issue_open"] = c["id"] in open_issues
                received = c["receipt"]["delivered_at"] or _parse_timestamp(c.get("created_at"))
                c["month_key"] = received.astimezone(LAB_TZ).strftime("%Y-%m") if received else ""
            completed_cases.sort(key=lambda c: c.get("delivered_at") or "", reverse=True)

        # =========================
        # 7. GET RECENT NOTIFICATIONS
        # =========================
        notif_response = (
            supabase_admin
            .table("notifications")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(5)
            .execute()
        )
        notifications = notif_response.data or []

        # =========================
        # 8. RENDER VIEW
        # =========================
        return render_template(
            "dentist_dashboard.html",
            profile=profile,
            dental_cases=dental_cases,
            action_cases=action_cases,
            pending_cases=pending_cases,
            prod_cases=prod_cases,
            active_cases=prod_cases + pending_cases,
            milling_cases=milling_cases,
            qc_cases=qc_cases,
            transit_cases=transit_cases,
            ready_cases=ready_cases,
            completed_cases=completed_cases,
            draft_cases=draft_cases,
            notifications=notifications
        )

    except Exception as e:
        import traceback
        traceback.print_exc()  # Prints the exact error file and line to Railway logs
        return f"Dentist dashboard error: {str(e)}", 500

@app.route("/dentist/cases/new", methods=["GET", "POST"])
def create_dental_case():
    CASE_FILES_BUCKET = "dental-case-files"

    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    try:
        response = supabase_admin.table("profiles").select("*").eq("id", user_id).execute()
        profiles = response.data or []
        if not profiles:
            session.clear()
            return "Profile not found.", 404

        profile = profiles[0]

        if profile.get("role") != "dentist":
            return "Access denied.", 403

        if profile.get("status") != "approved":
            return "Your account is not approved.", 403

        if request.method == "POST":
            patient_name = request.form.get("patient_name", "").strip()
            patient_reference = request.form.get("patient_reference", "").strip() or None
            case_type = request.form.get("case_type", "").strip()
            due_date = request.form.get("due_date", "").strip() or None  # <--- CAPTURE DUE DATE HERE
            instructions = request.form.get("instructions", "").strip() or None

            submit_action = request.form.get("submit_action", "draft").strip()
            case_status = "submitted" if submit_action == "send" else "draft"

            if not patient_name:
                return "Patient name is required.", 400
            if not case_type:
                return "Case type is required.", 400
            if not due_date:
                return "Due date is required.", 400

            # Get selected teeth data
            tooth_numbers = request.form.getlist("tooth_number")
            materials = request.form.getlist("material")
            shades = request.form.getlist("shade")
            tooth_notes = request.form.getlist("tooth_notes")

            teeth_to_insert = []
            for index, tooth_number in enumerate(tooth_numbers):
                tooth_number = tooth_number.strip()
                if not tooth_number:
                    continue

                material = materials[index].strip() if index < len(materials) else None
                shade = shades[index].strip() if index < len(shades) else None
                notes = tooth_notes[index].strip() if index < len(tooth_notes) else None

                teeth_to_insert.append({
                    "tooth_number": tooth_number,
                    "restoration_type": case_type,
                    "material": material,
                    "shade": shade,
                    "notes": notes
                })

            if not teeth_to_insert:
                return "Please select at least one tooth.", 400

            # Generate Case Number: highest existing number + 1, so deleted drafts never cause a duplicate
            case_number = _next_case_number()

            # Insert Case including due_date
            case_response = supabase_admin.table("dental_cases").insert({
                "case_number": case_number,
                "dentist_id": user_id,
                "patient_name": patient_name,
                "patient_reference": patient_reference,
                "case_type": case_type,
                "due_date": due_date,  # <--- SAVED TO SUPABASE
                "instructions": instructions,
                "status": case_status
            }).execute()

            created_cases = case_response.data or []
            if not created_cases:
                return "Failed to create dental case.", 500

            case = created_cases[0]

            for tooth in teeth_to_insert:
                tooth["case_id"] = case["id"]

            try:
                teeth_response = supabase_admin.table("case_teeth").insert(teeth_to_insert).execute()
            except Exception as teeth_error:
                supabase_admin.table("dental_cases").delete().eq("id", case["id"]).execute()
                return f"Failed to save selected teeth: {str(teeth_error)}", 500

            if not teeth_response.data:
                supabase_admin.table("dental_cases").delete().eq("id", case["id"]).execute()
                return "Failed to save selected teeth.", 500

            # File Upload Logic (STL & Smile)
            stl_file = request.files.get("stl_file")
            smile_files = request.files.getlist("patient_smile")
            uploaded_files = []

            if stl_file and stl_file.filename:
                original_name = stl_file.filename
                safe_name = secure_filename(original_name)
                storage_path = f"{user_id}/{case['id']}/stl/{safe_name}"
                file_bytes = stl_file.read()
                mime_type = stl_file.mimetype or "application/octet-stream"

                try:
                    supabase_admin.storage.from_(CASE_FILES_BUCKET).upload(
                        storage_path, file_bytes, {"content-type": mime_type, "upsert": "false"}
                    )
                except Exception as upload_error:
                    supabase_admin.table("case_teeth").delete().eq("case_id", case["id"]).execute()
                    supabase_admin.table("dental_cases").delete().eq("id", case["id"]).execute()
                    return f"Failed to upload STL file: {str(upload_error)}", 500

                uploaded_files.append({
                    "case_id": case["id"], "uploaded_by": user_id,
                    "file_name": original_name, "file_path": storage_path,
                    "file_type": "stl", "mime_type": mime_type, "file_size": len(file_bytes)
                })

            for smile_file in smile_files:
                if not smile_file or not smile_file.filename:
                    continue
                original_name = smile_file.filename
                safe_name = secure_filename(original_name)
                storage_path = f"{user_id}/{case['id']}/smile/{safe_name}"
                file_bytes = smile_file.read()
                mime_type = smile_file.mimetype or "image/jpeg"

                try:
                    supabase_admin.storage.from_(CASE_FILES_BUCKET).upload(
                        storage_path, file_bytes, {"content-type": mime_type, "upsert": "false"}
                    )
                except Exception as upload_error:
                    for uf in uploaded_files:
                        try: supabase_admin.storage.from_(CASE_FILES_BUCKET).remove([uf["file_path"]])
                        except: pass
                    supabase_admin.table("case_teeth").delete().eq("case_id", case["id"]).execute()
                    supabase_admin.table("dental_cases").delete().eq("id", case["id"]).execute()
                    return f"Failed to upload patient smile photo: {str(upload_error)}", 500

                uploaded_files.append({
                    "case_id": case["id"], "uploaded_by": user_id,
                    "file_name": original_name, "file_path": storage_path,
                    "file_type": "smile_image", "mime_type": mime_type, "file_size": len(file_bytes)
                })

            if uploaded_files:
                try:
                    supabase_admin.table("case_files").insert(uploaded_files).execute()
                except Exception:
                    pass

            # ======================================
            # NOTIFY ADMINS INSTEAD OF TECHNICIANS
            # ======================================
            if case_status == "submitted":
                try:
                    admins_res = supabase_admin.table("profiles").select("id").eq("role", "admin").eq("status", "approved").execute()
                    admins = admins_res.data or []
                    dentist_name = profile.get("display_name") or f"Dr. {profile.get('first_name', '')}"
                    
                    for admin in admins:
                        create_notification(
                            user_id=admin["id"],
                            message=f"New Case Submitted: {case_number} ({case_type}) due on {due_date} by {dentist_name}",
                            link="/admin"
                        )
                except Exception:
                    pass

            return redirect(url_for("dentist_dashboard"))

        return render_template("create_dental_case.html", profile=profile)

    except Exception as e:
        return f"Create dental case error: {str(e)}", 500

# =========================
# REGISTER
# =========================

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":

        # =========================
        # GET FORM DATA
        # =========================
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        display_name = request.form.get("display_name", "").strip()
        phone = request.form.get("phone", "").strip()

        role = request.form.get("role", "").strip()
        professional_id = request.form.get("professional_id", "N/A")
        
        # Grab clinic name if user selected dentist, otherwise set to None/Empty
        clinic_name = request.form.get("clinic_name", "").strip() if role == "dentist" else None

        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password")
        confirm_password = request.form.get("confirm_password")

        # =========================
        # VALIDATE PASSWORD
        # =========================
        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template("register.html")

        # =========================
        # PREVENT ADMIN REGISTRATION
        # =========================
        if role not in ["dentist", "technician"]:
            flash("Invalid role selected.", "error")
            return render_template("register.html")

        try:
            # =========================
            # CREATE SUPABASE AUTH USER
            # =========================
            response = supabase.auth.sign_up({
                "email": email,
                "password": password
            })

            if not response.user:
                flash("Registration failed. Please check your information.", "error")
                return render_template("register.html")

            # =========================
            # GET USER UUID
            # =========================
            user_id = response.user.id

            # =========================
            # CREATE PROFILE
            # =========================
            supabase_admin.table("profiles").insert({
                "id": user_id,
                "first_name": first_name,
                "last_name": last_name,
                "display_name": display_name,
                "phone": phone,
                "professional_id": professional_id,
                "clinic_name": clinic_name,  # <--- SAVED TO DATABASE HERE
                "role": role,
                "status": "pending"
            }).execute()

            # =========================
            # SHOW EMAIL VERIFICATION
            # =========================
            return render_template(
                "verify_email.html",
                email=email
            )

        except Exception as e:
            err_str = str(e)
            # Catch Postgres unique constraint 23505 or Supabase duplicate user notifications
            if "23505" in err_str or "already registered" in err_str or "already exists" in err_str:
                flash("An account with this email address already exists. Please sign in or use a different email.", "error")
            else:
                flash("Unable to complete registration. Please verify your details and try again.", "error")

            return render_template("register.html")

    return render_template("register.html")

# =========================
# LOGIN
# =========================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not email or not password:
            _record_login_event("failed", email, reason="missing_credentials")
            flash("Email and password are required.", "error")
            return render_template("login.html"), 400

        try:
            # =========================
            # SUPABASE LOGIN
            # =========================
            response = supabase.auth.sign_in_with_password({
                "email": email,
                "password": password
            })

            if not response.user:
                _record_login_event("failed", email, reason="invalid_credentials")
                flash("Login failed. Please check your credentials.", "error")
                return render_template("login.html")

            user_id = response.user.id

            # =========================
            # GET USER PROFILE
            # =========================
            profile_response = (
                supabase_admin
                .table("profiles")
                .select("*")
                .eq("id", user_id)
                .execute()
            )

            profiles = profile_response.data or []

            if not profiles:
                _record_login_event("blocked", email, user_id, "profile_not_found")
                flash("User profile not found in the system.", "error")
                return render_template("login.html")

            profile = profiles[0]

            # =========================
            # CHECK ACCOUNT STATUS
            # =========================
            if profile["status"] == "pending":
                _record_login_event("blocked", email, user_id, "account_pending")
                return render_template("account_pending.html")

            if profile["status"] == "rejected":
                _record_login_event("blocked", email, user_id, "account_rejected")
                flash("Your account application was rejected.", "error")
                return render_template("login.html")

            if profile["status"] != "approved":
                _record_login_event("blocked", email, user_id, "account_not_approved")
                flash("Your account is not yet approved by the administrator.", "error")
                return render_template("login.html")

            if profile.get("role") not in ("admin", "dentist", "technician"):
                _record_login_event("blocked", email, user_id, "invalid_account_role")
                flash("Invalid account role assigned.", "error")
                return render_template("login.html")

            session.clear()
            session["user_id"] = user_id
            session["login_session_id"] = str(uuid.uuid4())
            session["login_email"] = email
            _record_login_event("success", email, user_id)

            # =========================
            # UPDATE LAST SEEN TIMESTAMP
            # =========================
            try:
                from datetime import datetime
                supabase_admin.table("profiles").update({"last_seen": datetime.utcnow().isoformat()}).eq("id", user_id).execute()
            except Exception:
                pass

            # =========================
            # REDIRECT BY ROLE
            # =========================
            # A scanned dispatch label sends people here with ?next=/d/<token>
            next_path = request.args.get("next")
            if _safe_next_path(next_path):
                return redirect(next_path)

            if profile["role"] == "admin":
                return redirect(url_for("admin_dashboard"))

            if profile["role"] == "dentist":
                return redirect(url_for("dentist_dashboard"))

            if profile["role"] == "technician":
                return redirect(url_for("technician_dashboard"))

        except Exception as e:
            # Catch wrong password, invalid email format, or connection drops from Supabase
            error_message = str(e)
            _record_login_event(
                "failed",
                email,
                reason="invalid_credentials"
                if "Invalid login credentials" in error_message or "invalid_grant" in error_message
                else "authentication_error",
            )
            if "Invalid login credentials" in error_message or "invalid_grant" in error_message:
                flash("Incorrect email or password. Please try again.", "error")
            else:
                flash(f"Login error: {error_message}", "error")
            
            return render_template("login.html")

    return render_template("login.html")
# =========================
# REVIEW USER
# =========================

@app.route("/admin/users/<user_id>/review")
def review_user(user_id):

    # =========================
    # CHECK LOGIN
    # =========================
    admin_id = session.get("user_id")

    if not admin_id:
        return redirect(url_for("login"))

    try:
        # =========================
        # GET ADMIN PROFILE
        # =========================
        admin_response = (
            supabase_admin
            .table("profiles")
            .select("id, role, status")
            .eq("id", admin_id)
            .execute()
        )

        admin_profiles = admin_response.data or []

        if not admin_profiles:
            session.clear()
            return "Admin profile not found.", 404

        admin_profile = admin_profiles[0]

        # =========================
        # CHECK ADMIN ROLE & STATUS
        # =========================
        if admin_profile["role"] != "admin":
            return "Access denied.", 403

        if admin_profile["status"] != "approved":
            return "Administrator account is not approved.", 403

        # =========================
        # GET USER APPLICATION PROFILE
        # =========================
        user_response = (
            supabase_admin
            .table("profiles")
            .select("*")
            .eq("id", user_id)
            .execute()
        )

        users = user_response.data or []

        if not users:
            return "User application not found.", 404

        user = users[0]

        # =========================
        # CHECK SUPABASE AUTH EMAIL CONFIRMATION
        # =========================
        auth_user_response = supabase_admin.auth.admin.get_user_by_id(user_id)
        auth_user = getattr(auth_user_response, "user", None)
        
        # Check if email_confirmed_at exists and is populated
        email_verified = False
        if auth_user and getattr(auth_user, "email_confirmed_at", None):
            email_verified = True

        # =========================
        # ONLY ALLOW PENDING
        # OR REJECTED USERS
        # =========================
        if user["status"] not in ["pending", "rejected"]:
            return "This application cannot be reviewed.", 400

        # =========================
        # SHOW REVIEW PAGE
        # =========================
        return render_template(
            "admin_review.html",
            user=user,
            email_verified=email_verified
        )

    except Exception as e:
        return f"Review application error: {str(e)}", 500

# =========================
# FORGOT PASSWORD
# =========================

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():

    if request.method == "POST":

        email = request.form["email"].strip().lower()

        try:

            # =========================
            # FIND USER IN SUPABASE AUTH
            # =========================

            users_response = supabase_admin.auth.admin.list_users()

            auth_user = None

            for user in users_response:

                if user.email and user.email.lower() == email:
                    auth_user = user
                    break


            # =========================
            # DON'T REVEAL WHETHER
            # THE ACCOUNT EXISTS
            # =========================

            if not auth_user:

                return render_template(
                    "reset_email_sent.html"
                )


            # =========================
            # GET PROFILE
            # =========================

            profile_response = (
                supabase_admin
                .table("profiles")
                .select("id, role, status")
                .eq("id", auth_user.id)
                .execute()
            )

            profiles = profile_response.data


            # =========================
            # NO PROFILE
            # =========================

            if not profiles:

                return render_template(
                    "reset_email_sent.html"
                )


            profile = profiles[0]


            # =========================
            # BLOCK ADMIN PASSWORD RESET
            # =========================

            if profile["role"] == "admin":

                return render_template(
                    "reset_email_sent.html"
                )


            # =========================
            # SEND RESET EMAIL
            # =========================

            supabase.auth.reset_password_for_email(
                email,
                options={
                    "redirect_to": f"{APP_URL}/reset-password"
                }
            )


            # =========================
            # SHOW GENERIC SUCCESS PAGE
            # =========================

            return render_template(
                "reset_email_sent.html"
            )


        except Exception as e:

            return f"Password reset error: {str(e)}"


    return render_template("forgot_password.html")

# =========================
# RESET PASSWORD
# =========================
@app.route("/reset-password")
def reset_password():

    return render_template(
        "reset_password.html",
        supabase_url=SUPABASE_URL,
        supabase_publishable_key=SUPABASE_KEY
    )

# =========================
# LOGOUT
# =========================

@app.route("/logout", methods=["GET", "POST"])
def logout():

    user_id = session.get("user_id")
    if user_id:
        _record_login_event("logout", session.get("login_email"), user_id)

    try:

        supabase.auth.sign_out()

    except Exception:

        pass


    # =========================
    # CLEAR FLASK SESSION
    # =========================

    session.clear()


    # =========================
    # RETURN TO LOGIN
    # =========================

    return redirect(url_for("login"))

# =========================
# REOPEN USER
# =========================

@app.route("/admin/users/<user_id>/reopen", methods=["POST"])
def reopen_user(user_id):

    # =========================
    # CHECK LOGIN
    # =========================

    admin_id = session.get("user_id")

    if not admin_id:

        return redirect(url_for("login"))


    try:

        # =========================
        # GET ADMIN PROFILE
        # =========================

        admin_response = (
            supabase_admin
            .table("profiles")
            .select("id, role, status")
            .eq("id", admin_id)
            .execute()
        )

        admin_profiles = admin_response.data or []


        if not admin_profiles:

            session.clear()

            return "Admin profile not found.", 404


        admin_profile = admin_profiles[0]


        # =========================
        # VERIFY ADMIN
        # =========================

        if admin_profile["role"] != "admin":

            return "Access denied.", 403


        if admin_profile["status"] != "approved":

            return "Administrator account is not approved.", 403


        # =========================
        # GET USER
        # =========================

        user_response = (
            supabase_admin
            .table("profiles")
            .select("id, role, status")
            .eq("id", user_id)
            .execute()
        )

        users = user_response.data or []


        if not users:

            return "User not found.", 404


        user = users[0]


        # =========================
        # ONLY REOPEN REJECTED USERS
        # =========================

        if user["status"] != "rejected":

            return "Only rejected applications can be reopened.", 400


        # =========================
        # CHANGE TO PENDING
        # =========================

        supabase_admin \
            .table("profiles") \
            .update({
                "status": "pending"
            }) \
            .eq("id", user_id) \
            .execute()


        # =========================
        # RETURN TO ADMIN DASHBOARD
        # =========================

        return redirect(
            url_for("admin_dashboard")
        )


    except Exception as e:

        return f"Reopen application error: {str(e)}", 500

# =========================
# TECHNICIAN DASHBOARD
# =========================
def log_case_status_change(case_id, status, user_id, notes=None):
    try:
        supabase_admin.table("case_status_history").insert({
            "case_id": case_id,
            "status": status,
            "changed_by": user_id,
            "notes": notes
        }).execute()
    except Exception as e:
        print(f"Status history log error: {str(e)}")
# =========================
# TECHNICIAN DASHBOARD
# =========================
@app.route("/technician/cases/<case_id>/request-material", methods=["POST"])
def request_material(case_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))
    
    item_name = request.form.get("item_name", "").strip()
    if item_name:
        supabase_admin.table("material_requests").insert({
            "case_id": case_id,
            "technician_id": user_id,
            "item_name": item_name,
            "status": "pending"
        }).execute()

        # Alert all admins
        admins_res = supabase_admin.table("profiles").select("id").eq("role", "admin").eq("status", "approved").execute()
        for admin in (admins_res.data or []):
            create_notification(
                user_id=admin["id"],
                message=f"📦 Restock Request: Technician requested '{item_name}' for Case #{case_id[:8]}",
                link="/admin/inventory"
            )
            
    return redirect(f"/technician/cases/{case_id}")


# =========================
# ADMIN: RESOLVE RESTOCK REQUEST
# =========================
@app.route("/admin/restock-requests/<request_id>/resolve", methods=["POST"])
@role_required("admin")
def admin_resolve_restock_request(request_id):
    next_path = request.form.get("next")
    back = next_path if _safe_next_path(next_path) else url_for("admin_dashboard")
    try:
        action = request.form.get("action")
        if action not in ("fulfilled", "dismissed"):
            return "Invalid restock action.", 400

        rows = (
            supabase_admin.table("material_requests")
            .update({"status": action, "resolved_at": _now_iso(), "resolved_by": g.profile["id"]})
            .eq("id", request_id)
            .eq("status", "pending")
            .execute()
            .data
            or []
        )
        if not rows:
            # Already handled by another admin, or the request no longer exists.
            flash("This request was already marked as restocked or dismissed, possibly by another administrator. Nothing was changed.", "restock-stale")
            return redirect(back)

        req = rows[0]
        case_number = None
        if req.get("case_id"):
            cases = supabase_admin.table("dental_cases").select("case_number").eq("id", req["case_id"]).execute().data or []
            case_number = cases[0]["case_number"] if cases else None
        for_case = f" for Case {case_number}" if case_number else ""
        admin_name = _display_name(g.profile, "an administrator")
        if action == "fulfilled":
            message = f"Restocked: your request for '{req['item_name']}'{for_case} was restocked by {admin_name}."
        else:
            message = f"Restock request closed: your request for '{req['item_name']}'{for_case} was dismissed by {admin_name}."

        tech_name = "The technician"
        if req.get("technician_id"):
            create_notification(
                user_id=req["technician_id"],
                message=message,
                link=f"/technician/cases/{req['case_id']}" if req.get("case_id") else "/technician",
            )
            techs = supabase_admin.table("profiles").select("display_name, first_name").eq("id", req["technician_id"]).execute().data or []
            tech_name = _display_name(techs[0] if techs else None, "The technician")

        if action == "fulfilled":
            flash(f"'{req['item_name']}'{for_case} is closed as restocked. {tech_name} has been notified.", "restock-fulfilled")
        else:
            flash(f"'{req['item_name']}'{for_case} was closed without restocking. {tech_name} has been notified.", "restock-dismissed")
        return redirect(back)
    except Exception:
        app.logger.exception("Unable to resolve restock request")
        return "Unable to update the restock request. Check the server logs for details.", 500


# =========================
# TECHNICIAN DASHBOARD
# =========================
@app.route("/technician")
def technician_dashboard():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    try:
        # ==========================================
        # 1. VERIFY TECHNICIAN ACCESS
        # ==========================================
        response = supabase_admin.table("profiles").select("*").eq("id", user_id).execute()
        profiles = response.data or []
        if not profiles or profiles[0].get("role") != "technician" or profiles[0].get("status") != "approved":
            return "Access denied.", 403

        profile = profiles[0]

        # Guard initials to prevent template IndexError if names are blank
        if not profile.get("first_name"):
            profile["first_name"] = (profile.get("display_name") or "Tech")[:1]
        if not profile.get("last_name"):
            profile["last_name"] = ""

        # ==========================================
        # 2. UPDATE ACTIVE SESSION HEARTBEAT
        # ==========================================
        from datetime import datetime
        now_iso = datetime.utcnow().isoformat()
        try:
            supabase_admin.table("profiles").update({"last_seen": now_iso}).eq("id", user_id).execute()
        except Exception:
            pass

        # ==========================================
        # 3. GET CASES ASSIGNED TO THIS TECHNICIAN
        # ==========================================
        cases_response = (
            supabase_admin
            .table("dental_cases")
            .select("*, profiles!dental_cases_dentist_id_fkey(display_name, clinic_name, phone)")
            .eq("technician_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        assigned_cases = cases_response.data or []
        case_ids = [c["id"] for c in assigned_cases]

        # ==========================================
        # 4. BULK FETCH TEETH DETAILS & SHADES
        # ==========================================
        if case_ids:
            teeth_res = (
                supabase_admin
                .table("case_teeth")
                .select("*")
                .in_("case_id", case_ids)
                .order("created_at")
                .execute()
            )
            teeth_data = teeth_res.data or []

            teeth_by_case = {}
            for t in teeth_data:
                teeth_by_case.setdefault(t["case_id"], []).append(t)

            for case in assigned_cases:
                teeth_list = teeth_by_case.get(case["id"], [])
                # Bind to both keys so template lookups (teeth and case_teeth) succeed
                case["teeth"] = teeth_list
                case["case_teeth"] = teeth_list
        else:
            for case in assigned_cases:
                case["teeth"] = []
                case["case_teeth"] = []

        # ==========================================
        # 5. STAGE SLICES FOR UI FILTERS & SOUNDS
        # ==========================================
        new_intake_cases = [c for c in assigned_cases if c.get("status") in ["assigned", "pending", "new"]]
        active_cad_cases = [c for c in assigned_cases if c.get("status") in ["in_progress", "cad_designing", "wax_up_or_try_in"]]
        milling_cases = [c for c in assigned_cases if c.get("status") == "milling_or_printing"]
        packing_cases = [c for c in assigned_cases if c.get("status") == "packing_and_curing"]
        qc_cases = [c for c in assigned_cases if c.get("status") == "finishing_and_quality_check"]
        completed_cases = [c for c in assigned_cases if c.get("status") in ["ready_for_delivery", *DELIVERED_STATUSES]]

        # ==========================================
        # 6. TECHNICIAN'S MATERIAL RESTOCK REQUESTS
        # ==========================================
        restock_res = (
            supabase_admin
            .table("material_requests")
            .select("*, dental_cases(case_number)")
            .eq("technician_id", user_id)
            .order("created_at", desc=True)
            .limit(10)
            .execute()
        )
        my_restock_requests = restock_res.data or []

        # ==========================================
        # 7. NOTIFICATIONS
        # ==========================================
        notif_response = (
            supabase_admin
            .table("notifications")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(5)
            .execute()
        )
        notifications = notif_response.data or []

        # ==========================================
        # 8. RENDER VIEW
        # ==========================================
        return render_template(
            "technician_dashboard.html",
            profile=profile,
            cases=assigned_cases,
            assigned_cases=new_intake_cases,
            new_cases=new_intake_cases,
            active_cad_cases=active_cad_cases,
            milling_cases=milling_cases,
            packing_cases=packing_cases,
            qc_cases=qc_cases,
            completed_cases=completed_cases,
            restock_requests=my_restock_requests,
            notifications=notifications
        )

    except Exception as e:
        return f"Technician dashboard error: {str(e)}", 500

# =========================
# TECHNICIAN CASE DETAILS
# =========================

@app.route("/technician/cases/<case_id>")
def view_case_details(case_id):

    # =========================
    # CHECK LOGIN
    # =========================

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))


    try:

        # =========================
        # VERIFY TECHNICIAN
        # =========================

        profile_response = (
            supabase_admin
            .table("profiles")
            .select("role, status")
            .eq("id", user_id)
            .execute()
        )

        profiles = profile_response.data or []

        if not profiles or profiles[0]["role"] != "technician":
            return "Access denied.", 403


        # =========================
        # GET CASE DATA
        # =========================

        case_response = (
            supabase_admin
            .table("dental_cases")
            .select("*")
            .eq("id", case_id)
            .execute()
        )

        cases = case_response.data or []

        if not cases:
            return "Case not found.", 404

        case = cases[0]


        # =========================
        # GET TEETH DATA
        # =========================

        teeth_response = (
            supabase_admin
            .table("case_teeth")
            .select("*")
            .eq("case_id", case_id)
            .order("tooth_number")
            .execute()
        )

        teeth = teeth_response.data or []


        # =========================
        # GET FILE RECORDS
        # =========================

        files_response = (
            supabase_admin
            .table("case_files")
            .select("*")
            .eq("case_id", case_id)
            .execute()
        )

        files = files_response.data or []


        # =========================
        # GENERATE DOWNLOAD URLS
        # =========================

        CASE_FILES_BUCKET = "dental-case-files"

        for file_record in files:

            signed_url = (
                supabase_admin
                .storage
                .from_(CASE_FILES_BUCKET)
                .create_signed_url(file_record["file_path"], 3600)
            )
            
            if isinstance(signed_url, dict):
                file_record["download_url"] = signed_url.get("signedURL")
            else:
                file_record["download_url"] = signed_url


        # ==========================================
        # GET STATUS TIMELINE HISTORY
        # ==========================================

        history_response = (
            supabase_admin
            .table("case_status_history")
            .select("*, profiles(display_name)")
            .eq("case_id", case_id)
            .order("created_at")
            .execute()
        )

        case_history = history_response.data or []


        # =========================
        # RENDER TEMPLATE
        # =========================

        return render_template(
            "technician_case_details.html",
            case=case,
            teeth=teeth,
            files=files,
            case_history=case_history
        )


    except Exception as e:

        return f"Case details error: {str(e)}", 500

# =========================
# EDIT DENTAL CASE (DRAFT)
# =========================

@app.route("/dentist/cases/<case_id>/edit", methods=["GET", "POST"])
def edit_dental_case(case_id):

    # =========================
    # CHECK LOGIN
    # =========================
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    try:
        # =========================
        # VERIFY DENTIST
        # =========================
        profile_res = supabase_admin.table("profiles").select("role").eq("id", user_id).execute()
        if not profile_res.data or profile_res.data[0].get("role") != "dentist":
            return "Access denied.", 403

        # =========================
        # FETCH EXISTING CASE
        # =========================
        case_res = supabase_admin.table("dental_cases").select("*").eq("id", case_id).eq("dentist_id", user_id).execute()
        if not case_res.data:
            return "Case not found.", 404
        
        case = case_res.data[0]

        # =========================
        # PREVENT EDITING SUBMITTED CASES
        # =========================
        # =========================
        # ALLOW EDITING FOR DRAFT OR REVISIONS
        # =========================
        if case["status"] not in ["draft", "needs_revision"]:
            return "Only drafts or cases requiring revision can be edited.", 400

        # ==========================================
        # POST (SAVE UPDATES OR SEND)
        # ==========================================
        if request.method == "POST":
            
            patient_name = request.form.get("patient_name", "").strip()
            patient_reference = request.form.get("patient_reference", "").strip() or None
            case_type = request.form.get("case_type", "").strip()
            instructions = request.form.get("instructions", "").strip() or None
            
            submit_action = request.form.get("submit_action", "draft").strip()
            new_status = "submitted" if submit_action == "send" else "draft"
            # A remake goes straight back to its technician when resent, and never becomes a
            # deletable draft (it shares stored files with the original case).
            resend_to_technician = bool(case.get("remake_of") and case.get("technician_id"))
            if case.get("remake_of"):
                if submit_action == "send":
                    new_status = "assigned" if resend_to_technician else "submitted"
                else:
                    new_status = case["status"]

            # -----------------------------
            # UPDATE CASE DETAILS
            # -----------------------------
            supabase_admin.table("dental_cases").update({
                "patient_name": patient_name,
                "patient_reference": patient_reference,
                "case_type": case_type,
                "instructions": instructions,
                "status": new_status
            }).eq("id", case_id).execute()

            # -----------------------------
            # RESET TEETH (Delete old, insert new)
            # -----------------------------
            supabase_admin.table("case_teeth").delete().eq("case_id", case_id).execute()

            tooth_numbers = request.form.getlist("tooth_number")
            materials = request.form.getlist("material")
            shades = request.form.getlist("shade")
            tooth_notes = request.form.getlist("tooth_notes")

            teeth_to_insert = []
            for index, t_num in enumerate(tooth_numbers):
                t_num = t_num.strip()
                if not t_num: continue
                
                teeth_to_insert.append({
                    "case_id": case_id,
                    "tooth_number": t_num,
                    "restoration_type": case_type,
                    "material": materials[index].strip() if index < len(materials) else None,
                    "shade": shades[index].strip() if index < len(shades) else None,
                    "notes": tooth_notes[index].strip() if index < len(tooth_notes) else None
                })
            
            if teeth_to_insert:
                supabase_admin.table("case_teeth").insert(teeth_to_insert).execute()

            if case.get("remake_of") and submit_action == "send":
                log_case_status_change(case_id, "Remake details sent by dentist", user_id, None)
                if resend_to_technician:
                    create_notification(
                        user_id=case["technician_id"],
                        message=f"Remake ready: the dentist sent the updated details for #{case['case_number']}. You can start work.",
                        link=f"/technician/cases/{case_id}",
                    )
                else:
                    _notify_admins(f"Remake #{case['case_number']} is ready for assignment.")

            return redirect(url_for("dentist_dashboard"))

        # ==========================================
        # GET (LOAD FORM)
        # ==========================================
        
        teeth_res = supabase_admin.table("case_teeth").select("*").eq("case_id", case_id).execute()
        existing_teeth = teeth_res.data or []

        return render_template("edit_dental_case.html", case=case, teeth=existing_teeth)

    except Exception as e:
        return f"Edit case error: {str(e)}", 500

# =========================
# DENTIST: DELETE DRAFT
# =========================
@app.route("/dentist/cases/<case_id>/delete", methods=["POST"])
@role_required("dentist")
def delete_draft_case(case_id):
    try:
        cases = (
            supabase_admin.table("dental_cases")
            .select("id, case_number, patient_name, status")
            .eq("id", case_id)
            .eq("dentist_id", g.profile["id"])
            .execute()
            .data
            or []
        )
        if not cases:
            flash("This draft was already deleted or isn't yours. Nothing was changed.", "draft-stale")
            return redirect(url_for("dentist_dashboard"))
        case = cases[0]
        if case.get("status") != "draft":
            flash(f"Case #{case['case_number']} has already been sent to the lab, so it can't be deleted. Contact the laboratory to cancel it.", "draft-stale")
            return redirect(url_for("dentist_dashboard"))

        # Remove uploaded files from storage first, then the child rows, then the case itself.
        files = supabase_admin.table("case_files").select("file_path").eq("case_id", case_id).execute().data or []
        paths = [f["file_path"] for f in files if f.get("file_path")]
        if paths:
            # Keep any file that another case (such as a remake) still uses.
            shared = supabase_admin.table("case_files").select("file_path").in_("file_path", paths).neq("case_id", case_id).execute().data or []
            paths = [p for p in paths if p not in {s["file_path"] for s in shared}]
        if paths:
            try:
                supabase_admin.storage.from_("dental-case-files").remove(paths)
            except Exception:
                app.logger.exception("Unable to remove stored files of draft %s", case_id)
        for table in ("case_files", "case_teeth", "case_status_history", "material_requests"):
            supabase_admin.table(table).delete().eq("case_id", case_id).execute()
        supabase_admin.table("dental_cases").delete().eq("id", case_id).eq("dentist_id", g.profile["id"]).eq("status", "draft").execute()

        patient = f" for {case['patient_name']}" if case.get("patient_name") else ""
        flash(f"Draft #{case['case_number']}{patient} was deleted.", "draft-deleted")
        return redirect(url_for("dentist_dashboard"))
    except Exception:
        app.logger.exception("Unable to delete draft case")
        return "Unable to delete this draft. Check the server logs for details.", 500


def _parse_timestamp(value):
    """Parse a Supabase timestamp string; returns an aware datetime or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _delivery_receipt(case, events):
    """Summarize how a case reached the clinic, from its chain-of-custody events."""
    by_type = {}
    for event in events:
        by_type.setdefault(event.get("event_type"), event)  # events arrive oldest first
    delivered_at = _parse_timestamp(case.get("delivered_at"))
    created_at = _parse_timestamp(case.get("created_at"))
    receipt = {
        "approved": by_type.get("issued"),
        "dispatched": by_type.get("dispatched"),
        "delivered": by_type.get("delivered"),
        "delivered_at": delivered_at,
        "tracked": delivered_at is not None,
        "turnaround_days": None,
        "late_days": None,
        "report_until": None,
    }
    if delivered_at:
        delivered_day = delivered_at.astimezone(LAB_TZ).date()
        if created_at:
            receipt["turnaround_days"] = (delivered_day - created_at.astimezone(LAB_TZ).date()).days
        try:
            due_day = datetime.strptime(str(case.get("due_date"))[:10], "%Y-%m-%d").date()
            receipt["late_days"] = max(0, (delivered_day - due_day).days)
        except ValueError:
            pass
        receipt["report_until"] = delivered_at + timedelta(days=ISSUE_REPORT_WINDOW_DAYS)
    return receipt


def _issue_reports_for(case_ids):
    """Issue reports for these cases, newest first; empty if the table isn't migrated yet."""
    if not case_ids:
        return []
    try:
        return (
            supabase_admin.table("case_issue_reports")
            .select("*")
            .in_("case_id", list(case_ids))
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
    except Exception:
        app.logger.warning("case_issue_reports unavailable; run the issue reports migration")
        return []


def _open_issue_count():
    """Number of delivery issue reports awaiting review (0 if the table isn't migrated yet)."""
    try:
        return supabase_admin.table("case_issue_reports").select("id", count="exact").eq("status", "open").limit(1).execute().count or 0
    except Exception:
        return 0


def _next_case_number():
    """Return the next case number (DL-00001 style) after the highest one in use."""
    latest = (
        supabase_admin.table("dental_cases")
        .select("case_number")
        .like("case_number", "DL-%")
        .order("case_number", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    try:
        highest = int(latest[0]["case_number"].split("-", 1)[1]) if latest else 0
    except (ValueError, IndexError):
        highest = 0
    return f"DL-{highest + 1:05d}"


# =========================
# DENTIST: CASE SUMMARY, DELIVERY RECEIPT, ISSUE REPORTS
# =========================
@app.route("/dentist/cases/<case_id>")
@role_required("dentist")
def dentist_case_summary(case_id):
    try:
        cases = supabase_admin.table("dental_cases").select("*").eq("id", case_id).eq("dentist_id", g.profile["id"]).execute().data or []
        if not cases:
            return "Case not found.", 404
        case = cases[0]
        if case.get("status") in ("draft", "needs_revision"):
            return redirect(url_for("edit_dental_case", case_id=case_id))

        case["teeth"] = supabase_admin.table("case_teeth").select("*").eq("case_id", case_id).order("tooth_number").execute().data or []
        files = supabase_admin.table("case_files").select("*").eq("case_id", case_id).execute().data or []
        for f in files:
            try:
                signed = supabase_admin.storage.from_("dental-case-files").create_signed_url(f["file_path"], 3600)
                f["download_url"] = signed.get("signedURL") or signed.get("signed_url") if isinstance(signed, dict) else signed
            except Exception:
                f["download_url"] = None
        history = (
            supabase_admin.table("case_status_history").select("status, notes, created_at, changed_by")
            .eq("case_id", case_id).order("created_at").execute().data or []
        )
        events = (
            supabase_admin.table("case_dispatch_events")
            .select("event_type, actor_id, courier_name, received_by, notes, created_at")
            .eq("case_id", case_id).in_("event_type", ["issued", "dispatched", "delivered"])
            .order("created_at").execute().data or []
        )
        people_ids = {pid for pid in [case.get("technician_id")] + [h.get("changed_by") for h in history] + [e.get("actor_id") for e in events] if pid}
        people = (
            supabase_admin.table("profiles").select("id, display_name, first_name, role").in_("id", list(people_ids)).execute().data or []
        ) if people_ids else []
        names = {p["id"]: _display_name(p) for p in people}
        for item in history + events:
            item["actor_name"] = names.get(item.get("changed_by") or item.get("actor_id"), "Laboratory")

        receipt = _delivery_receipt(case, events)
        reports = _issue_reports_for([case_id])
        # Links between an original case and its remakes (empty until the remake migration runs)
        parent, remakes = None, []
        try:
            if case.get("remake_of"):
                parents = supabase_admin.table("dental_cases").select("id, case_number").eq("id", case["remake_of"]).execute().data or []
                parent = parents[0] if parents else None
            remakes = (
                supabase_admin.table("dental_cases").select("id, case_number, status, due_date")
                .eq("remake_of", case_id).order("created_at").execute().data or []
            )
        except Exception:
            app.logger.warning("Remake columns unavailable; run the remakes migration")
        has_open_report = any(r["status"] == "open" for r in reports)
        can_report = (
            case.get("status") == "delivered"
            and receipt["report_until"] is not None
            and datetime.now(timezone.utc) <= receipt["report_until"]
            and not has_open_report
        )
        return render_template(
            "dentist_case_summary.html",
            profile=g.profile,
            case=case,
            files=files,
            history=history,
            receipt=receipt,
            technician_name=names.get(case.get("technician_id")),
            reports=reports,
            can_report=can_report,
            has_open_report=has_open_report,
            issue_types=ISSUE_TYPES,
            issue_decisions=ISSUE_DECISIONS,
            window_days=ISSUE_REPORT_WINDOW_DAYS,
            parent=parent,
            remakes=remakes,
        )
    except Exception:
        app.logger.exception("Unable to load case summary")
        return "Unable to load this case. Check the server logs for details.", 500


@app.route("/dentist/cases/<case_id>/report-issue", methods=["POST"])
@role_required("dentist")
def report_case_issue(case_id):
    back = url_for("dentist_case_summary", case_id=case_id)
    try:
        issue_type = request.form.get("issue_type")
        details = request.form.get("details", "").strip()[:1000]
        if issue_type not in ISSUE_TYPES:
            flash("Choose what went wrong before sending the report.", "issue-stale")
            return redirect(back)
        if issue_type == "other" and not details:
            flash("Describe the problem when you choose \"Other problem\".", "issue-stale")
            return redirect(back)

        cases = (
            supabase_admin.table("dental_cases").select("id, case_number, status, delivered_at, created_at, due_date")
            .eq("id", case_id).eq("dentist_id", g.profile["id"]).execute().data or []
        )
        if not cases or cases[0].get("status") != "delivered":
            flash("Issues can only be reported on cases that were delivered to your clinic.", "issue-stale")
            return redirect(back if cases else url_for("dentist_dashboard"))
        case = cases[0]
        receipt = _delivery_receipt(case, [])
        if receipt["report_until"] is None or datetime.now(timezone.utc) > receipt["report_until"]:
            flash(f"Issues can be reported within {ISSUE_REPORT_WINDOW_DAYS} days of delivery. Please contact the laboratory directly.", "issue-stale")
            return redirect(back)
        if any(r["status"] == "open" for r in _issue_reports_for([case_id])):
            flash("You already reported an issue on this case. The laboratory is reviewing it.", "issue-stale")
            return redirect(back)

        supabase_admin.table("case_issue_reports").insert({
            "case_id": case_id,
            "dentist_id": g.profile["id"],
            "issue_type": issue_type,
            "details": details or None,
            "status": "open",
        }).execute()
        dentist = _display_name(g.profile, "A dentist")
        _notify_admins(
            f"Delivery issue: {dentist} reported '{ISSUE_TYPES[issue_type]}' on Case {case['case_number']}. Review it and decide whether to remake.",
            link=url_for("admin_delivery_issues"),
        )
        flash(f"Your report on Case #{case['case_number']} was sent. The laboratory will review it and notify you of its decision.", "issue-reported")
        return redirect(back)
    except Exception:
        app.logger.exception("Unable to report case issue")
        return "Unable to send the report. Check the server logs for details.", 500


@app.route("/admin/delivery-issues")
@role_required("admin")
def admin_delivery_issues():
    try:
        view = request.args.get("view", "open")
        if view not in ("open", "resolved", "all"):
            view = "open"
        try:
            reports = supabase_admin.table("case_issue_reports").select("*").order("created_at", desc=True).execute().data or []
            migrated = True
        except Exception:
            reports, migrated = [], False
        counts = {
            "open": sum(1 for r in reports if r["status"] == "open"),
            "resolved": sum(1 for r in reports if r["status"] != "open"),
            "all": len(reports),
        }
        if view == "open":
            reports = [r for r in reports if r["status"] == "open"]
        elif view == "resolved":
            reports = [r for r in reports if r["status"] != "open"]

        case_ids = list({r["case_id"] for r in reports} | {r["remake_case_id"] for r in reports if r.get("remake_case_id")})
        cases = (
            supabase_admin.table("dental_cases").select("id, case_number, patient_name, case_type, technician_id, dentist_id, delivered_at, due_date, created_at, status")
            .in_("id", case_ids).execute().data or []
        ) if case_ids else []
        cases_by_id = {c["id"]: c for c in cases}
        people_ids = {pid for c in cases for pid in (c.get("technician_id"), c.get("dentist_id")) if pid} | {r["reviewed_by"] for r in reports if r.get("reviewed_by")}
        people = (
            supabase_admin.table("profiles").select("id, display_name, first_name, clinic_name").in_("id", list(people_ids)).execute().data or []
        ) if people_ids else []
        people_by_id = {p["id"]: p for p in people}
        for r in reports:
            c = cases_by_id.get(r["case_id"], {})
            r["case"] = c
            r["dentist"] = people_by_id.get(c.get("dentist_id"), {})
            r["technician_name"] = _display_name(people_by_id.get(c.get("technician_id")), "Unassigned")
            r["reviewer_name"] = _display_name(people_by_id.get(r.get("reviewed_by")), "an administrator")
            r["issue_label"] = ISSUE_TYPES.get(r["issue_type"], r["issue_type"])
            r["decision_label"] = ISSUE_DECISIONS.get(r["status"])
            r["remake"] = cases_by_id.get(r.get("remake_case_id"))
        return render_template(
            "admin_delivery_issues.html",
            profile=g.profile,
            reports=reports,
            counts=counts,
            view=view,
            migrated=migrated,
            today=datetime.now(LAB_TZ).date().isoformat(),
            default_due=(datetime.now(LAB_TZ).date() + timedelta(days=5)).isoformat(),
        )
    except Exception:
        app.logger.exception("Unable to load delivery issues")
        return "Unable to load delivery issues. Check the server logs for details.", 500


@app.route("/admin/delivery-issues/<report_id>/review", methods=["POST"])
@role_required("admin")
def admin_review_delivery_issue(report_id):
    back = url_for("admin_delivery_issues")
    try:
        decision = request.form.get("decision")
        note = request.form.get("admin_note", "").strip()[:1000]
        if decision not in ISSUE_DECISIONS:
            return "Invalid decision.", 400
        if decision == "no_action" and not note:
            flash("Add a short note for the dentist explaining why no remake is needed.", "review-note")
            return redirect(back)

        # Remake details are checked before the report is claimed.
        remake_path = request.form.get("remake_path", "lab")
        return_requested = request.form.get("return_original") == "1"
        due_date = None
        if decision == "remake_accepted":
            if remake_path not in ("lab", "dentist"):
                return "Invalid remake path.", 400
            try:
                due_date = datetime.strptime(request.form.get("due_date", ""), "%Y-%m-%d").date()
            except ValueError:
                flash("Choose a due date for the remake.", "review-input")
                return redirect(back)
            if due_date < datetime.now(LAB_TZ).date():
                flash("The remake's due date can't be in the past.", "review-input")
                return redirect(back)
            if remake_path == "dentist" and not note:
                flash("Tell the dentist what the lab needs, e.g. a new impression of #11.", "review-input")
                return redirect(back)

        rows = (
            supabase_admin.table("case_issue_reports")
            .update({"status": decision, "admin_note": note or None, "reviewed_by": g.profile["id"], "reviewed_at": _now_iso()})
            .eq("id", report_id).eq("status", "open").execute().data or []
        )
        if not rows:
            flash("This report was already reviewed, possibly by another administrator. Nothing was changed.", "review-stale")
            return redirect(back)
        report = rows[0]
        originals = supabase_admin.table("dental_cases").select("*").eq("id", report["case_id"]).execute().data or []
        original = originals[0] if originals else {}
        case_number = original.get("case_number", "your case")

        if decision == "no_action":
            create_notification(
                user_id=report["dentist_id"],
                message=f"Issue reviewed on Case {case_number}: no remake needed. Note from the lab: {note}",
                link=url_for("dentist_case_summary", case_id=report["case_id"]),
            )
            flash(f"Case {case_number}: no remake needed. The dentist has been notified.", "issue-reviewed")
            return redirect(back)

        try:
            remake = _create_remake_case(original, report, remake_path, due_date, note, return_requested)
        except Exception:
            # Put the report back so the admin can try again.
            supabase_admin.table("case_issue_reports").update(
                {"status": "open", "admin_note": None, "reviewed_by": None, "reviewed_at": None}
            ).eq("id", report_id).execute()
            raise

        return_line = " Please send the original restoration back with the courier." if return_requested else ""
        due_text = due_date.strftime("%b %d, %Y").replace(" 0", " ")
        if remake_path == "dentist":
            create_notification(
                user_id=report["dentist_id"],
                message=(f"Remake accepted for Case {case_number}, but the lab needs something from you first: {note} "
                         f"Update and resend remake #{remake['case_number']}.{return_line}"),
                link=url_for("edit_dental_case", case_id=remake["id"]),
            )
            outcome = f"Remake #{remake['case_number']} is waiting in the dentist's Action Required tab."
        else:
            message = f"Remake accepted: the lab is remaking Case {case_number} as #{remake['case_number']}, due {due_text}.{return_line}"
            if note:
                message += f" Note from the lab: {note}"
            create_notification(user_id=report["dentist_id"], message=message, link=url_for("dentist_case_summary", case_id=remake["id"]))
            if remake.get("technician_id"):
                create_notification(
                    user_id=remake["technician_id"],
                    message=f"Remake assigned: #{remake['case_number']} remakes Case {case_number} ({remake['remake_reason']}). Due {due_text}.",
                    link=f"/technician/cases/{remake['id']}",
                )
                outcome = f"Remake #{remake['case_number']} was assigned to {remake['technician_name']}."
            else:
                outcome = f"Remake #{remake['case_number']} is in Incoming cases for assignment; the original technician is no longer active."
        flash(f"{outcome} The dentist has been notified.", "issue-reviewed")
        return redirect(back)
    except Exception:
        app.logger.exception("Unable to review delivery issue")
        return "Unable to save the decision. Check the server logs for details.", 500


def _create_remake_case(original, report, remake_path, due_date, note, return_requested):
    """Create a new case that remakes `original`, copying its prescription, teeth and files."""
    tech = {}
    if original.get("technician_id"):
        techs = (
            supabase_admin.table("profiles").select("id, display_name, first_name, role, status")
            .eq("id", original["technician_id"]).execute().data or []
        )
        if techs and techs[0].get("role") == "technician" and techs[0].get("status") == "approved":
            tech = techs[0]

    reason = ISSUE_TYPES.get(report["issue_type"], "Reported issue")
    if report.get("details"):
        reason += f": {report['details']}"
    if return_requested:
        reason += " (clinic asked to return the original restoration)"

    if remake_path == "dentist":
        status = "needs_revision"
    else:
        status = "assigned" if tech else "submitted"

    remake = supabase_admin.table("dental_cases").insert({
        "case_number": _next_case_number(),
        "dentist_id": original["dentist_id"],
        "technician_id": tech.get("id"),
        "patient_name": original.get("patient_name"),
        "patient_reference": original.get("patient_reference"),
        "case_type": original.get("case_type"),
        "material": original.get("material"),
        "shade": original.get("shade"),
        "instructions": original.get("instructions"),
        "due_date": due_date.isoformat(),
        "status": status,
        "revision_notes": note if remake_path == "dentist" else None,
        "remake_of": original["id"],
        "remake_reason": reason,
    }).execute().data[0]

    teeth = supabase_admin.table("case_teeth").select("*").eq("case_id", original["id"]).execute().data or []
    if teeth:
        supabase_admin.table("case_teeth").insert([
            {k: t.get(k) for k in ("tooth_number", "restoration_type", "material", "shade", "notes")} | {"case_id": remake["id"]}
            for t in teeth
        ]).execute()
    # The remake points at the same stored files; nothing is duplicated in storage.
    files = supabase_admin.table("case_files").select("*").eq("case_id", original["id"]).execute().data or []
    if files:
        supabase_admin.table("case_files").insert([
            {k: f.get(k) for k in ("uploaded_by", "file_name", "file_path", "file_type", "mime_type", "file_size")} | {"case_id": remake["id"]}
            for f in files
        ]).execute()

    log_case_status_change(
        remake["id"],
        f"Remake created from #{original['case_number']}",
        g.profile["id"],
        reason + (f" Lab note: {note}" if note else ""),
    )
    supabase_admin.table("case_issue_reports").update(
        {"remake_case_id": remake["id"], "return_requested": return_requested}
    ).eq("id", report["id"]).execute()
    remake["technician_name"] = _display_name(tech, "the original technician")
    return remake


# =========================
# NOTIFICATION HELPER
# =========================
def create_notification(user_id, message, link="#"):
    try:
        supabase_admin.table("notifications").insert({
            "user_id": user_id,
            "message": message,
            "link": link
        }).execute()
    except Exception as e:
        print(f"Failed to create notification: {str(e)}")

# =========================
# TECHNICIAN: RETURN CASE
# =========================
@app.route("/technician/cases/<case_id>/return", methods=["POST"])
def return_case_to_dentist(case_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    try:
        # Verify Technician Role
        profile_res = supabase_admin.table("profiles").select("role, status").eq("id", user_id).execute()
        if not profile_res.data or profile_res.data[0].get("role") != "technician" or profile_res.data[0].get("status") != "approved":
            return "Access denied.", 403

        # Get the technician's revision notes from the form
        revision_notes = request.form.get("revision_notes", "").strip()
        if not revision_notes:
            return "You must provide a reason for sending the case back.", 400

        # Only the assigned technician can return a case, and only while it is on the bench.
        case_res = supabase_admin.table("dental_cases").select("dentist_id, case_number, status").eq("id", case_id).eq("technician_id", user_id).execute()
        if not case_res.data:
            return "Case not found or not assigned to you.", 404
        if case_res.data[0].get("status") not in BENCH_STATUSES:
            return "Only cases still on the bench can be returned to the dentist.", 409

        dentist_id = case_res.data[0]["dentist_id"]
        case_number = case_res.data[0]["case_number"]

        # Update the case status
        updated = supabase_admin.table("dental_cases").update({
            "status": "needs_revision",
            "revision_notes": revision_notes
        }).eq("id", case_id).in_("status", list(BENCH_STATUSES)).execute().data or []
        if not updated:
            return "This case moved past the bench meanwhile. Reload your dashboard.", 409
        log_case_status_change(case_id, "Returned to dentist for revision", user_id, revision_notes)

        # Send Notification to Dentist
        create_notification(
            user_id=dentist_id,
            message=f"Action Required: Technician requested revisions on Case {case_number}",
            link=f"/dentist/cases/{case_id}/edit"
        )

        return redirect(url_for("technician_dashboard"))

    except Exception as e:
        return f"Return case error: {str(e)}", 500

# =========================
# QC REVIEW & QR DISPATCH
# =========================
def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _display_name(profile, fallback="Staff"):
    return (profile or {}).get("display_name") or (profile or {}).get("first_name") or fallback


def _notify_admins(message, link="/admin"):
    admins = (
        supabase_admin.table("profiles")
        .select("id")
        .eq("role", "admin")
        .eq("status", "approved")
        .execute()
        .data
        or []
    )
    for admin in admins:
        create_notification(user_id=admin["id"], message=message, link=link)


def _set_case_status(case_id, from_statuses, changes):
    """Apply `changes` only while the case is still in one of `from_statuses`.

    Returns the updated case, or None if someone else already moved it.
    """
    rows = (
        supabase_admin.table("dental_cases")
        .update(changes)
        .eq("id", case_id)
        .in_("status", list(from_statuses))
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None


def _log_dispatch_event(case_id, event_type, dispatch_id=None, **fields):
    try:
        supabase_admin.table("case_dispatch_events").insert({
            "case_id": case_id,
            "dispatch_id": dispatch_id,
            "event_type": event_type,
            "actor_id": session.get("user_id"),
            "ip_address": request.remote_addr,
            "user_agent": request.headers.get("User-Agent", "")[:512] or None,
            **fields,
        }).execute()
    except Exception:
        app.logger.exception("Unable to record dispatch event")


def _active_dispatch_label(case_id):
    labels = (
        supabase_admin.table("case_dispatches")
        .select("*")
        .eq("case_id", case_id)
        .eq("status", "active")
        .execute()
        .data
        or []
    )
    return labels[0] if labels else None


def _issue_dispatch_label(case_id):
    """Void the case's current label, if any, and create a new one."""
    old_label = _active_dispatch_label(case_id)
    if old_label:
        supabase_admin.table("case_dispatches").update({
            "status": "voided",
            "voided_at": _now_iso(),
        }).eq("id", old_label["id"]).execute()
        _log_dispatch_event(case_id, "voided", old_label["id"])

    label = supabase_admin.table("case_dispatches").insert({
        "case_id": case_id,
        "token": secrets.token_urlsafe(16),
        "created_by": session.get("user_id"),
    }).execute().data[0]
    _log_dispatch_event(case_id, "issued", label["id"])
    return label


def _dispatch_scan_url(token):
    return (APP_URL or request.host_url).rstrip("/") + url_for("scan_dispatch_label", token=token)


def _qr_svg(data):
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_Q,
        border=2,
        image_factory=qrcode.image.svg.SvgPathImage,
    )
    qr.add_data(data)
    qr.make(fit=True)
    return Markup(qr.make_image().to_string(encoding="unicode"))


def _patient_initials(name):
    return " ".join(f"{part[0].upper()}." for part in (name or "").split()) or "N/A"


def _case_for_dispatch(case_id):
    """Load a case with its teeth plus dentist and technician profiles."""
    cases = supabase_admin.table("dental_cases").select("*").eq("id", case_id).execute().data or []
    if not cases:
        return None
    case = cases[0]
    case["teeth"] = (
        supabase_admin.table("case_teeth")
        .select("tooth_number, material, shade, restoration_type")
        .eq("case_id", case_id)
        .order("tooth_number")
        .execute()
        .data
        or []
    )
    people_ids = [pid for pid in (case.get("dentist_id"), case.get("technician_id")) if pid]
    people = (
        supabase_admin.table("profiles")
        .select("id, display_name, first_name, clinic_name, phone")
        .in_("id", people_ids)
        .execute()
        .data
        or []
    ) if people_ids else []
    people_by_id = {p["id"]: p for p in people}
    case["dentist"] = people_by_id.get(case.get("dentist_id"), {})
    case["technician"] = people_by_id.get(case.get("technician_id"), {})
    return case


def _load_dispatch_label(token):
    labels = supabase_admin.table("case_dispatches").select("*").eq("token", token).execute().data or []
    return labels[0] if labels else None


@app.route("/technician/cases/<case_id>/submit-review", methods=["POST"])
@role_required("technician")
def submit_for_admin_review(case_id):
    try:
        tech_name = _display_name(g.profile, "Technician")
        rows = (
            supabase_admin.table("dental_cases")
            .update({"status": "pending_admin_review"})
            .eq("id", case_id)
            .eq("technician_id", g.profile["id"])
            .eq("status", "finishing_and_quality_check")
            .execute()
            .data
            or []
        )
        if not rows:
            return "This case isn't in Finishing & QC or isn't assigned to you.", 409

        case_number = rows[0]["case_number"]
        log_case_status_change(case_id, "Submitted for admin QC", g.profile["id"], f"Submitted by technician {tech_name}")
        _notify_admins(f"QC review needed: {tech_name} finished Case {case_number}. Inspect it and approve it for dispatch.")
        return redirect(url_for("technician_dashboard"))
    except Exception:
        app.logger.exception("Unable to submit case for QC review")
        return "Unable to submit this case for QC. Check the server logs for details.", 500


@app.route("/admin/cases/<case_id>/approve-qc", methods=["POST"])
@role_required("admin")
def admin_approve_qc(case_id):
    try:
        case = _set_case_status(case_id, ["pending_admin_review"], {"status": "ready_for_dispatch"})
        if not case:
            return "This case is no longer waiting for QC review.", 409

        _issue_dispatch_label(case_id)
        admin_name = _display_name(g.profile, "Admin")
        log_case_status_change(case_id, "QC approved, ready for dispatch", g.profile["id"], f"Approved by {admin_name}")
        if case.get("technician_id"):
            create_notification(
                user_id=case["technician_id"],
                message=f"QC passed: Case {case['case_number']} is ready for dispatch. Print the label and pack the case.",
                link=url_for("dispatch_slip", case_id=case_id),
            )
        return redirect(url_for("dispatch_slip", case_id=case_id))
    except Exception:
        app.logger.exception("Unable to approve QC")
        return "Unable to approve this case. Check the server logs for details.", 500


@app.route("/admin/cases/<case_id>/reject-qc", methods=["POST"])
@role_required("admin")
def admin_reject_qc(case_id):
    try:
        notes = request.form.get("qc_notes", "").strip()[:1000]
        if not notes:
            return "Add a note telling the technician what to fix.", 400

        case = _set_case_status(case_id, ["pending_admin_review"], {"status": "finishing_and_quality_check"})
        if not case:
            return "This case is no longer waiting for QC review.", 409

        log_case_status_change(case_id, "Returned to Finishing & QC", g.profile["id"], notes)
        if case.get("technician_id"):
            create_notification(
                user_id=case["technician_id"],
                message=f"QC returned: Case {case['case_number']} needs more work. Admin note: {notes}",
                link=f"/technician/cases/{case_id}",
            )
        return redirect(url_for("admin_dashboard"))
    except Exception:
        app.logger.exception("Unable to reject QC")
        return "Unable to return this case. Check the server logs for details.", 500


@app.route("/cases/<case_id>/dispatch-slip")
@role_required("admin", "technician")
def dispatch_slip(case_id):
    try:
        case = _case_for_dispatch(case_id)
        if not case:
            return "Case not found.", 404
        if g.profile["role"] == "technician" and case.get("technician_id") != g.profile["id"]:
            return "Access denied.", 403
        if case.get("status") not in DISPATCH_STATUSES:
            return "A dispatch label is only available after the case passes QC.", 409

        label = _active_dispatch_label(case_id)
        if not label:
            return "This case has no active label. Ask an admin to reissue it.", 404

        scan_url = _dispatch_scan_url(label["token"])
        return render_template(
            "dispatch_slip.html",
            case=case,
            label=label,
            qr_svg=_qr_svg(scan_url),
            scan_url=scan_url,
            patient_initials=_patient_initials(case.get("patient_name")),
            back_url=url_for("admin_dashboard" if g.profile["role"] == "admin" else "technician_dashboard"),
        )
    except Exception:
        app.logger.exception("Unable to render dispatch slip")
        return "Unable to load the dispatch label. Check the server logs for details.", 500


@app.route("/admin/cases/<case_id>/dispatch/reissue", methods=["POST"])
@role_required("admin")
def admin_reissue_dispatch_label(case_id):
    try:
        cases = supabase_admin.table("dental_cases").select("status").eq("id", case_id).execute().data or []
        if not cases or cases[0].get("status") not in DISPATCH_STATUSES:
            return "Labels can only be reissued for cases that are ready for dispatch or in transit.", 409
        _issue_dispatch_label(case_id)
        return redirect(url_for("dispatch_slip", case_id=case_id))
    except Exception:
        app.logger.exception("Unable to reissue dispatch label")
        return "Unable to reissue the label. Check the server logs for details.", 500


@app.route("/d/<token>")
@role_required("admin", "technician", "dentist")
def scan_dispatch_label(token):
    try:
        label = _load_dispatch_label(token)
        if not label:
            return render_template("dispatch_scan.html", problem="unknown", profile=g.profile), 404

        case = _case_for_dispatch(label["case_id"])
        if not case:
            return render_template("dispatch_scan.html", problem="unknown", profile=g.profile), 404

        if label["status"] != "active":
            _log_dispatch_event(case["id"], "denied", label["id"], notes="voided_label")
            return render_template("dispatch_scan.html", problem="voided", case=case, profile=g.profile), 410

        if g.profile["role"] == "dentist" and case.get("dentist_id") != g.profile["id"]:
            _log_dispatch_event(case["id"], "denied", label["id"], notes="wrong_clinic")
            _notify_admins(
                f"Delivery check: {_display_name(g.profile, 'A clinic')} scanned the label for Case "
                f"{case['case_number']}, which is addressed to another clinic. Check for a mixed-up package."
            )
            return render_template("dispatch_scan.html", problem="wrong_clinic", case=case, profile=g.profile), 403

        events = (
            supabase_admin.table("case_dispatch_events")
            .select("event_type, actor_id, courier_name, received_by, notes, created_at")
            .eq("case_id", case["id"])
            .in_("event_type", ["issued", "dispatched", "delivered"])
            .order("created_at")
            .execute()
            .data
            or []
        )
        actor_ids = list({e["actor_id"] for e in events if e.get("actor_id")})
        actors = (
            supabase_admin.table("profiles").select("id, display_name, first_name").in_("id", actor_ids).execute().data
            or []
        ) if actor_ids else []
        actors_by_id = {a["id"]: _display_name(a) for a in actors}
        for event in events:
            event["actor_name"] = actors_by_id.get(event.get("actor_id"), "Unknown user")

        return render_template(
            "dispatch_scan.html",
            case=case,
            label=label,
            events=events,
            profile=g.profile,
            patient_initials=_patient_initials(case.get("patient_name")),
        )
    except Exception:
        app.logger.exception("Unable to load dispatch scan page")
        return "Unable to load this delivery label. Check the server logs for details.", 500


@app.route("/d/<token>/dispatch", methods=["POST"])
@role_required("admin", "technician")
def dispatch_case(token):
    try:
        label = _load_dispatch_label(token)
        if not label or label["status"] != "active":
            return redirect(url_for("scan_dispatch_label", token=token))

        courier_name = request.form.get("courier_name", "").strip()[:120]
        if not courier_name:
            flash("Enter the courier's name before handing over the case.", "error")
            return redirect(url_for("scan_dispatch_label", token=token))

        case = _set_case_status(
            label["case_id"],
            ["ready_for_dispatch"],
            {"status": "in_transit", "dispatched_at": _now_iso()},
        )
        if not case:
            flash("This case has already been handed to a courier.", "error")
            return redirect(url_for("scan_dispatch_label", token=token))

        staff_name = _display_name(g.profile)
        _log_dispatch_event(case["id"], "dispatched", label["id"], courier_name=courier_name)
        log_case_status_change(case["id"], "Dispatched (in transit)", g.profile["id"], f"Handed to courier {courier_name} by {staff_name}")
        create_notification(
            user_id=case["dentist_id"],
            message=f"On its way: Case {case['case_number']} left the lab with {courier_name}. Scan the label when it arrives to confirm receipt.",
            link=url_for("scan_dispatch_label", token=token),
        )
        _notify_admins(f"Dispatched: Case {case['case_number']} was handed to {courier_name} by {staff_name}.")
        flash(f"Handed to {courier_name}. The clinic has been notified.", "success")
        return redirect(url_for("scan_dispatch_label", token=token))
    except Exception:
        app.logger.exception("Unable to dispatch case")
        return "Unable to record the handoff. Check the server logs for details.", 500


@app.route("/d/<token>/receive", methods=["POST"])
@role_required("dentist")
def receive_case(token):
    try:
        label = _load_dispatch_label(token)
        if not label or label["status"] != "active":
            return redirect(url_for("scan_dispatch_label", token=token))

        cases = (
            supabase_admin.table("dental_cases")
            .select("id, dentist_id, status")
            .eq("id", label["case_id"])
            .execute()
            .data
            or []
        )
        if not cases or cases[0].get("dentist_id") != g.profile["id"]:
            return redirect(url_for("scan_dispatch_label", token=token))
        was_scanned_out = cases[0].get("status") == "in_transit"

        received_by = request.form.get("received_by", "").strip()[:120] or _display_name(g.profile, "Clinic")
        case = _set_case_status(
            label["case_id"],
            DISPATCH_STATUSES,
            {"status": "delivered", "delivered_at": _now_iso()},
        )
        if not case:
            flash("This case was already confirmed as delivered.", "error")
            return redirect(url_for("scan_dispatch_label", token=token))

        skipped_note = None if was_scanned_out else "Received without a dispatch scan at the lab"
        _log_dispatch_event(case["id"], "delivered", label["id"], received_by=received_by, notes=skipped_note)
        log_case_status_change(case["id"], "Delivered", g.profile["id"], f"Received by {received_by}")

        clinic = g.profile.get("clinic_name") or _display_name(g.profile, "the clinic")
        message = f"Delivered: Case {case['case_number']} was received by {received_by} at {clinic}."
        if skipped_note:
            message += " It was never scanned out at the lab."
        _notify_admins(message)
        if case.get("technician_id"):
            create_notification(
                user_id=case["technician_id"],
                message=f"Delivered: Case {case['case_number']} reached {clinic}.",
                link="/technician",
            )
        flash("Receipt confirmed. The lab has been notified.", "success")
        return redirect(url_for("scan_dispatch_label", token=token))
    except Exception:
        app.logger.exception("Unable to confirm receipt")
        return "Unable to confirm receipt. Check the server logs for details.", 500

# =========================
# ADMIN: ASSIGN CASE TO TECH
# =========================
# =========================
# ADMIN: ASSIGN CASE TO TECHNICIAN
# =========================
@app.route("/admin/cases/<case_id>/assign", methods=["POST"])
def admin_assign_case(case_id):
    admin_id = session.get("user_id")
    if not admin_id:
        return redirect(url_for("login"))

    try:
        admin_res = supabase_admin.table("profiles").select("role, status").eq("id", admin_id).execute()
        if not admin_res.data or admin_res.data[0]["role"] != "admin" or admin_res.data[0].get("status") != "approved":
            return "Access denied.", 403

        technician_id = request.form.get("technician_id")
        if not technician_id:
            return "Technician selection is required.", 400
        tech_res = supabase_admin.table("profiles").select("role, status").eq("id", technician_id).execute()
        if not tech_res.data or tech_res.data[0].get("role") != "technician" or tech_res.data[0].get("status") != "approved":
            return "Choose an approved technician.", 400

        # Fetch Case info for notification; only incoming (submitted) cases are assigned here.
        case_res = supabase_admin.table("dental_cases").select("case_number, status").eq("id", case_id).execute()
        if not case_res.data:
            return "Case not found.", 404
        if case_res.data[0].get("status") != "submitted":
            return "This case was already assigned or has moved on. Use Reassign on the production board instead.", 409
        case_number = case_res.data[0]["case_number"]

        # Update case status to assigned and set technician_id
        updated = supabase_admin.table("dental_cases").update({
            "technician_id": technician_id,
            "status": "assigned"
        }).eq("id", case_id).eq("status", "submitted").execute().data or []
        if not updated:
            return "This case was assigned by someone else meanwhile.", 409
        log_case_status_change(case_id, "Assigned", admin_id, None)

        # NOTIFY ONLY THE SPECIFIC ASSIGNED TECHNICIAN
        create_notification(
            user_id=technician_id,
            message=f"New Case Assigned: You have been assigned Case {case_number}.",
            link=f"/technician/cases/{case_id}"
        )

        return redirect(url_for("admin_dashboard"))

    except Exception as e:
        return f"Assign case error: {str(e)}", 500

# =========================
# INVENTORY MANAGEMENT ROUTES
# =========================
# =========================
# ADMIN: VIEW INVENTORY
# =========================
# =========================
# ADMIN: INVENTORY DASHBOARD
# =========================
@app.route("/admin/inventory")
def admin_inventory():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    try:
        # 1. Verify admin access & get profile info
        profile_res = supabase_admin.table("profiles").select("*").eq("id", user_id).execute()
        profiles = profile_res.data or []
        if not profiles or profiles[0].get("role") != "admin" or profiles[0].get("status") != "approved":
            return "Access denied.", 403

        profile = profiles[0]

        # 2. Update session heartbeat
        from datetime import datetime
        now_iso = datetime.utcnow().isoformat()
        try:
            supabase_admin.table("profiles").update({"last_seen": now_iso}).eq("id", user_id).execute()
        except Exception:
            pass

        # 3. Fetch inventory items
        inv_res = supabase_admin.table("inventory").select("*").order("item_name").execute()
        inventory_items = inv_res.data or []

        # ==========================================
        # MACHINE LEARNING FORECASTER INTEGRATION (Idea C)
        # ==========================================
        try:
            # Fetch historical audit/usage logs from Supabase
            logs_res = supabase_admin.table("inventory_logs").select("*").execute()
            historical_logs = logs_res.data or []
        except Exception:
            historical_logs = []

        # Run the forecaster class
        from ml_models.forecaster import InventoryForecaster
        forecaster = InventoryForecaster(inventory_items, historical_logs)
        predictions = forecaster.predict_stockouts()
        
        # Build a quick lookup dictionary: { item_id: prediction_dict }
        pred_map = {p['item_id']: p for p in predictions}
        # ==========================================

        # 4. Fetch notifications for layout consistency
        notif_res = (
            supabase_admin
            .table("notifications")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(5)
            .execute()
        )
        notifications = notif_res.data or []

        # 5. Fetch Restock Requests (from material_requests) for sidebar badge & modal
        restock_res = (
            supabase_admin
            .table("material_requests")
            .select("*, profiles(display_name), dental_cases(case_number)")
            .eq("status", "pending")
            .order("created_at", desc=True)
            .execute()
        )
        restock_requests = restock_res.data or []

        # 6. Fetch Unassigned Cases for sidebar badge & modal
        unassigned_res = (
            supabase_admin
            .table("dental_cases")
            .select("*")
            .eq("status", "submitted")
            .order("created_at", desc=True)
            .execute()
        )
        unassigned_cases = unassigned_res.data or []

        # 7. Fetch Technicians & compute active workloads in bulk
        techs_res = (
            supabase_admin
            .table("profiles")
            .select("id, display_name, first_name")
            .eq("role", "technician")
            .eq("status", "approved")
            .execute()
        )
        technicians = techs_res.data or []
        tech_ids = [t["id"] for t in technicians]

        active_statuses = [
            "assigned", "in_progress", "cad_designing", "wax_up_or_try_in",
            "milling_or_printing", "packing_and_curing", "finishing_and_quality_check", "needs_revision"
        ]

        if tech_ids:
            workload_cases_res = (
                supabase_admin
                .table("dental_cases")
                .select("technician_id")
                .in_("technician_id", tech_ids)
                .in_("status", active_statuses)
                .execute()
            )
            workload_counts = {}
            for w in (workload_cases_res.data or []):
                t_id = w.get("technician_id")
                workload_counts[t_id] = workload_counts.get(t_id, 0) + 1

            for tech in technicians:
                tech["active_workload"] = workload_counts.get(tech["id"], 0)
        else:
            for tech in technicians:
                tech["active_workload"] = 0

        # 8. Fetch Milling & Finishing stages for audio chime triggers
        milling_res = (
            supabase_admin
            .table("dental_cases")
            .select("id")
            .eq("status", "milling_or_printing")
            .execute()
        )
        milling_cases = milling_res.data or []

        qc_res = (
            supabase_admin
            .table("dental_cases")
            .select("id")
            .eq("status", "finishing_and_quality_check")
            .execute()
        )
        qc_cases = qc_res.data or []

        return render_template(
            "admin_inventory.html",
            profile=profile,
            inventory=inventory_items,
            pred_map=pred_map,  # Passed to HTML for forecasting UI display
            log_counts={item["id"]: sum(1 for log in historical_logs if log.get("inventory_id") == item["id"]) for item in inventory_items},
            notifications=notifications,
            restock_requests=restock_requests,
            unassigned_cases=unassigned_cases,
            technicians=technicians,
            milling_cases=milling_cases,
            qc_cases=qc_cases
        )
    except Exception as e:
        return f"Inventory error: {str(e)}", 500


# =========================
# ADMIN: ADD INVENTORY ITEM
# =========================
@app.route("/admin/inventory/add", methods=["POST"])
def add_inventory_item():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    try:
        item_name = request.form.get("item_name", "").strip()
        category = request.form.get("category", "").strip()
        quantity = float(request.form.get("quantity", 0))
        unit = request.form.get("unit", "").strip()
        min_threshold = float(request.form.get("minimum_threshold", 5))
        supplier_name = request.form.get("supplier_name", "").strip() or None
        unit_cost = float(request.form.get("unit_cost", 0.00))

        if not item_name or not category or not unit:
            return "All fields are required.", 400

        # Insert Item
        inv_res = supabase_admin.table("inventory").insert({
            "item_name": item_name,
            "category": category,
            "quantity": quantity,
            "unit": unit,
            "minimum_threshold": min_threshold,
            "supplier_name": supplier_name,
            "unit_cost": unit_cost
        }).execute()

        if inv_res.data:
            item_id = inv_res.data[0]["id"]
            # Log initial restock
            supabase_admin.table("inventory_logs").insert({
                "inventory_id": item_id,
                "user_id": user_id,
                "action_type": "restock",
                "quantity_changed": quantity,
                "previous_quantity": 0,
                "new_quantity": quantity,
                "notes": "Initial stock entry"
            }).execute()

        return redirect(url_for("admin_inventory"))
    except Exception as e:
        return f"Add item error: {str(e)}", 500


# =========================
# ADMIN: DELETE INVENTORY ITEM
# =========================
@app.route("/admin/inventory/<item_id>/delete", methods=["POST"])
@role_required("admin")
def delete_inventory_item(item_id):
    try:
        items = supabase_admin.table("inventory").select("id, item_name").eq("id", item_id).execute().data or []
        if not items:
            flash("This material was already deleted or no longer exists. Nothing was changed.", "inventory-stale")
            return redirect(url_for("admin_inventory"))
        item_name = items[0]["item_name"]

        # Its stock-change records go too, so no log points at a missing material.
        log_count = (
            supabase_admin.table("inventory_logs").select("id", count="exact").eq("inventory_id", item_id).limit(1).execute().count
            or 0
        )
        supabase_admin.table("inventory_logs").delete().eq("inventory_id", item_id).execute()
        supabase_admin.table("inventory").delete().eq("id", item_id).execute()
        app.logger.info("Inventory item %s (%s) deleted by %s with %s log entries", item_id, item_name, g.profile["id"], log_count)

        records = f" and its {log_count} stock-change record{'s' if log_count != 1 else ''}" if log_count else ""
        flash(f"'{item_name}'{records} {'were' if log_count else 'was'} removed from inventory.", "inventory-deleted")
        return redirect(url_for("admin_inventory"))
    except Exception:
        app.logger.exception("Unable to delete inventory item")
        return "Unable to delete this material. Check the server logs for details.", 500


# =========================
# ADMIN: UPDATE STOCK QUANTITY
# =========================
@app.route("/admin/inventory/update/<item_id>", methods=["POST"])
def update_inventory_stock(item_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    try:
        # Verify admin
        profile_res = supabase_admin.table("profiles").select("role, status").eq("id", user_id).execute()
        if not profile_res.data or profile_res.data[0].get("role") != "admin" or profile_res.data[0].get("status") != "approved":
            return "Access denied.", 403

        action = request.form.get("action")
        amount = float(request.form.get("amount", 0))

        if amount <= 0:
            return "Invalid quantity amount.", 400

        # Fetch current item stock
        item_res = supabase_admin.table("inventory").select("*").eq("id", item_id).execute()
        if not item_res.data:
            return "Item not found.", 404

        item = item_res.data[0]
        current_qty = float(item["quantity"])

        # Calculate new quantity based on action
        if action == "add":
            new_qty = round(current_qty + amount, 2)
            quantity_changed = amount
            action_type = "restock"
            notes = f"Manual restock of +{amount}"
        elif action == "subtract":
            if current_qty - amount < 0:
                new_qty = 0.00
                quantity_changed = -current_qty
                notes = f"Manual adjustment: attempted -{amount}, capped at 0 to avoid negative inventory."
            else:
                new_qty = round(current_qty - amount, 2)
                quantity_changed = -amount
                action_type = "manual_deduction"
                notes = f"Manual usage of -{amount}"
        else:
            return "Invalid action type.", 400

        # Update inventory item quantity
        supabase_admin.table("inventory").update({"quantity": new_qty}).eq("id", item_id).execute()

        # Insert audit log entry
        supabase_admin.table("inventory_logs").insert({
            "inventory_id": item_id,
            "user_id": user_id,
            "action_type": action_type,
            "quantity_changed": quantity_changed,
            "previous_quantity": current_qty,
            "new_quantity": new_qty,
            "notes": notes
        }).execute()

        return redirect(url_for("admin_inventory"))

    except Exception as e:
        return f"Stock update error: {str(e)}", 500


# =========================
# MACHINE LEARNING
# =========================
@app.route('/admin/inventory/forecast')
def inventory_forecast():
    # 1. Fetch your actual inventory items and logs from your database
    # (Adjust query according to your SQLAlchemy models, e.g., Inventory.query.all())
    inventory_items = [
        {"id": item.id, "item_name": item.item_name, "quantity": item.quantity} 
        for item in Inventory.query.all()
    ]
    
    historical_logs = [
        {
            "inventory_id": log.inventory_id, 
            "quantity_changed": log.quantity_changed, 
            "created_at": log.created_at
        } 
        for log in InventoryLog.query.all()
    ]

    # 2. Run the forecaster class we just created
    forecaster = InventoryForecaster(inventory_items, historical_logs)
    predictions = forecaster.predict_stockouts()

    # 3. Pass predictions to your admin inventory HTML page
    return render_template('admin_inventory.html', predictions=predictions, inventory=Inventory.query.all(), profile=...)

@app.route("/admin/forecast-analytics")
def admin_forecast_analytics():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    try:
        # 1. Verify admin access & profile
        profile_res = supabase_admin.table("profiles").select("*").eq("id", user_id).execute()
        profiles = profile_res.data or []
        if not profiles or profiles[0].get("role") != "admin" or profiles[0].get("status") != "approved":
            return "Access denied.", 403

        profile = profiles[0]

        # 2. Fetch inventory items and historical logs
        inv_res = supabase_admin.table("inventory").select("*").order("item_name").execute()
        inventory_items = inv_res.data or []

        logs_res = supabase_admin.table("inventory_logs").select("*").execute()
        historical_logs = logs_res.data or []

        # 3. Fetch active Kanban cases (Pipeline-Awareness)
        active_statuses = [
            "assigned", "in_progress", "cad_designing", "wax_up_or_try_in",
            "milling_or_printing", "packing_and_curing", "finishing_and_quality_check", "needs_revision"
        ]
        cases_res = (
            supabase_admin.table("dental_cases")
            .select("*")
            .in_("status", active_statuses)
            .execute()
        )
        active_cases = cases_res.data or []

        # 4. Run ML Forecaster
        from ml_models.forecaster import InventoryForecaster
        forecaster = InventoryForecaster(inventory_items, historical_logs)
        predictions = forecaster.predict_stockouts(active_cases=active_cases)

        # 5. Automated Database Notification Trigger for Urgent Shortages
        for p in predictions:
            if p['needs_urgent_reorder']:
                msg = f"Critical Stock Alert: '{p['item_name']}' is projected to run out in {p['estimated_days_left']} days (Lead time: {p['lead_time_days']}d)."
                
                # Check if a similar notification already exists recently to avoid spamming
                existing_notif = (
                    supabase_admin.table("notifications")
                    .select("id")
                    .eq("user_id", user_id)
                    .ilike("message", f"%{p['item_name']}%")
                    .execute()
                )
                
                if not existing_notif.data:
                    supabase_admin.table("notifications").insert({
                        "user_id": user_id,
                        "message": msg,
                        "link": url_for("admin_forecast_analytics")
                    }).execute()

        # 6. Fetch layout data
        notif_res = supabase_admin.table("notifications").select("*").eq("user_id", user_id).order("created_at", desc=True).limit(5).execute()
        notifications = notif_res.data or []

        restock_res = supabase_admin.table("material_requests").select("*, profiles(display_name), dental_cases(case_number)").eq("status", "pending").order("created_at", desc=True).execute()
        restock_requests = restock_res.data or []

        unassigned_res = supabase_admin.table("dental_cases").select("*").eq("status", "submitted").order("created_at", desc=True).execute()
        unassigned_cases = unassigned_res.data or []

        return render_template(
            "admin_forecast.html",
            profile=profile,
            predictions=predictions,
            active_cases_count=len(active_cases),
            notifications=notifications,
            restock_requests=restock_requests,
            unassigned_cases=unassigned_cases
        )
    except Exception as e:
        return f"Forecast Analytics error: {str(e)}", 500

import csv
from flask import make_response

@app.route("/admin/forecast/quick-restock/<string:item_id>", methods=["POST"])
def admin_quick_restock(item_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))
    try:
        # Fetch current item quantity
        item_res = supabase_admin.table("inventory").select("*").eq("id", item_id).execute()
        if not item_res.data:
            return "Item not found", 404
        
        item = item_res.data[0]
        item_name = item.get("item_name", "Item")
        current_qty = item.get("quantity", 0)
        restock_qty = 10.0  # Standard automated restock batch increment

        # Update inventory stock in Supabase
        new_qty = current_qty + restock_qty
        supabase_admin.table("inventory").update({"quantity": new_qty}).eq("id", item_id).execute()

        # Log the restock action matching your audit log schema columns
        supabase_admin.table("inventory_logs").insert({
            "inventory_id": item_id,
            "quantity_changed": restock_qty,
            "action_type": "Restock",
            "notes": f"Automated forecast quick-restock of +{rest_qty if 'rest_qty' in locals() else 10}",
            "user_id": user_id
        }).execute()

        return redirect(url_for("admin_forecast_analytics"))
    except Exception as e:
        return f"Quick restock error: {str(e)}", 500

@app.route("/admin/forecast/export-csv")
def admin_forecast_export_csv():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))
    try:
        # Re-run forecaster data to generate export payload
        inv_res = supabase_admin.table("inventory").select("*").order("item_name").execute()
        inventory_items = inv_res.data or []
        logs_res = supabase_admin.table("inventory_logs").select("*").execute()
        historical_logs = logs_res.data or []

        from ml_models.forecaster import InventoryForecaster
        forecaster = InventoryForecaster(inventory_items, historical_logs)
        predictions = forecaster.predict_stockouts()

        # Build CSV response
        csv_data = "Item Name,Current Quantity,Daily Burn Rate,Estimated Days Left,Projected Stockout Date,Reorder Status\n"
        for p in predictions:
            status = "URGENT REORDER" if p['estimated_days_left'] <= 7 else "Stable"
            csv_data += f"\"{p['item_name']}\",{p['current_qty']},{p['daily_burn_rate']},{p['estimated_days_left'],},{p['projected_stockout_date']},{status}\n"

        response = make_response(csv_data)
        response.headers["Content-Disposition"] = "attachment; filename=dentalink_forecast_report.csv"
        response.headers["Content-Type"] = "text/csv"
        return response
    except Exception as e:
        return f"Export error: {str(e)}", 500
@app.route("/admin/forecast/report-preview")
def admin_forecast_report_preview():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))
    try:
        inv_res = supabase_admin.table("inventory").select("*").order("item_name").execute()
        inventory_items = inv_res.data or []
        logs_res = supabase_admin.table("inventory_logs").select("*").execute()
        historical_logs = logs_res.data or []

        from ml_models.forecaster import InventoryForecaster
        forecaster = InventoryForecaster(inventory_items, historical_logs)
        predictions = forecaster.predict_stockouts()

        # Render a dedicated professional report preview template
        return render_template("admin_forecast_report.html", predictions=predictions)
    except Exception as e:
        return f"Report preview error: {str(e)}", 500
# =========================
# START FLASK
# =========================

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))