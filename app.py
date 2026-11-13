import os
import json
import secrets
import uuid
import zipfile
from functools import wraps
from urllib.parse import quote

import qrcode
import qrcode.image.svg
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file, g, make_response, jsonify
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
def _normalize_app_url(raw):
    """The public address DentaLink links to (QR labels, emails), always with http(s)://.

    "dentalink.up.railway.app" becomes "https://dentalink.up.railway.app": without a scheme,
    phone cameras read a QR code as plain text instead of a link.
    """
    raw = (raw or "").strip().rstrip("/")
    if raw and "://" not in raw:
        local = raw.startswith(("localhost", "127.", "0.0.0.0", "192.168.", "10."))
        raw = f"{'http' if local else 'https'}://{raw}"
    return raw or None


def _is_local_url(url):
    """True for addresses a phone on another network can't reach (this computer, home Wi-Fi)."""
    from urllib.parse import urlparse
    host = (urlparse(url or "").hostname or "").lower()
    if host in ("localhost", "0.0.0.0") or host.startswith(("127.", "192.168.", "10.")):
        return True
    parts = host.split(".")
    return len(parts) == 4 and parts[0] == "172" and parts[1].isdigit() and 16 <= int(parts[1]) <= 31


APP_URL = _normalize_app_url(os.getenv("APP_URL"))

# Version of templates/_terms_content.html. Change it whenever the terms' wording changes, so each
# profile records which version the user accepted (profiles.terms_version).
TERMS_VERSION = "2026-11-04"
app.jinja_env.globals["terms_version"] = TERMS_VERSION
def _use_http1_for_database():
    """Make Supabase's database client (postgrest) use HTTP/1.1 instead of HTTP/2.

    postgrest shares one HTTP/2 connection between all request threads. On Windows, overlapping
    requests on it (e.g. a page load and a live-update poll) intermittently fail with
    "[WinError 10035] A non-blocking socket operation could not be completed immediately".
    With HTTP/1.1 each request takes its own connection from a pool, which avoids that.
    """
    from postgrest import SyncPostgrestClient
    from postgrest.utils import SyncClient

    def create_session(self, base_url, headers, timeout, verify=True, proxy=None):
        return SyncClient(base_url=base_url, headers=headers, timeout=timeout, verify=verify,
                          proxy=proxy, follow_redirects=True, http2=False)

    SyncPostgrestClient.create_session = create_session


_use_http1_for_database()  # must run before the clients below are created

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
    # Added later; listed after the tables they reference so a restore inserts them in order.
    "case_dispatches",
    "case_dispatch_events",
    "case_issue_reports",
    "job_order_history",
    "forecast_runs",
    "zirconia_discs",
    "zirconia_disc_usage",
)
# Tables every DentaLink backup has had; older backups may lack the ones added after them.
BACKUP_CORE_TABLES = BACKUP_TABLES[:9]
BACKUP_STORAGE_BUCKETS = ("dental-case-files",)
BACKUP_BUCKET = "system-backups"          # where saved backups (scheduled and manual) are kept
BACKUP_MAX_BYTES = 45 * 1024 * 1024       # Supabase's free plan accepts uploads up to 50 MB
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
    "signed_in_elsewhere": "Signed out: the account signed in on another device",
}

# Roles limited to one signed-in device at a time; a new sign-in signs the other device out.
SINGLE_SESSION_ROLES = ("admin",)

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
# A case can be flagged as rush from intake until it passes QC.
RUSHABLE_STATUSES = ("submitted",) + REASSIGNABLE_STATUSES

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
        app_url = _normalize_app_url(os.getenv("APP_URL")) or "http://localhost:5000"

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
        profile = _current_profile()
        if not profile:
            session.clear()
            return "Profile not found.", 404

        if profile.get("role") != "admin" or profile.get("status") != "approved":
            return _access_denied()

        # ==========================================
        # 2. ACTIVE SESSION HEARTBEAT
        # ==========================================
        # update_user_activity() (before every request) records last_seen once a minute.
        from datetime import datetime, timedelta

        # ==========================================
        # 3. DIRECTORY USERS & ONLINE METRIC
        # ==========================================
        all_response = supabase_admin.table("profiles").select("*").order("created_at", desc=True).execute()
        all_users = all_response.data or []

        pending_users = [u for u in all_users if u.get("status") == "pending"]
        approved_users = [u for u in all_users if u.get("status") == "approved"]
        rejected_users = [u for u in all_users if u.get("status") == "rejected"]

        # Online = seen in the last 5 minutes; counted from the profiles already loaded above.
        five_mins_ago = datetime.now(timezone.utc) - timedelta(minutes=5)
        online_count = sum(
            1 for u in all_users
            if (_parse_timestamp(u.get("last_seen")) or datetime.min.replace(tzinfo=timezone.utc)) >= five_mins_ago
        )

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

        # Active workflow cases (section 8 uses them too); loaded here so one teeth query
        # covers both the unassigned and the active cases.
        active_cases_query = supabase_admin.table("dental_cases").select("*")
        for closed_status in CLOSED_STATUSES:
            active_cases_query = active_cases_query.neq("status", closed_status)
        active_cases = active_cases_query.order("created_at", desc=True).execute().data or []
        active_case_ids = [c["id"] for c in active_cases]

        teeth_by_case = {}
        if unassigned_ids or active_case_ids:
            teeth_rows = (supabase_admin.table("case_teeth").select("*")
                          .in_("case_id", unassigned_ids + active_case_ids).execute().data or [])
            for t in teeth_rows:
                teeth_by_case.setdefault(t["case_id"], []).append(t)

        # The incoming list shows each case's summary only; its scans and photos open on the
        # case page, so they aren't loaded (or signed) here.
        for c in unassigned_cases:
            c["teeth"] = teeth_by_case.get(c["id"], [])
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

        # Every active status is also an open workflow status, so the active cases loaded
        # above already hold each technician's workload.
        workload_counts = {}
        for c in active_cases:
            if c.get("technician_id") in tech_ids and c.get("status") in active_statuses:
                workload_counts[c["technician_id"]] = workload_counts.get(c["technician_id"], 0) + 1
        for t in technicians:
            t["active_workload"] = workload_counts.get(t["id"], 0)

        # ==========================================
        # 8. ACTIVE WORKFLOW CASES WITH TEETH & CLINIC
        # ==========================================
        for case in active_cases:
            case["teeth"] = teeth_by_case.get(case["id"], [])

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
        # 9. RECENT NOTIFICATIONS (shared with the bell)
        # ==========================================
        notifications = notification_feed()["items"][:5]
        _prime_sidebar_counts(incoming=len(unassigned_cases), restock=len(restock_requests))

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


def _current_profile():
    """The signed-in user's profile row, read from the database at most once per request.

    Every database call costs a round trip (~200 ms from the Philippines to the Sydney
    project), and a page used to read the same profile up to three times: the one-device
    check, role_required and the view itself. They now share this copy.
    """
    user_id = session.get("user_id")
    if not user_id:
        return None
    cached = g.get("_current_profile_entry")
    if cached and cached[0] == user_id:
        return cached[1]
    rows = supabase_admin.table("profiles").select("*").eq("id", user_id).execute().data or []
    profile = rows[0] if rows else None
    g._current_profile_entry = (user_id, profile)
    return profile


@app.before_request
def enforce_single_device_sessions():
    """Sign out an admin browser whose sign-in was replaced by a newer one on another device."""
    user_id = session.get("user_id")
    if not user_id or request.endpoint in (None, "static", "login", "logout"):
        return None
    role = session.get("role")
    if role and role not in SINGLE_SESSION_ROLES:
        return None  # dentists and technicians may use several devices
    try:
        profile = _current_profile()
    except Exception:
        return None  # never lock anyone out over a failed check
    if not profile:
        return None
    session["role"] = profile.get("role")
    active = profile.get("active_session_id")
    if profile.get("role") not in SINGLE_SESSION_ROLES or not active or active == session.get("login_session_id"):
        return None

    _record_login_event("logout", session.get("login_email"), user_id, "signed_in_elsewhere")
    session.clear()
    flash("You were signed out because your admin account signed in on another device. "
          "If that wasn't you, sign in again and change your password.", "session")
    if request.path.startswith("/api/"):
        # Background checks (live updates) get JSON, so the page can send the user to sign in
        # without the fetch using up the message above.
        return app.response_class(json.dumps({"signed_out": True, "redirect": url_for("login")}),
                                  status=401, mimetype="application/json")
    return redirect(url_for("login"))


def admin_sidebar_counts():
    """Badge counts for the admin sidebar; called only by pages that render it.

    Pages that already loaded the incoming cases and restock requests hand their counts
    over with _prime_sidebar_counts(), which saves two database calls.
    """
    cached = g.get("_sidebar_counts")
    if cached is not None:
        return cached
    counts = {"incoming": 0, "restock": 0, "issues": 0}
    try:
        counts["incoming"] = _count("dental_cases", status="submitted")
        counts["restock"] = _count("material_requests", status="pending")
        counts["issues"] = _open_issue_count()
    except Exception:
        app.logger.exception("Unable to load sidebar counts")
    g._sidebar_counts = counts
    return counts


def _prime_sidebar_counts(incoming, restock):
    """Reuse counts a page already has: `incoming` submitted cases, `restock` pending requests."""
    g._sidebar_counts = {"incoming": incoming, "restock": restock, "issues": _open_issue_count()}


@app.context_processor
def inject_admin_sidebar_counts():
    return {"admin_sidebar_counts": admin_sidebar_counts, "notification_feed": notification_feed}


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


ROLE_HOME = {"admin": "admin_dashboard", "technician": "technician_dashboard", "dentist": "dentist_dashboard"}
ROLE_LABEL = {"admin": "an admin", "technician": "a technician", "dentist": "a dentist"}


def _access_denied(roles=None):
    """403 page saying who is signed in and who the page is for.

    The usual cause: several accounts signed in through tabs of one browser, which share one
    sign-in cookie, so the last sign-in replaced the others."""
    if request.path.startswith("/api/"):
        return app.response_class(json.dumps({"error": "access_denied"}), status=403, mimetype="application/json")
    if not roles:
        first = request.path.strip("/").split("/", 1)[0]
        roles = (first,) if first in ROLE_LABEL else ()
    try:
        profile = _current_profile()
    except Exception:
        profile = None
    approved = bool(profile) and profile.get("status") == "approved" and profile.get("role") in ROLE_HOME
    needed = " or ".join(ROLE_LABEL[r] for r in roles if r in ROLE_LABEL)
    return render_template(
        "access_denied.html",
        profile=profile,
        approved=approved,
        needed=needed,
        home_url=url_for(ROLE_HOME[profile["role"]]) if approved else None,
    ), 403


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

            profile = _current_profile()
            if not profile or profile.get("status") != "approved" or profile.get("role") not in roles:
                return _access_denied(roles)
            if request.method == "POST" and not _valid_csrf_token():
                return "This form has expired. Reload the page and try again.", 400

            g.profile = profile
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
            return _access_denied()
        return render_template(
            "admin_backup.html",
            profile=profile,
            csrf_token=_csrf_token(),
            **_backup_page_context(),
        )
    except Exception:
        app.logger.exception("Unable to open the admin backup page")
        return "Unable to load backup page.", 500


def _build_backup_zip():
    """The full backup ZIP (every backed-up table, every case file, a manifest) as bytes.
    Used by the Download button and by scheduled backups alike."""
    import io
    buffer = io.BytesIO()
    created_at = datetime.utcnow().isoformat() + "Z"
    storage_manifest = []
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for table_name in BACKUP_TABLES:
            rows = _backup_table_rows(table_name)
            archive.writestr(f"database/{table_name}.json", json.dumps(rows, ensure_ascii=False, default=str))

        for bucket_name in BACKUP_STORAGE_BUCKETS:
            bucket = supabase_admin.storage.from_(bucket_name)
            for object_path, metadata in _storage_objects(bucket):
                if not _safe_archive_path(object_path):
                    raise ValueError(f"Unsafe storage object path in {bucket_name}")
                archive.writestr(f"storage/{bucket_name}/{object_path}", bucket.download(object_path))
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
                "values": {"APP_URL": APP_URL, "SUPABASE_URL": SUPABASE_URL},
                "redacted": ["FLASK_SECRET_KEY", "SUPABASE_PUBLISHABLE_KEY", "SUPABASE_SERVICE_ROLE_KEY", "BREVO_SMTP_PASS"],
            },
            "encrypted": False,
            "limitations": [
                "This archive does not include database schema or Supabase Auth accounts.",
                "Restore merges records into the existing schema and does not delete newer records.",
            ],
        }
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return buffer.getvalue()


# ---------- Saved backups (system-backups bucket) and the schedule ----------
BACKUP_FREQUENCIES = ("off", "daily", "weekly", "monthly")
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
BACKUP_STALE_MINUTES = 30   # a "running" backup older than this died with its server process


def _backup_settings():
    rows = supabase_admin.table("backup_settings").select("*").eq("id", 1).execute().data or []
    return rows[0] if rows else {"frequency": "off", "time_of_day": "12:00", "weekday": 6, "month_day": 1, "keep_count": 7}


def _backup_slot(settings, now, step):
    """The scheduled time at or before `now` (step=0) or the one after it (step=1), in lab time;
    None when backups are off."""
    freq = settings.get("frequency")
    if freq not in ("daily", "weekly", "monthly"):
        return None
    hour, minute = (int(x) for x in (settings.get("time_of_day") or "12:00").split(":"))
    local = now.astimezone(LAB_TZ)

    def at(d):
        return datetime(d.year, d.month, d.day, hour, minute, tzinfo=LAB_TZ)

    if freq == "daily":
        slot = at(local.date())
        last = slot if slot <= local else slot - timedelta(days=1)
        return last if step == 0 else last + timedelta(days=1)
    if freq == "weekly":
        day = local.date() - timedelta(days=(local.weekday() - int(settings.get("weekday", 6))) % 7)
        slot = at(day)
        last = slot if slot <= local else slot - timedelta(days=7)
        return last if step == 0 else last + timedelta(days=7)
    day_no = int(settings.get("month_day", 1))
    last = at(local.date().replace(day=day_no))
    if last > local:   # this month's date hasn't come yet: the last one was last month's
        last = at((local.date().replace(day=1) - timedelta(days=1)).replace(day=day_no))
    if step == 0:
        return last
    return at((last.date().replace(day=28) + timedelta(days=4)).replace(day=day_no))


def _save_backup(kind, data, user_id=None, due_at=None, run_id=None):
    """Store a backup ZIP in the system-backups bucket and record it; returns the run row.
    A scheduled run's row already exists (it was claimed first); otherwise one is created."""
    if len(data) > BACKUP_MAX_BYTES:
        raise ValueError(f"The backup is too large to store ({len(data) / 1048576:.1f} MB; the limit is "
                         f"{BACKUP_MAX_BYTES // 1048576} MB). Download backups from this page instead.")
    stamp = datetime.now(LAB_TZ)
    # The short suffix keeps two backups made in the same second from overwriting each other.
    path = f"{kind}/dentalink-backup-{stamp:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.zip"
    supabase_admin.storage.from_(BACKUP_BUCKET).upload(path, data, {"content-type": "application/zip", "upsert": "true"})
    done = {"status": "succeeded", "finished_at": _now_iso(), "file_path": path, "size_bytes": len(data)}
    if run_id:
        row = supabase_admin.table("backup_runs").update(done).eq("id", run_id).execute().data[0]
    else:
        row = supabase_admin.table("backup_runs").insert(dict(done, kind=kind, created_by=user_id, due_at=due_at)).execute().data[0]
    _prune_backups()
    return row


def _prune_backups():
    """Keep only the newest saved backups (the schedule's keep_count); delete older files."""
    keep = int(_backup_settings().get("keep_count") or 7)
    saved = (supabase_admin.table("backup_runs").select("id, file_path").eq("status", "succeeded")
             .order("started_at", desc=True).execute().data or [])
    saved = [r for r in saved if r.get("file_path")]
    for old in saved[keep:]:
        try:
            supabase_admin.storage.from_(BACKUP_BUCKET).remove([old["file_path"]])
        except Exception:
            app.logger.exception("Unable to delete old backup %s", old["file_path"])
        supabase_admin.table("backup_runs").update({"file_path": None}).eq("id", old["id"]).execute()


def _admin_emails():
    """(email, name) for every approved admin: the address each one signs in with."""
    admins = (supabase_admin.table("profiles").select("id, display_name, first_name")
              .eq("role", "admin").eq("status", "approved").execute().data or [])
    out = []
    for a in admins:
        try:
            user = supabase_admin.auth.admin.get_user_by_id(a["id"])
            email = getattr(getattr(user, "user", None), "email", None)
            if email:
                out.append((email, _display_name(a, "Administrator")))
        except Exception:
            app.logger.exception("Unable to look up admin email for %s", a["id"])
    return out


def _send_plain_email(recipient, name, subject, html):
    """Send one email through Brevo (the same account as the approval emails)."""
    api_key = os.getenv("BREVO_SMTP_PASS")
    sender = os.getenv("BREVO_SENDER_EMAIL", "jiclao24@gmail.com")
    if not api_key or not recipient:
        app.logger.warning("Email not sent (%s): Brevo key or recipient missing", subject)
        return
    payload = {"sender": {"name": "DentaLink LIS", "email": sender}, "to": [{"email": recipient, "name": name}],
               "subject": subject, "htmlContent": html}
    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email", data=json.dumps(payload).encode("utf-8"),
        headers={"accept": "application/json", "api-key": api_key, "content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def _alert_backup_failed(kind, due_at, error):
    """Tell every admin, in the app and by email, that a backup failed."""
    from markupsafe import escape
    when = (_parse_timestamp(due_at) or datetime.now(timezone.utc)).astimezone(LAB_TZ)
    label = f"The scheduled backup for {when:%b %d, %Y %I:%M %p}" if kind == "scheduled" else "A backup"
    _notify_admins(f"{label} failed: {error} Open Backup & Recovery to try again.", link="/admin/backup")
    app_url = APP_URL or "http://localhost:5000"
    for email, name in _admin_emails():
        html = (f"<p>Hi {escape(name)},</p><p>{label} of DentaLink <strong>failed</strong>.</p>"
                f"<p style=\"color:#B91C1C\">{escape(error)}</p>"
                f"<p>No data was lost, but there is no new backup. Open "
                f"<a href=\"{app_url}/admin/backup\">Backup &amp; Recovery</a> to back up now; "
                f"the next scheduled backup will also run as planned.</p><p>DentaLink LIS</p>")
        try:
            _send_plain_email(email, name, "DentaLink backup failed", html)
        except Exception:
            app.logger.exception("Unable to email the backup failure to %s", email)


def run_due_backup(now=None):
    """Make the scheduled backup if one is due and not done yet. Called every minute.

    Catch-up: only the most recent scheduled time is checked, so after downtime the missed
    backup runs once as soon as the server is back (not once per missed day). A time before
    the schedule was last saved doesn't count, so saving a schedule doesn't start one.
    Each time is claimed with a unique row first, so two app processes never both run it.
    Returns the run row it made, or None.
    """
    now = now or datetime.now(timezone.utc)
    settings = _backup_settings()
    slot = _backup_slot(settings, now, 0)
    if slot is None:
        return None
    saved_at = _parse_timestamp(settings.get("updated_at"))
    if saved_at and slot < saved_at:
        return None
    # A run left "running" by a server that stopped mid-backup is marked failed, so it shows.
    stale = (now - timedelta(minutes=BACKUP_STALE_MINUTES)).isoformat()
    (supabase_admin.table("backup_runs")
     .update({"status": "failed", "finished_at": _now_iso(), "error": "The server stopped before the backup finished."})
     .eq("status", "running").lt("started_at", stale).execute())
    due = slot.astimezone(timezone.utc).isoformat()
    if supabase_admin.table("backup_runs").select("id").eq("kind", "scheduled").eq("due_at", due).execute().data:
        return None
    try:
        run = supabase_admin.table("backup_runs").insert({"kind": "scheduled", "due_at": due, "status": "running"}).execute().data[0]
    except Exception:
        return None   # another process claimed this time first
    try:
        return _save_backup("scheduled", _build_backup_zip(), due_at=due, run_id=run["id"])
    except Exception as error:
        app.logger.exception("Scheduled backup failed")
        message = str(error) if isinstance(error, ValueError) else "Something went wrong while creating or saving the backup."
        (supabase_admin.table("backup_runs").update({"status": "failed", "finished_at": _now_iso(), "error": message})
         .eq("id", run["id"]).execute())
        _alert_backup_failed("scheduled", due, message)
        return None


def _backup_scheduler():
    """Background loop: check for a due backup every minute, for the life of the process."""
    import time
    time.sleep(20)   # let the app finish starting
    while True:
        try:
            run_due_backup()
        except Exception:
            app.logger.exception("Backup scheduler check failed")
        time.sleep(60)


def start_backup_scheduler():
    if os.getenv("DISABLE_BACKUP_SCHEDULER") == "1" or getattr(start_backup_scheduler, "started", False):
        return
    start_backup_scheduler.started = True
    threading.Thread(target=_backup_scheduler, name="backup-scheduler", daemon=True).start()


@app.route("/admin/backup/download", methods=["POST"])
def admin_backup_download():
    """Make a backup now: saved to the backup list and downloaded to this computer."""
    import io
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))
    try:
        if not _approved_admin_profile(user_id):
            return _access_denied()
        if not _valid_csrf_token():
            return "Invalid backup request. Reload the page and try again.", 400
        data = _build_backup_zip()
    except Exception:
        app.logger.exception("Unable to create an admin backup")
        return "Backup failed. Check the server logs for details.", 500
    try:
        _save_backup("manual", data, user_id=user_id)
    except Exception:
        # The download still goes ahead; the saved list just won't have this copy.
        app.logger.exception("Unable to save a copy of the manual backup")
    response = send_file(io.BytesIO(data), mimetype="application/zip", as_attachment=True,
                         download_name=f"dentalink-backup-{datetime.now(LAB_TZ):%Y%m%d-%H%M%S}.zip")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/admin/backup/schedule", methods=["POST"])
@role_required("admin")
def admin_backup_schedule():
    import re
    freq = request.form.get("frequency")
    time_of_day = (request.form.get("time_of_day") or "").strip()
    try:
        weekday = int(request.form.get("weekday", 6))
        month_day = int(request.form.get("month_day", 1))
        keep = int(request.form.get("keep_count", 7))
    except ValueError:
        weekday, month_day, keep = -1, 0, 0
    if (freq not in BACKUP_FREQUENCIES or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time_of_day)
            or not 0 <= weekday <= 6 or not 1 <= month_day <= 28 or not 1 <= keep <= 30):
        flash("Check the schedule: choose how often, a time, and a day where needed.", "backup-input")
        return redirect(url_for("admin_backup"))
    supabase_admin.table("backup_settings").upsert({
        "id": 1, "frequency": freq, "time_of_day": time_of_day, "weekday": weekday, "month_day": month_day,
        "keep_count": keep, "updated_at": _now_iso(), "updated_by": g.profile["id"],
    }).execute()
    _prune_backups()   # a smaller "keep" takes effect right away
    nxt = _backup_slot({"frequency": freq, "time_of_day": time_of_day, "weekday": weekday, "month_day": month_day},
                       datetime.now(timezone.utc), 1)
    flash("Automatic backups are turned off." if not nxt else
          f"Automatic backups saved. The next one is on {nxt:%a, %b %d, %Y at %I:%M %p}.", "backup-saved")
    return redirect(url_for("admin_backup"))


@app.route("/admin/backup/files/<run_id>")
@role_required("admin")
def admin_backup_file(run_id):
    """Download a saved backup through a short-lived signed link to the private bucket."""
    rows = supabase_admin.table("backup_runs").select("file_path").eq("id", run_id).execute().data or []
    if not rows or not rows[0].get("file_path"):
        flash("That backup is no longer stored.", "backup-input")
        return redirect(url_for("admin_backup"))
    path = rows[0]["file_path"]
    signed = supabase_admin.storage.from_(BACKUP_BUCKET).create_signed_url(path, 120)
    url = (signed.get("signedURL") or signed.get("signedUrl")) if isinstance(signed, dict) else signed
    return redirect(f"{url}{'&' if '?' in url else '?'}download={quote(path.rsplit('/', 1)[-1])}")


def _schedule_label(settings):
    """e.g. "Every Sunday at 11:00 PM"; "Off" when automatic backups are off."""
    freq = settings.get("frequency")
    if freq not in ("daily", "weekly", "monthly"):
        return "Off"
    t = datetime.strptime(settings.get("time_of_day") or "12:00", "%H:%M").strftime("%I:%M %p").lstrip("0")
    if freq == "daily":
        return f"Every day at {t}"
    if freq == "weekly":
        return f"Every {WEEKDAY_NAMES[int(settings.get('weekday', 6))]} at {t}"
    d = int(settings.get("month_day", 1))
    suffix = "th" if 11 <= d <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")
    return f"Every month on the {d}{suffix} at {t}"


def _backup_page_context():
    """Schedule, next and last backups, and the saved list, for the Backup page."""
    try:
        settings = _backup_settings()
        runs = (supabase_admin.table("backup_runs").select("*").order("started_at", desc=True)
                .limit(40).execute().data or [])
    except Exception:
        app.logger.warning("backup tables unavailable; run the scheduled backups migration")
        return {"backup_ready": False}
    for r in runs:
        r["started_local"] = (_parse_timestamp(r.get("started_at")) or datetime.now(timezone.utc)).astimezone(LAB_TZ)
        r["due_local"] = _parse_timestamp(r["due_at"]).astimezone(LAB_TZ) if r.get("due_at") else None
        size = r.get("size_bytes") or 0
        r["size_label"] = (f"{size / 1048576:.1f} MB" if size >= 1048576 else f"{max(size // 1024, 1)} KB") if size else ""
    scheduled = [r for r in runs if r["kind"] == "scheduled"]
    return {
        "backup_ready": True,
        "settings": settings,
        "schedule_label": _schedule_label(settings),
        "next_backup": _backup_slot(settings, datetime.now(timezone.utc), 1),
        "last_scheduled": scheduled[0] if scheduled else None,
        "saved_backups": [r for r in runs if r["status"] == "succeeded" and r.get("file_path")],
        "recent_failures": [r for r in runs if r["status"] == "failed"][:3],
        "weekday_names": WEEKDAY_NAMES,
    }


@app.route("/admin/backup/restore", methods=["POST"])
def admin_backup_restore():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    try:
        if not _approved_admin_profile(user_id):
            return _access_denied()
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
            # Older backups hold fewer tables than today's list; any known subset with the core
            # tables is accepted.
            if (
                not isinstance(manifest, dict)
                or manifest.get("format") != "dentalink-app-backup"
                or manifest.get("version") != 1
                or not isinstance(manifest_tables, list)
                or len(set(manifest_tables)) != len(manifest_tables)
                or not set(manifest_tables) <= set(BACKUP_TABLES)
                or not set(BACKUP_CORE_TABLES) <= set(manifest_tables)
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

            for table_name in BACKUP_TABLES:   # dependency order, whatever order the backup lists
                rows = database_rows.get(table_name, [])
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
            return _access_denied()

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
            return _access_denied()
        if not _valid_csrf_token():
            return "This form has expired. Reload the page and try again.", 400

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

        # Milling, packing and finishing must log the material used (there is no "skip").
        # Rows are checked before anything changes, so a bad row never half-applies.
        material_entries = []
        if new_status in MATERIAL_LOG_STAGES:
            material_entries, material_error = _parse_material_log(request.form)
            if material_error:
                # Back to the page the technician came from, with the reason in a pop-up.
                from urllib.parse import urlparse
                back = urlparse(request.referrer or "").path
                flash(material_error, "material-blocked")
                return redirect(back if back.startswith("/technician") else url_for("technician_dashboard"))

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
        # MATERIAL USE: amounts from the usage rules (confirmed by the technician) and discs
        # ==========================================
        if material_entries:
            _apply_material_log(material_entries, case_id, case_number,
                                status_labels.get(new_status, new_status).replace("in ", "", 1), user_id)

        # 1. Notify the Dentist
        create_notification(
            user_id=dentist_id,
            message=f"Case Status Update: Case {case_number} is now {readable_status} (Tech: {tech_name}).",
            link=f"/dentist/cases/{case_id}"
        )

        # Admins aren't notified of routine stage moves: they follow the pipeline live on the
        # dashboard, and their bell is kept for things that need action (QC review, issues...).

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
            return _access_denied()

        new_tech_id = request.form.get("technician_id")
        if not new_tech_id:
            return "No technician selected.", 400

        # Fetch case and new tech info for logging/notification
        case_res = supabase_admin.table("dental_cases").select("case_number, status, technician_id").eq("id", case_id).execute()
        if not case_res.data:
            return "Case not found.", 404
        if case_res.data[0].get("status") not in REASSIGNABLE_STATUSES:
            flash(f"Case {case_res.data[0]['case_number']} has left production, so its technician can't be changed.", "reassign-stale")
            return redirect(url_for("admin_dashboard"))
        case_number = case_res.data[0]["case_number"]
        old_tech_id = case_res.data[0].get("technician_id")
        if old_tech_id == new_tech_id:
            flash(f"Case {case_number} is already assigned to that technician. Nothing was changed.", "reassign-stale")
            return redirect(url_for("admin_dashboard"))

        tech_res = supabase_admin.table("profiles").select("display_name, first_name, role, status").eq("id", new_tech_id).execute()
        if not tech_res.data or tech_res.data[0].get("role") != "technician" or tech_res.data[0].get("status") != "approved":
            return "Choose an approved technician.", 400
        tech_name = _display_name(tech_res.data[0], "the new technician")
        old_name = None
        if old_tech_id:
            old = supabase_admin.table("profiles").select("display_name, first_name").eq("id", old_tech_id).execute().data or []
            old_name = _display_name(old[0] if old else None, "the previous technician")

        # UPDATE ONLY THE TECHNICIAN_ID (Status is intentionally preserved)
        updated = supabase_admin.table("dental_cases").update({
            "technician_id": new_tech_id
        }).eq("id", case_id).in_("status", list(REASSIGNABLE_STATUSES)).execute().data or []
        if not updated:
            flash(f"Case {case_number} left production while you were choosing, so nothing was changed.", "reassign-stale")
            return redirect(url_for("admin_dashboard"))

        log_case_status_change(case_id, "Technician changed", user_id,
                               f"Reassigned from {old_name} to {tech_name}" if old_name else f"Assigned to {tech_name}")
        # Tell both technicians: the case moves to one bench and leaves the other.
        create_notification(
            user_id=new_tech_id,
            message=f"Case Reassigned: You have been assigned to take over Case {case_number}.",
            link="/technician"
        )
        if old_tech_id:
            create_notification(
                user_id=old_tech_id,
                message=f"Case {case_number} was reassigned to {tech_name}. It's no longer on your bench.",
                link="/technician"
            )

        flash(f"Case {case_number} now belongs to {tech_name}" + (f" (was {old_name})." if old_name else "."), "reassign-done")
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
        profile = _current_profile()
        if not profile or profile.get("role") != "admin" or profile.get("status") != "approved":
            return _access_denied()

        # 2. Session heartbeat: update_user_activity() records last_seen once a minute.

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
        notifications = notification_feed()["items"][:5]  # shared with the bell

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

        # 6–8. One case query covers the unassigned cases (sidebar badge & modal), each
        # technician's active workload, and the milling / finishing counts for the chime.
        active_statuses = [
            "assigned", "in_progress", "cad_designing", "wax_up_or_try_in",
            "milling_or_printing", "packing_and_curing", "finishing_and_quality_check", "needs_revision"
        ]
        open_cases = (
            supabase_admin
            .table("dental_cases")
            .select("*")
            .in_("status", ["submitted"] + active_statuses)
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
        unassigned_cases = [c for c in open_cases if c.get("status") == "submitted"]

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

        workload_counts = {}
        for c in open_cases:
            if c.get("technician_id") in tech_ids and c.get("status") in active_statuses:
                workload_counts[c["technician_id"]] = workload_counts.get(c["technician_id"], 0) + 1
        for tech in technicians:
            tech["active_workload"] = workload_counts.get(tech["id"], 0)

        milling_cases = [{"id": c["id"]} for c in open_cases if c.get("status") == "milling_or_printing"]
        qc_cases = [{"id": c["id"]} for c in open_cases if c.get("status") == "finishing_and_quality_check"]
        _prime_sidebar_counts(incoming=len(unassigned_cases), restock=len(restock_requests))

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
            return _access_denied()

        user_response = supabase_admin.table("profiles").select("id, role, status, display_name, first_name, clinic_name").eq("id", user_id).execute()
        users = user_response.data or []
        if not users:
            return "User not found.", 404

        user = users[0]
        if user["role"] == "admin":
            return "Admin accounts cannot be approved here.", 403

        # Dentists need a clinic name (clinic reports group by it); the admin can correct it here.
        changes = {"status": "approved", "approved_at": _now_iso(), "approved_by": admin_id}
        if user["role"] == "dentist":
            clinic = " ".join((request.form.get("clinic_name") or user.get("clinic_name") or "").split())[:120]
            if not clinic:
                flash("Enter the dentist's clinic name before approving the account.", "approve-input")
                return redirect(url_for("review_user", user_id=user_id))
            changes["clinic_name"] = clinic

        # Update status in database
        supabase_admin.table("profiles").update(changes).eq("id", user_id).execute()

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
            return _access_denied()

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
            return _access_denied()

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
        # 3. USER HEARTBEAT: update_user_activity() records last_seen once a minute.
        # =========================

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

        # Technician names for the case cards.
        tech_ids = list({c["technician_id"] for c in dental_cases if c.get("technician_id")})
        tech_names = {}
        if tech_ids:
            try:
                rows = supabase_admin.table("profiles").select("id, display_name, first_name, last_name").in_("id", tech_ids).execute().data or []
                tech_names = {r["id"]: _display_name(r, "Technician") for r in rows}
            except Exception:
                app.logger.exception("Unable to load technician names")
        for case in dental_cases:
            case["technician_name"] = tech_names.get(case.get("technician_id"))

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

        # Previous patients for the intake form's name autocomplete:
        # one entry per name (case and spacing ignored), most recent first.
        patient_history = {}
        for c in dental_cases:  # already newest first
            name = " ".join((c.get("patient_name") or "").split())
            if not name:
                continue
            key = name.casefold()
            entry = patient_history.get(key)
            if entry:
                entry["count"] += 1
            else:
                patient_history[key] = {
                    "name": name,
                    "count": 1,
                    "last_date": (c.get("created_at") or "")[:10],
                    "last_type": (c.get("case_type") or "").replace("_", " "),
                }
        patient_suggestions = list(patient_history.values())

        # =========================
        # 7. GET RECENT NOTIFICATIONS
        # =========================
        notifications = notification_feed()["items"][:5]  # shared with the bell

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
            patient_suggestions=patient_suggestions,
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
            return _access_denied()

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

        # Clinic reports group dentists by clinic, so a dentist must name theirs.
        if role == "dentist":
            clinic_name = " ".join((clinic_name or "").split())[:120]
            if not clinic_name:
                flash("Please enter your dental clinic's name.", "error")
                return render_template("register.html")

        # =========================
        # REQUIRE TERMS ACCEPTANCE
        # =========================
        if request.form.get("accept_terms") != "yes":
            flash("You must read and accept the Terms and Conditions and Data Privacy Notice to create an account.", "error")
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
                "status": "pending",
                "terms_version": TERMS_VERSION,
                "terms_accepted_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
            _notify_admins(
                f"New account waiting for approval: {display_name} ({role}{', ' + clinic_name if clinic_name else ''}).",
                link="/admin",
            )

            # =========================
            # SHOW EMAIL VERIFICATION
            # =========================
            # Remember the address in this browser's session: the "Resend" button can only
            # resend to the account this browser just registered, never to a typed-in address.
            session["verify_email"] = email
            session["verify_sent_at"] = datetime.now(timezone.utc).isoformat()
            return redirect(url_for("verify_email"))

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
# CONFIRMATION EMAIL: "check your inbox" page and resend
# =========================
VERIFY_RESEND_WAIT_SECONDS = 60   # Supabase also allows one resend per minute per address


def _verify_wait_seconds():
    """Seconds until this browser may ask for another confirmation email."""
    sent = _parse_timestamp(session.get("verify_sent_at"))
    if not sent:
        return 0
    left = VERIFY_RESEND_WAIT_SECONDS - (datetime.now(timezone.utc) - sent).total_seconds()
    return max(0, int(left + 0.999))


@app.route("/verify-email")
def verify_email():
    email = session.get("verify_email")
    if not email:
        return redirect(url_for("login"))
    return render_template("verify_email.html", email=email, wait_seconds=_verify_wait_seconds(),
                           resend_token=_csrf_token())


@app.route("/verify-email/resend", methods=["POST"])
def resend_verification_email():
    """Send the sign-up confirmation email again, to the address this browser just registered."""
    email = session.get("verify_email")
    if not email:
        return redirect(url_for("login"))
    if not _valid_csrf_token():
        flash("This page expired. Try the button again.", "verify-error")
        return redirect(url_for("verify_email"))
    wait = _verify_wait_seconds()
    if wait:
        flash(f"Please wait {wait} seconds before asking for another email.", "verify-wait")
        return redirect(url_for("verify_email"))
    try:
        supabase.auth.resend({"type": "signup", "email": email})
        session["verify_sent_at"] = datetime.now(timezone.utc).isoformat()
        flash("A new confirmation email is on its way. It can take 5–10 minutes to arrive.", "verify-sent")
    except Exception as error:
        text = str(error).lower()
        if "already" in text and "confirm" in text:
            session.pop("verify_email", None)
            flash("This email address is already confirmed. You can sign in once the lab approves your account.", "verify-sent")
            return redirect(url_for("login"))
        if "seconds" in text or "rate" in text or "429" in text:
            session["verify_sent_at"] = datetime.now(timezone.utc).isoformat()
            flash("The email service asks us to wait a minute between emails. Try again shortly.", "verify-wait")
        else:
            app.logger.exception("Unable to resend the confirmation email")
            flash("We couldn't send the email right now. Try again in a minute.", "verify-error")
    return redirect(url_for("verify_email"))

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
            session["role"] = profile["role"]
            _record_login_event("success", email, user_id)

            # One device per admin: this sign-in becomes the account's only valid session,
            # so any other browser signed in as this admin is signed out on its next request.
            if profile["role"] in SINGLE_SESSION_ROLES:
                try:
                    supabase_admin.table("profiles").update(
                        {"active_session_id": session["login_session_id"]}
                    ).eq("id", user_id).execute()
                except Exception:
                    app.logger.exception("Unable to record the active admin session (run migration 20261101020000)")

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
            return _access_denied()

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
        # Free the admin's one-device slot, unless a newer sign-in already holds it.
        if session.get("role") in SINGLE_SESSION_ROLES and session.get("login_session_id"):
            try:
                supabase_admin.table("profiles").update({"active_session_id": None}).eq("id", user_id).eq(
                    "active_session_id", session["login_session_id"]
                ).execute()
            except Exception:
                app.logger.exception("Unable to clear the active admin session")

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

            return _access_denied()


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
@role_required("technician")
def request_material(case_id):
    user_id = g.profile["id"]
    cases = (supabase_admin.table("dental_cases").select("case_number")
             .eq("id", case_id).eq("technician_id", user_id).execute().data or [])
    if not cases:
        return "This job order isn't assigned to you.", 404
    item_name = request.form.get("item_name", "").strip()[:120]
    if item_name:
        supabase_admin.table("material_requests").insert({
            "case_id": case_id,
            "technician_id": user_id,
            "item_name": item_name,
            "status": "pending"
        }).execute()
        _notify_admins(
            f"Restock request: {_display_name(g.profile, 'A technician')} needs '{item_name}' for Case {cases[0]['case_number']}.",
            link=url_for("admin_dashboard", open_modal="restock"),
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
def _due_status(due_date, today):
    """(state, label) for a case's due date: overdue, today, soon (within 2 days), later, or none."""
    try:
        due = datetime.strptime(str(due_date)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return "none", "No due date"
    days = (due - today).days
    if days < 0:
        return "overdue", f"Overdue {-days}d"
    if days == 0:
        return "today", "Due today"
    if days <= 2:
        return "soon", "Due tomorrow" if days == 1 else f"Due in {days}d"
    return "later", "Due " + due.strftime("%b %d").replace(" 0", " ")


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
            return _access_denied()

        profile = profiles[0]

        # Guard initials to prevent template IndexError if names are blank
        if not profile.get("first_name"):
            profile["first_name"] = (profile.get("display_name") or "Tech")[:1]
        if not profile.get("last_name"):
            profile["last_name"] = ""

        # ==========================================
        # 2. SESSION HEARTBEAT: update_user_activity() records last_seen once a minute.
        # ==========================================

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

        inventory_items = _picker_inventory()

        # Due-date urgency for the pipeline cards (lab time). Within a stage, rush cases come
        # first, then the soonest due date; cases without a due date go last.
        today = datetime.now(LAB_TZ).date()
        for case in assigned_cases:
            case["due_state"], case["due_label"] = _due_status(case.get("due_date"), today)
            case["urgency_rank"] = (
                0 if case.get("is_rush") else 1,
                str(case.get("due_date") or "9999-12-31")[:10],
                str(case.get("created_at") or ""),
            )

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
        notifications = notification_feed()["items"][:5]  # shared with the bell

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
            notifications=notifications,
            inventory_items=inventory_items,
        )

    except Exception as e:
        return f"Technician dashboard error: {str(e)}", 500

# =========================
# TECHNICIAN CASE DETAILS
# =========================

CASE_FILES_BUCKET = "dental-case-files"
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
# Bench stages in order; a technician moves a case forward through them.
BENCH_FLOW = (
    ("assigned", "Assigned"), ("in_progress", "In progress"), ("cad_designing", "CAD designing"),
    ("wax_up_or_try_in", "Wax-up / setup"), ("milling_or_printing", "Milling / printing"),
    ("packing_and_curing", "Packing & curing"), ("finishing_and_quality_check", "Finishing & QC"),
)
# Stages where the technician logs the material used.
MATERIAL_LOG_STAGES = ("milling_or_printing", "packing_and_curing", "finishing_and_quality_check")




USAGE_BASIS_LABELS = {"unit": "per unit", "arch": "per arch", "case": "per case"}


def _disc_token(shade):
    """Which zirconia disc a shade is milled from (mirrors ml_models/lab_profile.py): A1/B1 from
    A1, A2/B2/C1 from A2, bleach and OM shades from OM, darker shades from A3."""
    s = (shade or "").upper().replace(" ", "")
    if s in ("A1", "B1"):
        return "a1"
    if s in ("A2", "B2", "C1"):
        return "a2"
    if s.startswith(("BL", "OM")):
        return "om"
    return "a3"


def _rule_tokens(text):
    return [t.strip().lower() for t in (text or "").split(",") if t.strip()]


def _usage_plan(case, teeth, stage, items):
    """Suggested material rows for a case moving to `stage`, from each Inventory item's rule.

    A rule applies to a tooth when one of its words is the tooth's prescribed material
    (zirconia, emax...) or appears in the case type (e.g. "denture"); a rule with no words
    applies to every case. Disc items (tracks_discs) are split by shade, so each zirconia
    tooth is counted against the disc it is milled from. Amounts are in the item's own unit.
    """
    import re
    case_type = (case.get("case_type") or "").lower()

    def applies(tokens, tooth):
        material = (tooth.get("material") or "").lower()
        return not tokens or any(t == material or t in case_type for t in tokens)

    rules = [i for i in items if i.get("usage_stage") == stage and float(i.get("usage_amount") or 0) > 0]
    disc_items = [i for i in rules if i.get("tracks_discs")]
    disc_of_tooth = {}
    for idx, tooth in enumerate(teeth):
        candidates = [i for i in disc_items if applies(_rule_tokens(i.get("usage_applies_to")), tooth)]
        if candidates:
            token = _disc_token(tooth.get("shade"))
            words = lambda i: [w for w in re.split(r"[\s/()\-]+", i["item_name"].lower()) if w]
            pick = next((i for i in candidates if any(w.startswith(token) for w in words(i))), candidates[0])
            disc_of_tooth[idx] = pick["id"]

    rows = []
    for item in rules:
        tokens = _rule_tokens(item.get("usage_applies_to"))
        if item.get("tracks_discs"):
            matched = [t for idx, t in enumerate(teeth) if disc_of_tooth.get(idx) == item["id"]]
        else:
            matched = [t for t in teeth if applies(tokens, t)]
        if not matched and not (teeth == [] and (not tokens or any(t in case_type for t in tokens))):
            continue
        basis = item.get("usage_basis") or "unit"
        arches = len({"upper" if str(t.get("tooth_number"))[:1] in "12" else "lower" for t in matched
                      if str(t.get("tooth_number"))[:1] in "1234"}) or 1
        count = {"unit": max(len(matched), 1), "arch": arches, "case": 1}[basis]
        per = float(item["usage_amount"])
        unit = item.get("unit") or "unit"
        shades = sorted({t.get("shade") for t in matched if t.get("shade")})
        what = {"unit": f"{count} unit{'s' if count != 1 else ''}", "arch": f"{count} arch{'es' if count != 1 else ''}",
                "case": "this case"}[basis]
        rows.append({
            "inventory_id": item["id"],
            "item_name": item["item_name"],
            "unit": unit,
            "amount": round(per * count, 3),
            "units": len(matched),
            "tracks_discs": bool(item.get("tracks_discs")),
            "reason": f"{what}{' · ' + ', '.join(shades) if shades else ''} × {per:g} {unit} {USAGE_BASIS_LABELS[basis]}",
        })
    return rows


def _disc_units(disc_ids):
    """Units milled so far from each disc."""
    if not disc_ids:
        return {}
    rows = supabase_admin.table("zirconia_disc_usage").select("disc_id, units").in_("disc_id", list(disc_ids)).execute().data or []
    totals = {}
    for r in rows:
        totals[r["disc_id"]] = totals.get(r["disc_id"], 0) + int(r["units"])
    return totals


def _open_discs(item_ids):
    """Open discs of these Inventory items, oldest first, with the units milled from each."""
    if not item_ids:
        return {}
    discs = (supabase_admin.table("zirconia_discs").select("id, inventory_id, label, opened_at")
             .in_("inventory_id", list(item_ids)).eq("status", "open").order("opened_at").execute().data or [])
    units = _disc_units([d["id"] for d in discs])
    by_item = {}
    for d in discs:
        by_item.setdefault(d["inventory_id"], []).append({"id": d["id"], "label": d["label"], "units": units.get(d["id"], 0)})
    return by_item


def _parse_material_log(form):
    """Read and check the material rows sent with a stage change.

    Each row sends log_item, log_amount (non-disc items, in the item's unit), log_disc (an
    open disc's id), log_new_disc (the ID written on a newly opened disc), log_units (units
    milled from the disc) and log_used_up ("1" when the disc is finished). Returns
    (entries, error); entries hold the Inventory item and, for discs, the disc to use.
    """
    keys = ("log_item", "log_amount", "log_disc", "log_new_disc", "log_units", "log_used_up")
    columns = [form.getlist(k) for k in keys]
    n = len(columns[0])
    if any(len(c) != n for c in columns):
        return [], "The material rows were incomplete. Reload the page and try again."
    rows = [dict(zip(keys, values)) for values in zip(*columns)]
    rows = [r for r in rows if r["log_item"]]
    if not rows:
        return [], "Log at least one material used at this stage."
    ids = list({r["log_item"] for r in rows})
    items = {i["id"]: i for i in (supabase_admin.table("inventory").select("*").in_("id", ids).execute().data or [])}
    entries = []
    for r in rows:
        item = items.get(r["log_item"])
        if not item:
            return [], "One of the materials is no longer in Inventory. Reload the page and try again."
        if item.get("tracks_discs"):
            try:
                units = int(r["log_units"])
            except (TypeError, ValueError):
                units = 0
            if not 1 <= units <= 99:
                return [], f"Enter how many units were milled from the {item['item_name']} disc (1–99)."
            disc, label = None, " ".join((r["log_new_disc"] or "").split())[:40]
            if r["log_disc"]:
                found = (supabase_admin.table("zirconia_discs").select("*").eq("id", r["log_disc"])
                         .eq("inventory_id", item["id"]).execute().data or [])
                if not found or found[0]["status"] != "open":
                    return [], f"That {item['item_name']} disc is no longer open. Reload the page and choose another."
                disc = found[0]
            elif label:
                same = [d for d in (supabase_admin.table("zirconia_discs").select("*").eq("inventory_id", item["id"])
                                    .ilike("label", label).execute().data or []) if d["label"].lower() == label.lower()]
                if same and same[0]["status"] != "open":
                    return [], f"Disc {label} of {item['item_name']} was already used up. Check the ID on the box."
                disc = same[0] if same else None
            else:
                return [], f"Choose the open {item['item_name']} disc, or enter the ID written on the new disc's box."
            entries.append({"item": item, "disc": disc, "new_label": None if disc else label, "units": units,
                            "used_up": r["log_used_up"] == "1"})
        else:
            try:
                amount = float(r["log_amount"])
            except (TypeError, ValueError):
                amount = 0
            if not 0 < amount <= 1000:
                return [], f"Enter how much {item['item_name']} was used, in {item.get('unit') or 'units'}."
            entries.append({"item": item, "amount": round(amount, 3)})

    # Block the stage change when Inventory doesn't have enough of a material: amounts used,
    # and one sealed disc for each newly opened disc (milling from an open disc needs none).
    needed = {}
    for e in entries:
        if "amount" in e:
            needed[e["item"]["id"]] = needed.get(e["item"]["id"], 0) + e["amount"]
        elif e["disc"] is None:
            needed[e["item"]["id"]] = needed.get(e["item"]["id"], 0) + 1
    short = []
    for item_id, need in needed.items():
        item = items[item_id]
        have = float(item.get("quantity") or 0)
        if have + 1e-9 < need:
            unit = item.get("unit") or "unit"
            short.append(f"{item['item_name']} (needs {need:g} {unit}, Inventory has {have:g})")
    if short:
        return [], ("Not enough stock in Inventory: " + "; ".join(short) + ". The case can't move to this stage "
                    "until the admin restocks it or corrects the count. Send a request with \"Running low on a "
                    "material?\" on the job order page.")
    return entries, None


def _log_inventory(item, user_id, action, change, notes):
    """Change an item's stock by `change` (never below zero) and record it in inventory_logs."""
    current = float(item.get("quantity") or 0)
    new = round(max(current + change, 0.0), 3)
    if new != current:
        supabase_admin.table("inventory").update({"quantity": new}).eq("id", item["id"]).execute()
    item["quantity"] = new
    supabase_admin.table("inventory_logs").insert({
        "inventory_id": item["id"], "user_id": user_id, "action_type": action,
        "quantity_changed": round(new - current, 3), "previous_quantity": current, "new_quantity": new,
        "notes": notes + (" (stock was already 0; count it in Inventory)" if change < 0 and current <= 0 else ""),
    }).execute()


def _apply_material_log(entries, case_id, case_number, stage_label, user_id):
    """Record checked material rows: deduct amounts, open discs, record units milled per disc."""
    for e in entries:
        item = e["item"]
        unit = item.get("unit") or "unit"
        try:
            if "amount" in e:
                _log_inventory(item, user_id, "case_deduction", -e["amount"],
                               f"Used {e['amount']:g} {unit} at {stage_label} for Case {case_number}")
                continue
            disc = e["disc"]
            if disc is None:  # a newly opened disc: one whole disc leaves the shelf
                disc = supabase_admin.table("zirconia_discs").insert({
                    "inventory_id": item["id"], "label": e["new_label"], "opened_by": user_id,
                }).execute().data[0]
                _log_inventory(item, user_id, "disc_opened", -1,
                               f"Opened disc {disc['label']} at {stage_label} for Case {case_number}")
            supabase_admin.table("zirconia_disc_usage").insert({
                "disc_id": disc["id"], "case_id": case_id, "units": e["units"], "logged_by": user_id,
            }).execute()
            note = f"Milled {e['units']} unit{'s' if e['units'] != 1 else ''} from disc {disc['label']} at {stage_label} for Case {case_number}"
            if e["used_up"]:
                supabase_admin.table("zirconia_discs").update({
                    "status": "used_up", "used_up_at": _now_iso(), "used_up_by": user_id,
                }).eq("id", disc["id"]).execute()
                note += f"; disc used up after {_disc_units([disc['id']]).get(disc['id'], e['units'])} units"
            _log_inventory(item, user_id, "disc_use", 0, note)
        except Exception:
            app.logger.exception("Material log error for %s on case %s", item.get("item_name"), case_number)


def _picker_inventory():
    """Inventory items a technician can log material use against, grouped by category."""
    try:
        items = (supabase_admin.table("inventory").select("id, item_name, category, quantity, unit, tracks_discs")
                 .order("category").order("item_name").execute().data or [])
    except Exception:
        app.logger.exception("Unable to load inventory for the material picker")
        return []
    for item in items:  # the picker groups by category; a blank one would break the grouping's sort
        item["category"] = item.get("category") or "Uncategorized"
    return sorted(items, key=lambda i: (i["category"].lower(), (i.get("item_name") or "").lower()))


def _tooth_chart(teeth):
    """Positions for an FDI tooth chart: two arches of 16 teeth, the case's teeth marked.

    Upper arch reads 18..11 | 21..28 left to right (the patient's right on the viewer's left),
    lower arch 48..41 | 31..38, front teeth at the outer edge of each arch.
    """
    import math
    marked = {}
    for t in teeth or []:
        num = str(t.get("tooth_number") or "").strip()
        marked.setdefault(num, []).append(t)
    upper = [f"1{i}" for i in range(8, 0, -1)] + [f"2{i}" for i in range(1, 9)]
    lower = [f"4{i}" for i in range(8, 0, -1)] + [f"3{i}" for i in range(1, 9)]
    chart = []
    for row, cy, sign in ((upper, 128, -1), (lower, 172, 1)):
        for i, num in enumerate(row):
            angle = math.pi - (i + 0.5) * math.pi / 16
            info = marked.get(num, [])
            chart.append({
                "num": num,
                "x": round(180 + 152 * math.cos(angle), 1),
                "y": round(cy + sign * 104 * math.sin(angle), 1),
                "marked": bool(info),
                "label": ", ".join(
                    f"{(t.get('material') or 'material not set').replace('_', ' ')} {t.get('shade') or ''}".strip() for t in info
                ),
            })
    outside = sorted(n for n in marked if n not in upper + lower)
    return chart, outside


def _materials_used(case_number):
    """Material use logged against a job order (the log notes name its case number)."""
    try:
        logs = (supabase_admin.table("inventory_logs").select("*")
                .ilike("notes", f"%Case {case_number}%").order("created_at").execute().data or [])
    except Exception:
        app.logger.exception("Unable to load material use for %s", case_number)
        return []
    import re
    exact = re.compile(rf"Case {re.escape(case_number)}(?![\w-])")   # DL-0001 must not match DL-00018
    logs = [l for l in logs if exact.search(l.get("notes") or "")
            and (l.get("action_type") in ("case_deduction", "auto_created_and_deducted", "disc_use", "disc_opened")
                 or float(l.get("quantity_changed") or 0) < 0)]
    ids = list({l["inventory_id"] for l in logs if l.get("inventory_id")})
    items = {}
    if ids:
        rows = supabase_admin.table("inventory").select("id, item_name, unit").in_("id", ids).execute().data or []
        items = {r["id"]: r for r in rows}
    for l in logs:
        item = items.get(l.get("inventory_id"), {})
        l["item_name"] = item.get("item_name") or "A deleted material"
        unit = item.get("unit") or "unit"
        notes = l.get("notes") or ""
        # The note records what was used even when stock was already 0 (no quantity change).
        milled = re.search(r"Milled (\d+) units? from disc (.+?) at ", notes)
        opened = re.search(r"Opened disc (.+?) at ", notes)
        amount = re.search(r"Used ([\d.]+) ", notes)
        portion = re.search(r"(?:Used|Deducted) ([\d.]+)%", notes)   # entries from before usage rules
        if milled:
            l["portion"] = f"{milled.group(1)} unit{'s' if milled.group(1) != '1' else ''} from disc {milled.group(2)}"
            if "used up" in notes:
                l["portion"] += " (disc used up)"
        elif opened:
            l["portion"] = f"Opened new disc {opened.group(1)} (1 {unit} from stock)"
        elif portion:
            l["portion"] = f"{float(portion.group(1)):g}% of a {unit}"
        elif amount:
            l["portion"] = f"{float(amount.group(1)):g} {unit}"
        else:
            l["portion"] = f"{abs(float(l.get('quantity_changed') or 0)):g} {unit}"
        stage = re.search(r" at (.+?) (?:stage )?for Case", l.get("notes") or "")
        l["stage"] = stage.group(1).strip("[]").replace("_", " ") if stage else ""
    return logs


def _signed_case_file_urls(paths, expires_in=3600):
    """Signed URLs for case files, {path: url}: one storage request for all of them.

    Falls back to one request per file if the batch call fails (e.g. a file is missing,
    which the batch call can't report per file)."""
    paths = [p for p in dict.fromkeys(paths) if p]
    if not paths:
        return {}
    bucket = supabase_admin.storage.from_(CASE_FILES_BUCKET)
    try:
        return {item.get("path"): item.get("signedURL") for item in bucket.create_signed_urls(paths, expires_in)
                if item.get("path") and item.get("signedURL")}
    except Exception:
        urls = {}
        for path in paths:
            try:
                signed = bucket.create_signed_url(path, expires_in)
                urls[path] = signed.get("signedURL") or signed.get("signedUrl") if isinstance(signed, dict) else signed
            except Exception:
                app.logger.exception("Unable to sign case file %s", path)
        return urls


def _case_file_links(files):
    """Add view_url (opens in the browser) and download_url (saves the file) to case file rows,
    plus kind: "stl", "image", "pdf" or "other" for choosing a viewer."""
    urls = _signed_case_file_urls([f.get("file_path") for f in files])
    for f in files:
        name = (f.get("file_name") or "").lower()
        mime = (f.get("mime_type") or "").lower()
        if f.get("file_type") == "stl" or name.endswith(".stl"):
            f["kind"] = "stl"
        elif mime.startswith("image/") or name.endswith(IMAGE_EXTENSIONS):
            f["kind"] = "image"
        elif mime == "application/pdf" or name.endswith(".pdf"):
            f["kind"] = "pdf"
        else:
            f["kind"] = "other"
        url = urls.get(f.get("file_path"))
        f["view_url"] = url
        # Supabase sends the file as an attachment when the URL carries ?download=<name>.
        f["download_url"] = f"{url}{'&' if '?' in url else '?'}download={quote(f.get('file_name') or 'file')}" if url else None
        size = f.get("file_size") or 0
        f["size_label"] = f"{size / 1048576:.1f} MB" if size >= 1048576 else (f"{max(size // 1024, 1)} KB" if size else "")
    return files


@app.route("/technician/cases/<case_id>/material-plan")
@role_required("technician")
def technician_material_plan(case_id):
    """Material rows to pre-fill when this case moves to `stage`: the usage rules applied to its
    teeth, plus the open discs of every disc-tracked item (for picking which disc was milled)."""
    stage = request.args.get("stage")
    if stage not in MATERIAL_LOG_STAGES:
        return jsonify(error="This stage doesn't log materials."), 400
    cases = (supabase_admin.table("dental_cases").select("id, case_type").eq("id", case_id)
             .eq("technician_id", g.profile["id"]).execute().data or [])
    if not cases:
        return jsonify(error="This job order isn't assigned to you."), 404
    teeth = supabase_admin.table("case_teeth").select("tooth_number, material, shade").eq("case_id", case_id).execute().data or []
    items = (supabase_admin.table("inventory")
             .select("id, item_name, unit, usage_stage, usage_applies_to, usage_basis, usage_amount, tracks_discs")
             .execute().data or [])
    discs = _open_discs([i["id"] for i in items if i.get("tracks_discs")])
    return jsonify(rows=_usage_plan(cases[0], teeth, stage, items), discs=discs,
                   stage_label=dict(BENCH_FLOW).get(stage, stage))


@app.route("/technician/cases/<case_id>")
@role_required("technician")
def view_case_details(case_id):
    """A technician's view of one of their job orders: prescription, teeth, files, history."""
    user_id = g.profile["id"]
    try:
        cases = (supabase_admin.table("dental_cases").select("*")
                 .eq("id", case_id).eq("technician_id", user_id).execute().data or [])
        if not cases:
            return "This job order isn't assigned to you, or it no longer exists.", 404
        case = cases[0]

        teeth = (supabase_admin.table("case_teeth").select("*").eq("case_id", case_id)
                 .order("tooth_number").execute().data or [])
        files = _case_file_links(
            supabase_admin.table("case_files").select("*").eq("case_id", case_id).execute().data or []
        )
        dentist = {}
        if case.get("dentist_id"):
            rows = (supabase_admin.table("profiles").select("display_name, first_name, last_name, clinic_name, phone")
                    .eq("id", case["dentist_id"]).execute().data or [])
            dentist = rows[0] if rows else {}
        case_history = (supabase_admin.table("case_status_history").select("*, profiles(display_name)")
                        .eq("case_id", case_id).order("created_at").execute().data or [])

        parent = None
        if case.get("remake_of"):
            parents = (supabase_admin.table("dental_cases").select("id, case_number")
                       .eq("id", case["remake_of"]).execute().data or [])
            parent = parents[0] if parents else None

        today = datetime.now(LAB_TZ).date()
        case["due_state"], case["due_label"] = _due_status(case.get("due_date"), today)
        tooth_chart, teeth_off_chart = _tooth_chart(teeth)
        current = next((i for i, (value, _) in enumerate(BENCH_FLOW) if value == case.get("status")), None)
        next_stages = list(BENCH_FLOW[current + 1:]) if current is not None else []
        materials = {}
        for t in teeth:
            key = ((t.get("material") or "Not specified").replace("_", " ").title(), t.get("shade") or "")
            materials[key] = materials.get(key, 0) + 1

        return render_template(
            "technician_case_details.html",
            profile=g.profile,
            case=case,
            teeth=teeth,
            files=files,
            dentist=dentist,
            parent=parent,
            case_history=case_history,
            material_summary=[{"material": m, "shade": s, "units": n} for (m, s), n in materials.items()],
            bench_statuses=BENCH_STATUSES,
            next_stages=next_stages,
            material_log_stages=MATERIAL_LOG_STAGES,
            inventory_items=_picker_inventory() if next_stages else [],
            tooth_chart=tooth_chart,
            teeth_off_chart=teeth_off_chart,
            materials_used=_materials_used(case["case_number"]),
        )
    except Exception:
        app.logger.exception("Unable to load case details")
        return "Unable to load this job order. Check the server logs for details.", 500

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
            return _access_denied()

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
    """Number of delivery issue reports awaiting review (0 if the table isn't migrated yet).

    Counted once per request: the dashboard and the sidebar both show it."""
    if g.get("_open_issue_count") is not None:
        return g._open_issue_count
    try:
        count = supabase_admin.table("case_issue_reports").select("id", count="exact").eq("status", "open").limit(1).execute().count or 0
    except Exception:
        return 0
    g._open_issue_count = count
    return count


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
def _job_order_context(case, include_internal=True):
    """Everything a job order page shows besides the case row itself (dentist and admin pages).

    Sets case["teeth"], due-date urgency, and returns files, history (with names), dispatch
    events, delivery receipt, issue reports, remake links, technician name, tooth chart and
    materials summary. include_internal=False hides lab-only scheduling (rush flags).
    """
    case_id = case["id"]
    case["teeth"] = supabase_admin.table("case_teeth").select("*").eq("case_id", case_id).order("tooth_number").execute().data or []
    files = _case_file_links(supabase_admin.table("case_files").select("*").eq("case_id", case_id).execute().data or [])
    history = (
        supabase_admin.table("case_status_history").select("status, notes, created_at, changed_by")
        .eq("case_id", case_id).order("created_at").execute().data or []
    )
    if not include_internal:
        history = [h for h in history if not str(h.get("status") or "").startswith(("Marked as rush", "Rush flag removed"))]
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

    case["due_state"], case["due_label"] = _due_status(case.get("due_date"), datetime.now(LAB_TZ).date())
    tooth_chart, teeth_off_chart = _tooth_chart(case["teeth"])
    materials = {}
    for t in case["teeth"]:
        key = ((t.get("material") or "Not specified").replace("_", " ").title(), t.get("shade") or "")
        materials[key] = materials.get(key, 0) + 1
    return {
        "files": files,
        "history": history,
        "events": events,
        "receipt": _delivery_receipt(case, events),
        "reports": _issue_reports_for([case_id]),
        "parent": parent,
        "remakes": remakes,
        "technician_name": names.get(case.get("technician_id")),
        "tooth_chart": tooth_chart,
        "teeth_off_chart": teeth_off_chart,
        "material_summary": [{"material": m, "shade": sh, "units": n} for (m, sh), n in materials.items()],
    }


def _can_view_case(profile, case_id):
    """Admins see every case. Dentists and technicians see their own cases, plus any earlier
    job order that one of their remakes came from (walking down the remake chain)."""
    if profile.get("role") == "admin":
        return True
    uid = profile["id"]
    level, seen = [case_id], set()
    for _ in range(10):  # remake chains are short; the cap guards against bad data
        rows = (
            supabase_admin.table("dental_cases").select("id, dentist_id, technician_id")
            .in_("id", level).execute().data or []
        )
        if any(uid in (r.get("dentist_id"), r.get("technician_id")) for r in rows):
            return True
        seen.update(level)
        children = (
            supabase_admin.table("dental_cases").select("id")
            .in_("remake_of", level).execute().data or []
        )
        level = [c["id"] for c in children if c["id"] not in seen]
        if not level:
            return False
    return False


@app.route("/job-orders/<case_id>/original")
@role_required("admin", "technician", "dentist")
def previous_job_order(case_id):
    """Read-only summary of an earlier job order, shown in a pop-up from the remake that
    replaced it: what was prescribed, what went wrong, and how the lab decided."""
    try:
        cases = supabase_admin.table("dental_cases").select("*").eq("id", case_id).execute().data or []
        if not cases or not _can_view_case(g.profile, case_id):
            return "This job order isn't available to you.", 404
        case = cases[0]
        ctx = _job_order_context(case, include_internal=g.profile.get("role") != "dentist")
        remake_numbers = {rm["id"]: rm["case_number"] for rm in ctx["remakes"]}
        for r in ctx["reports"]:
            r["remake_number"] = remake_numbers.get(r.get("remake_case_id"))
        return render_template(
            "partials/previous_job_order_body.html",
            case=case,
            issue_types=ISSUE_TYPES,
            **ctx,
        )
    except Exception:
        app.logger.exception("Unable to load previous job order")
        return "Unable to load this job order. Try again later.", 500


@app.route("/admin/cases/<case_id>")
@role_required("admin")
def admin_case_details(case_id):
    """The admin's job order page: everything about one case, from any stage."""
    try:
        cases = supabase_admin.table("dental_cases").select("*").eq("id", case_id).execute().data or []
        if not cases:
            return "Job order not found.", 404
        case = cases[0]
        ctx = _job_order_context(case, include_internal=True)
        dentist = {}
        if case.get("dentist_id"):
            rows = (supabase_admin.table("profiles").select("display_name, first_name, last_name, clinic_name, phone")
                    .eq("id", case["dentist_id"]).execute().data or [])
            dentist = rows[0] if rows else {}
        label = _active_dispatch_label(case_id) if case.get("status") in DISPATCH_STATUSES + DELIVERED_STATUSES else None
        return render_template(
            "admin_case_details.html",
            profile=g.profile,
            case=case,
            dentist=dentist,
            label=label,
            materials_used=_materials_used(case["case_number"]),
            issue_types=ISSUE_TYPES,
            issue_decisions=ISSUE_DECISIONS,
            **ctx,
        )
    except Exception:
        app.logger.exception("Unable to load admin job order page")
        return "Unable to load this job order. Check the server logs for details.", 500


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

        ctx = _job_order_context(case, include_internal=False)
        receipt, reports = ctx["receipt"], ctx["reports"]
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
            can_report=can_report,
            has_open_report=has_open_report,
            issue_types=ISSUE_TYPES,
            issue_decisions=ISSUE_DECISIONS,
            window_days=ISSUE_REPORT_WINDOW_DAYS,
            **ctx,
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
# LIVE UPDATES: lightweight change check polled by every dashboard
# =========================
def _latest(table, column, **filters):
    """Newest value of `column` in `table` matching the equality filters, or ''."""
    query = supabase_admin.table(table).select(column)
    for key, value in filters.items():
        query = query.eq(key, value)
    # Skip NULLs: Postgres sorts them first on a descending order, which would hide newer rows.
    rows = query.not_.is_(column, "null").order(column, desc=True).limit(1).execute().data or []
    return str(rows[0].get(column) or "") if rows else ""


def _count(table, **filters):
    query = supabase_admin.table(table).select("id", count="exact")
    for key, value in filters.items():
        query = query.eq(key, value)
    return query.limit(1).execute().count or 0


def _forecast_stamp():
    """Latest forecast run and its status; '' before the forecast_runs migration has run."""
    try:
        rows = (supabase_admin.table("forecast_runs").select("id, status")
                .order("created_at", desc=True).limit(1).execute().data or [])
        return f"{rows[0]['id']}:{rows[0]['status']}" if rows else ""
    except Exception:
        return ""


@app.route("/api/live")
@role_required("admin", "technician", "dentist")
def live_status():
    """Return a short fingerprint of everything this user's pages show.

    Pages poll this every few seconds and refresh their sections only when the
    fingerprint changes. Case rows have an auto-updated `updated_at`; inventory and
    requests are tracked through their logs and counts.

    The database function live_stamp() (migration 20261112000000) builds the fingerprint in
    one call; without it, the separate queries below are used.
    """
    try:
        user_id, role = g.profile["id"], g.profile["role"]
        try:
            result = supabase_admin.rpc("live_stamp", {"p_user": user_id, "p_role": role}).execute().data
        except Exception:
            result = None
        if isinstance(result, dict) and "stamp" in result:
            response = app.response_class(
                json.dumps({"stamp": result["stamp"], "latest": result.get("latest")}),
                mimetype="application/json",
            )
            response.headers["Cache-Control"] = "no-store"
            return response

        scope = {"technician": {"technician_id": user_id}, "dentist": {"dentist_id": user_id}}.get(role, {})
        parts = [
            _latest("notifications", "created_at", user_id=user_id),
            _latest("dental_cases", "updated_at", **scope),
            str(_count("dental_cases", **scope)),
        ]
        if role == "admin":
            parts += [
                _latest("inventory_logs", "created_at"),
                str(_count("inventory")),
                str(_count("material_requests", status="pending")),
                _latest("profiles", "created_at"),
                str(_count("profiles", status="pending")),
                str(_open_issue_count()),
                _forecast_stamp(),
            ]
        latest = (
            supabase_admin.table("notifications").select("id, message, created_at")
            .eq("user_id", user_id).order("created_at", desc=True).limit(1).execute().data or []
        )
        response = app.response_class(
            json.dumps({"stamp": "|".join(parts), "latest": latest[0] if latest else None}),
            mimetype="application/json",
        )
        response.headers["Cache-Control"] = "no-store"
        return response
    except Exception:
        app.logger.exception("Live status check failed")
        return app.response_class(json.dumps({"error": "unavailable"}), status=503, mimetype="application/json")


# =========================
# NOTIFICATION HELPER
# =========================
def create_notification(user_id, message, link="#"):
    if not user_id:
        return
    try:
        supabase_admin.table("notifications").insert({
            "user_id": user_id,
            "message": message,
            "link": link,
            "is_read": False,
        }).execute()
    except Exception:
        app.logger.exception("Failed to create notification")


NOTIFICATION_FEED_SIZE = 10


def notification_feed():
    """The signed-in user's bell: unread count and the newest notifications (for partials/notifications_bell.html).

    Loaded once per request; dashboards reuse the items for their alert chime."""
    user_id = session.get("user_id")
    feed = {"unread": 0, "items": []}
    if not user_id:
        return feed
    cached = g.get("_notification_feed")
    if cached and cached[0] == user_id:
        return cached[1]
    g._notification_feed = (user_id, feed)
    try:
        feed["unread"] = (supabase_admin.table("notifications").select("id", count="exact")
                          .eq("user_id", user_id).eq("is_read", False).limit(1).execute().count or 0)
        feed["items"] = (supabase_admin.table("notifications").select("id, message, link, is_read, created_at")
                         .eq("user_id", user_id).order("created_at", desc=True)
                         .limit(NOTIFICATION_FEED_SIZE).execute().data or [])
    except Exception:
        app.logger.exception("Unable to load notifications")
    return feed


def _unread_count(user_id):
    return (supabase_admin.table("notifications").select("id", count="exact")
            .eq("user_id", user_id).eq("is_read", False).limit(1).execute().count or 0)


@app.route("/notifications/<notification_id>/read", methods=["POST"])
@role_required("admin", "technician", "dentist")
def mark_notification_read(notification_id):
    user_id = g.profile["id"]
    supabase_admin.table("notifications").update({"is_read": True}).eq("id", notification_id).eq("user_id", user_id).execute()
    return {"ok": True, "unread": _unread_count(user_id)}


@app.route("/notifications/read-all", methods=["POST"])
@role_required("admin", "technician", "dentist")
def mark_all_notifications_read():
    user_id = g.profile["id"]
    supabase_admin.table("notifications").update({"is_read": True}).eq("user_id", user_id).eq("is_read", False).execute()
    return {"ok": True, "unread": 0}


@app.route("/notifications/<notification_id>/open")
@role_required("admin", "technician", "dentist")
def open_notification(notification_id):
    """Mark a notification read and go where it points (only to pages inside DentaLink)."""
    rows = (supabase_admin.table("notifications").update({"is_read": True})
            .eq("id", notification_id).eq("user_id", g.profile["id"]).execute().data or [])
    link = rows[0].get("link") if rows else None
    if link and link != "#" and _safe_next_path(link):
        return redirect(link)
    return redirect(url_for({"admin": "admin_dashboard", "technician": "technician_dashboard"}.get(g.profile["role"], "dentist_dashboard")))

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
            return _access_denied()

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
        # Submitted from the job order page: stay there.
        if request.referrer and f"/technician/cases/{case_id}" in request.referrer:
            return redirect(url_for("view_case_details", case_id=case_id))
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
                message=f"QC passed: Case {case['case_number']} passed the final check. The admin will pack and dispatch it.",
                link=f"/technician/cases/{case_id}",
            )
        if case.get("dentist_id"):
            create_notification(
                user_id=case["dentist_id"],
                message=f"Passed the lab's quality check: Case {case['case_number']} is being packed for dispatch.",
                link=f"/dentist/cases/{case_id}",
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


@app.route("/admin/cases/<case_id>/rush", methods=["POST"])
@role_required("admin")
def admin_set_rush(case_id):
    """Mark or unmark a case as a rush job. The assigned technician is told when it becomes rush."""
    rush = request.form.get("rush") == "1"
    try:
        updated = (
            supabase_admin.table("dental_cases")
            .update({"is_rush": rush})
            .eq("id", case_id)
            .in_("status", list(RUSHABLE_STATUSES))
            .execute()
            .data
            or []
        )
        if not updated:
            flash("This case has already passed QC or was closed, so its rush flag can't change.", "rush-stale")
            return redirect(url_for("admin_dashboard"))

        case = updated[0]
        log_case_status_change(case_id, "Marked as rush" if rush else "Rush flag removed", g.profile["id"])
        if rush and case.get("technician_id"):
            create_notification(
                user_id=case["technician_id"],
                message=f"Rush: Case {case['case_number']} ({case.get('patient_name') or 'patient'}) is now a rush job. Please work on it first.",
                link=f"/technician/cases/{case_id}",
            )
        if rush:
            flash(f"Case {case['case_number']} is now a rush job and moves to the top of the technician's pipeline.", "rush-on")
        else:
            flash(f"Case {case['case_number']} is no longer marked as rush.", "rush-off")
        return redirect(url_for("admin_dashboard"))
    except Exception:
        app.logger.exception("Unable to update rush flag")
        return "Unable to update the rush flag. Run the latest migration, then check the server logs.", 500


@app.route("/cases/<case_id>/dispatch-slip")
@role_required("admin")
def dispatch_slip(case_id):
    """The printable QR label. The admin packs the case and prints it; the clinic scans it."""
    try:
        case = _case_for_dispatch(case_id)
        if not case:
            return "Case not found.", 404
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
            scan_url_is_local=_is_local_url(scan_url),
            patient_initials=_patient_initials(case.get("patient_name")),
            back_url=url_for("admin_dashboard"),
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


# The box's QR label opens this page for anyone, signed in or not, so the public view must never
# show more than a courier or receptionist needs: no patient name, phone numbers, instructions,
# files or prices.
PUBLIC_LABEL_DETAIL_DAYS = 30   # after delivery, the public page only says "Delivered on <date>"


def _label_viewer():
    """The signed-in, approved user scanning a label, or None for the public."""
    if not session.get("user_id"):
        return None
    try:
        profile = _current_profile()
    except Exception:
        return None
    if profile and profile.get("status") == "approved" and profile.get("role") in ("admin", "technician", "dentist"):
        return profile
    return None


def _no_store(body, status=200):
    """Keep label pages out of caches and search engines, and don't leak the link in Referer."""
    response = app.make_response((body, status))
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.route("/d/<token>")
def scan_dispatch_label(token):
    """The page behind a printed dispatch label.

    Anyone can open it without signing in and sees the clinic, dentist, due date, case type
    and shade. Signed-in lab staff and the case's own dentist see the full details, and an
    admin can confirm the package was checked and hand it to the courier.
    """
    try:
        viewer = _label_viewer()
        role = viewer.get("role") if viewer else None
        label = _load_dispatch_label(token)
        case = _case_for_dispatch(label["case_id"]) if label else None
        if not label or not case:
            return _no_store(render_template("dispatch_scan.html", problem="unknown", profile=viewer), 404)

        if label["status"] != "active":
            _log_dispatch_event(case["id"], "denied", label["id"], notes="voided_label")
            return _no_store(render_template("dispatch_scan.html", problem="voided", case=case, profile=viewer), 410)

        if role == "dentist" and case.get("dentist_id") != viewer["id"]:
            _log_dispatch_event(case["id"], "denied", label["id"], notes="wrong_clinic")
            _notify_admins(
                f"Delivery check: {_display_name(viewer, 'A clinic')} scanned the label for Case "
                f"{case['case_number']}, which is addressed to another clinic. Check for a mixed-up package."
            )
            return _no_store(render_template("dispatch_scan.html", problem="wrong_clinic", case=case, profile=viewer), 403)

        full = role is not None   # lab staff, or this case's own dentist (other dentists stopped above)
        delivered_at = _parse_timestamp(case.get("delivered_at"))
        expired = (not full and delivered_at is not None
                   and datetime.now(timezone.utc) - delivered_at > timedelta(days=PUBLIC_LABEL_DETAIL_DAYS))
        shades = list(dict.fromkeys(t.get("shade") for t in case.get("teeth") or [] if t.get("shade")))

        events = []
        if full:
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

        return _no_store(render_template(
            "dispatch_scan.html",
            case=case,
            label=label,
            events=events,
            profile=viewer,
            public=not full,
            expired=expired,
            shades=shades,
            patient_initials=_patient_initials(case.get("patient_name")),
        ))
    except Exception:
        app.logger.exception("Unable to load dispatch scan page")
        return "Unable to load this delivery label. Try again in a moment.", 500


def _handover_form_problem():
    """Why the courier handover form can't be accepted, or None if it can."""
    if not request.form.get("courier_name", "").strip():
        return "Enter the courier's name before handing over the case."
    if request.form.get("package_checked") != "yes":
        return "Tick the box to confirm you checked the package against the job order."
    return None


def _hand_to_courier(case_id, courier_name, via_web=False):
    """Move a checked, packed case from Ready for dispatch to In transit; the current admin is the
    actor. Returns the case, or None if it isn't ready for dispatch (e.g. already handed over)."""
    case = _set_case_status(case_id, ["ready_for_dispatch"], {"status": "in_transit", "dispatched_at": _now_iso()})
    if not case:
        return None
    staff_name = _display_name(g.profile)
    label = _active_dispatch_label(case_id)
    _log_dispatch_event(case_id, "dispatched", label["id"] if label else None, courier_name=courier_name,
                        notes="Recorded on the web (label not scanned)" if via_web else None)
    log_case_status_change(case_id, "Dispatched (in transit)", g.profile["id"],
                           f"Handed to courier {courier_name} by {staff_name}" + (" (recorded on the web)" if via_web else ""))
    create_notification(
        user_id=case["dentist_id"],
        message=f"On its way: Case {case['case_number']} left the lab with {courier_name}. When the box arrives, open the case in DentaLink and tap Confirm receipt.",
        link=f"/dentist/cases/{case_id}",
    )
    _notify_admins(f"Dispatched: Case {case['case_number']} was checked and handed to {courier_name} by {staff_name}.")
    return case


@app.route("/d/<token>/dispatch", methods=["POST"])
@role_required("admin")
def dispatch_case(token):
    """Admin scanned the box, checked it matches the label, and hands it to the courier."""
    try:
        label = _load_dispatch_label(token)
        if not label or label["status"] != "active":
            return redirect(url_for("scan_dispatch_label", token=token))
        problem = _handover_form_problem()
        if problem:
            flash(problem, "error")
            return redirect(url_for("scan_dispatch_label", token=token))
        courier_name = request.form.get("courier_name", "").strip()[:120]
        if _hand_to_courier(label["case_id"], courier_name):
            flash(f"Handed to {courier_name}. The clinic has been notified.", "success")
        else:
            flash("This case has already been handed to a courier.", "error")
        return redirect(url_for("scan_dispatch_label", token=token))
    except Exception:
        app.logger.exception("Unable to dispatch case")
        return "Unable to record the handoff. Check the server logs for details.", 500


@app.route("/admin/cases/<case_id>/hand-over", methods=["POST"])
@role_required("admin")
def admin_hand_over(case_id):
    """Hand a case to the courier from the dashboard, without scanning the label on a phone.
    Same package check as the QR page; the chain of custody notes it was recorded on the web."""
    next_path = request.form.get("next", "")
    back = next_path if _safe_next_path(next_path) else url_for("admin_dashboard", stage="stage-dispatch")
    try:
        problem = _handover_form_problem()
        if problem:
            flash(problem, "dispatch-stale")
            return redirect(back)
        courier_name = request.form.get("courier_name", "").strip()[:120]
        case = _hand_to_courier(case_id, courier_name, via_web=True)
        if case:
            flash(f"Case {case['case_number']} is with {courier_name} and now In transit. The clinic has been notified.", "dispatch-done")
        else:
            flash("That case was already handed to a courier, or it isn't ready for dispatch.", "dispatch-stale")
        return redirect(back)
    except Exception:
        app.logger.exception("Unable to hand case to courier")
        return "Unable to record the handoff. Check the server logs for details.", 500


def _mark_case_delivered(case_id, received_by, by_lab=False):
    """Record delivery of a case that's ready for dispatch or in transit; the current user is the
    actor. Returns the case, or None if it was already delivered or isn't out for delivery.
    Without `received_by`, it says how the lab learned of it: via the courier, or collected at the lab."""
    before = supabase_admin.table("dental_cases").select("status").eq("id", case_id).execute().data or []
    was_scanned_out = bool(before) and before[0].get("status") == "in_transit"
    received_by = received_by or ("Clinic (confirmed by courier)" if was_scanned_out else "Collected from the lab")
    case = _set_case_status(case_id, DISPATCH_STATUSES, {"status": "delivered", "delivered_at": _now_iso()})
    if not case:
        return None

    label = _active_dispatch_label(case_id)
    notes = []
    if by_lab:
        notes.append(f"Marked delivered by the lab ({_display_name(g.profile, 'Admin')})")
    if not was_scanned_out:
        notes.append("Never handed to a courier" if by_lab else "Received without a dispatch scan at the lab")
    _log_dispatch_event(case_id, "delivered", label["id"] if label else None,
                        received_by=received_by, notes="; ".join(notes) or None)
    log_case_status_change(case_id, "Delivered", g.profile["id"],
                           f"Received by {received_by}" + (" (recorded by the lab)" if by_lab else ""))

    dentist = {}
    if case.get("dentist_id"):
        rows = (supabase_admin.table("profiles").select("display_name, first_name, clinic_name")
                .eq("id", case["dentist_id"]).execute().data or [])
        dentist = rows[0] if rows else {}
    clinic = dentist.get("clinic_name") or _display_name(dentist, "the clinic")
    if not by_lab:
        message = f"Delivered: Case {case['case_number']} was received by {received_by} at {clinic}."
        if not was_scanned_out:
            message += " It was never scanned out at the lab."
        _notify_admins(message)
    elif case.get("dentist_id"):
        create_notification(
            user_id=case["dentist_id"],
            message=f"Delivered: the lab recorded Case {case['case_number']} as received by {received_by}. If that's not right, contact the lab.",
            link=f"/dentist/cases/{case_id}",
        )
    if case.get("technician_id"):
        create_notification(user_id=case["technician_id"],
                            message=f"Delivered: Case {case['case_number']} reached {clinic}.", link="/technician")
    return case


@app.route("/dentist/cases/<case_id>/confirm-receipt", methods=["POST"])
@role_required("dentist")
def dentist_confirm_receipt(case_id):
    """The clinic confirms the box arrived, from the case page (the QR label is information only)."""
    try:
        rows = supabase_admin.table("dental_cases").select("id, dentist_id").eq("id", case_id).execute().data or []
        if not rows or rows[0].get("dentist_id") != g.profile["id"]:
            return "Case not found.", 404
        received_by = request.form.get("received_by", "").strip()[:120] or _display_name(g.profile, "Clinic")
        if _mark_case_delivered(case_id, received_by):
            flash("Thank you. The lab has been notified that the case arrived.", "receipt-done")
        else:
            flash("This case was already confirmed as delivered.", "receipt-stale")
        return redirect(url_for("dentist_case_summary", case_id=case_id))
    except Exception:
        app.logger.exception("Unable to confirm receipt")
        return "Unable to confirm receipt. Check the server logs for details.", 500


@app.route("/admin/cases/<case_id>/mark-delivered", methods=["POST"])
@role_required("admin")
def admin_mark_delivered(case_id):
    """The lab records delivery, e.g. when the courier confirms drop-off and the clinic hasn't."""
    next_path = request.form.get("next", "")
    back = next_path if _safe_next_path(next_path) else url_for("admin_dashboard", stage="stage-dispatch")
    from_label_page = back.startswith("/d/")   # the QR page shows plain messages, not the pop-up
    try:
        received_by = request.form.get("received_by", "").strip()[:120]
        case = _mark_case_delivered(case_id, received_by, by_lab=True)
        if case:
            flash(f"Case {case['case_number']} is marked as delivered. The clinic has been notified.",
                  "success" if from_label_page else "delivered-done")
        else:
            flash("That case was already delivered, or it isn't out for delivery.",
                  "error" if from_label_page else "delivered-stale")
        return redirect(back)
    except Exception:
        app.logger.exception("Unable to mark case delivered")
        return "Unable to mark the case delivered. Check the server logs for details.", 500

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
            return _access_denied()

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

        create_notification(
            user_id=technician_id,
            message=f"New Case Assigned: You have been assigned Case {case_number}.",
            link=f"/technician/cases/{case_id}"
        )
        # The dentist learns the lab has started on it.
        if updated[0].get("dentist_id"):
            create_notification(
                user_id=updated[0]["dentist_id"],
                message=f"In the lab: Case {case_number} was assigned to a technician.",
                link=f"/dentist/cases/{case_id}",
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
        profile = _current_profile()
        if not profile or profile.get("role") != "admin" or profile.get("status") != "approved":
            return _access_denied()

        # 2. Session heartbeat: update_user_activity() records last_seen once a minute.

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

        # 4. Notifications for the alert chime (shared with the bell)
        notifications = notification_feed()["items"][:5]

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

        # 6–8. One case query covers the unassigned cases (sidebar badge & modal), each
        # technician's active workload, and the milling / finishing counts for the chime.
        active_statuses = [
            "assigned", "in_progress", "cad_designing", "wax_up_or_try_in",
            "milling_or_printing", "packing_and_curing", "finishing_and_quality_check", "needs_revision"
        ]
        open_cases = (
            supabase_admin
            .table("dental_cases")
            .select("*")
            .in_("status", ["submitted"] + active_statuses)
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
        unassigned_cases = [c for c in open_cases if c.get("status") == "submitted"]

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

        workload_counts = {}
        for c in open_cases:
            if c.get("technician_id") in tech_ids and c.get("status") in active_statuses:
                workload_counts[c["technician_id"]] = workload_counts.get(c["technician_id"], 0) + 1
        for tech in technicians:
            tech["active_workload"] = workload_counts.get(tech["id"], 0)

        milling_cases = [{"id": c["id"]} for c in open_cases if c.get("status") == "milling_or_printing"]
        qc_cases = [{"id": c["id"]} for c in open_cases if c.get("status") == "finishing_and_quality_check"]
        _prime_sidebar_counts(incoming=len(unassigned_cases), restock=len(restock_requests))

        return render_template(
            "admin_inventory.html",
            profile=profile,
            inventory=inventory_items,
            pred_map=pred_map,  # Passed to HTML for forecasting UI display
            log_counts={item["id"]: sum(1 for log in historical_logs if log.get("inventory_id") == item["id"]) for item in inventory_items},
            last_change={
                item["id"]: max((log.get("created_at") or "" for log in historical_logs if log.get("inventory_id") == item["id"]), default=None)
                for item in inventory_items
            },
            categories=sorted(set(INVENTORY_CATEGORIES) | {i["category"] for i in inventory_items if i.get("category")}, key=str.lower),
            standard_categories=INVENTORY_CATEGORIES,
            notifications=notifications,
            restock_requests=restock_requests,
            unassigned_cases=unassigned_cases,
            technicians=technicians,
            milling_cases=milling_cases,
            qc_cases=qc_cases,
            disc_overview=_disc_overview(inventory_items),
            stage_labels=dict(BENCH_FLOW),
            usage_basis_labels=USAGE_BASIS_LABELS,
        )
    except Exception as e:
        return f"Inventory error: {str(e)}", 500


def _disc_overview(inventory_items):
    """Zirconia discs for the Inventory page: open discs with units milled so far, recently
    used-up discs with their real yield, and the average units per used-up disc for each item."""
    names = {i["id"]: i["item_name"] for i in inventory_items}
    try:
        discs = supabase_admin.table("zirconia_discs").select("*").order("opened_at", desc=True).execute().data or []
    except Exception:
        app.logger.warning("zirconia_discs unavailable; run the material usage migration")
        return {"open": [], "used_up": [], "yields": []}
    units = _disc_units([d["id"] for d in discs])
    for d in discs:
        d["item_name"] = names.get(d["inventory_id"], "A deleted material")
        d["units"] = units.get(d["id"], 0)
    used = [d for d in discs if d["status"] == "used_up"]
    yields = {}
    for d in used:
        yields.setdefault(d["item_name"], []).append(d["units"])
    return {
        "open": sorted((d for d in discs if d["status"] == "open"), key=lambda d: (d["item_name"], d["opened_at"])),
        "used_up": used[:15],
        "yields": [{"item_name": k, "discs": len(v), "average": round(sum(v) / len(v), 1), "lowest": min(v), "highest": max(v)}
                   for k, v in sorted(yields.items())],
    }


@app.route("/admin/discs/<disc_id>/used-up", methods=["POST"])
@role_required("admin")
def admin_mark_disc_used_up(disc_id):
    """For a disc the technician forgot to mark as used up when it ran out."""
    updated = (supabase_admin.table("zirconia_discs").update({
        "status": "used_up", "used_up_at": _now_iso(), "used_up_by": g.profile["id"],
    }).eq("id", disc_id).eq("status", "open").execute().data or [])
    if updated:
        flash(f"Disc {updated[0]['label']} is marked as used up.", "inventory-added")
    else:
        flash("That disc was already marked as used up.", "inventory-stale")
    return redirect(url_for("admin_inventory"))


# =========================
# ADMIN: ADD INVENTORY ITEM
# =========================
INVENTORY_CATEGORIES = ("Zirconia", "Ceramic & E.max", "Acrylic & Denture", "3D Printing Resin", "Consumables")


def _inventory_details_from_form():
    """Read and validate the material detail fields shared by the add and edit forms."""
    def number(field, label, default=None):
        raw = (request.form.get(field) or "").strip()
        if raw == "" and default is not None:
            return default
        try:
            value = float(raw)
        except ValueError:
            raise ValueError(f"{label} must be a number.")
        if value < 0:
            raise ValueError(f"{label} can't be negative.")
        return round(value, 2)

    details = {
        "item_name": " ".join(request.form.get("item_name", "").split())[:120],
        "category": request.form.get("category", "").strip()[:60],
        "unit": request.form.get("unit", "").strip()[:30],
        "supplier_name": request.form.get("supplier_name", "").strip()[:120] or None,
        "minimum_threshold": number("minimum_threshold", "Reorder level"),
        "unit_cost": number("unit_cost", "Unit cost", default=0.0),
    }
    if not details["item_name"]:
        raise ValueError("Enter the material's name.")
    if not details["category"]:
        raise ValueError("Choose a category.")
    if not details["unit"]:
        raise ValueError("Enter the unit, e.g. disc, block, bottle, or gram.")

    # Usage rule: fills in the amount for technicians when a case reaches the stage.
    stage = request.form.get("usage_stage") or None
    if stage and stage not in MATERIAL_LOG_STAGES:
        raise ValueError("Choose the stage this material is used at.")
    details["usage_stage"] = stage
    details["tracks_discs"] = request.form.get("tracks_discs") == "1"
    if stage:
        raw = (request.form.get("usage_amount") or "").strip()
        try:
            amount = float(raw)
        except ValueError:
            raise ValueError("Enter how much is used, e.g. 0.05 of a disc per unit.")
        if amount <= 0:
            raise ValueError("The amount used must be more than 0.")
        basis = request.form.get("usage_basis")
        if basis not in USAGE_BASIS_LABELS:
            raise ValueError("Choose whether the amount is per unit, per arch or per case.")
        details["usage_amount"] = round(amount, 4)
        details["usage_basis"] = basis
        details["usage_applies_to"] = ", ".join(_rule_tokens(request.form.get("usage_applies_to")))[:200] or None
    else:
        details.update(usage_amount=None, usage_basis=None, usage_applies_to=None)
    return details


def _duplicate_material(name, exclude_id=None):
    same = supabase_admin.table("inventory").select("id, item_name").ilike("item_name", name).execute().data or []
    return next((m for m in same if m["id"] != exclude_id and m["item_name"].strip().lower() == name.lower()), None)


@app.route("/admin/inventory/add", methods=["POST"])
@role_required("admin")
def add_inventory_item():
    try:
        try:
            details = _inventory_details_from_form()
            quantity = float(request.form.get("quantity") or 0)
            if quantity < 0:
                raise ValueError("Starting quantity can't be negative.")
        except ValueError as error:
            flash(str(error), "inventory-input")
            return redirect(url_for("admin_inventory"))
        if _duplicate_material(details["item_name"]):
            flash(f"'{details['item_name']}' is already in inventory. Adjust its stock instead of adding it again.", "inventory-input")
            return redirect(url_for("admin_inventory"))

        quantity = round(quantity, 2)
        inv_res = supabase_admin.table("inventory").insert({**details, "quantity": quantity}).execute()
        if inv_res.data and quantity:
            supabase_admin.table("inventory_logs").insert({
                "inventory_id": inv_res.data[0]["id"],
                "user_id": g.profile["id"],
                "action_type": "restock",
                "quantity_changed": quantity,
                "previous_quantity": 0,
                "new_quantity": quantity,
                "notes": "Initial stock entry",
            }).execute()
        flash(f"'{details['item_name']}' was added with {quantity:g} {details['unit']} in stock.", "inventory-added")
        return redirect(url_for("admin_inventory"))
    except Exception:
        app.logger.exception("Unable to add inventory item")
        return "Unable to add this material. Check the server logs for details.", 500


@app.route("/admin/inventory/<item_id>/edit", methods=["POST"])
@role_required("admin")
def edit_inventory_item(item_id):
    try:
        items = supabase_admin.table("inventory").select("id, item_name").eq("id", item_id).execute().data or []
        if not items:
            flash("This material no longer exists. Nothing was changed.", "inventory-stale")
            return redirect(url_for("admin_inventory"))
        try:
            details = _inventory_details_from_form()
        except ValueError as error:
            flash(str(error), "inventory-input")
            return redirect(url_for("admin_inventory"))
        if _duplicate_material(details["item_name"], exclude_id=item_id):
            flash(f"Another material is already named '{details['item_name']}'.", "inventory-input")
            return redirect(url_for("admin_inventory"))
        supabase_admin.table("inventory").update(details).eq("id", item_id).execute()
        flash(f"Details for '{details['item_name']}' were saved.", "inventory-updated")
        return redirect(url_for("admin_inventory"))
    except Exception:
        app.logger.exception("Unable to edit inventory item")
        return "Unable to save these details. Check the server logs for details.", 500


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
@role_required("admin")
def update_inventory_stock(item_id):
    """Restock, record use, or set the counted quantity; every change is logged."""
    try:
        action = request.form.get("action")
        reason = request.form.get("reason", "").strip()[:200]
        try:
            amount = round(float(request.form.get("amount") or ""), 2)
        except ValueError:
            flash("Enter a quantity as a number.", "inventory-input")
            return redirect(url_for("admin_inventory"))
        if action not in ("add", "subtract", "set"):
            return "Invalid action type.", 400
        if amount < 0 or (amount == 0 and action != "set"):
            flash("Enter a quantity greater than zero.", "inventory-input")
            return redirect(url_for("admin_inventory"))

        items = supabase_admin.table("inventory").select("*").eq("id", item_id).execute().data or []
        if not items:
            flash("This material no longer exists. Nothing was changed.", "inventory-stale")
            return redirect(url_for("admin_inventory"))
        item = items[0]
        current_qty = round(float(item.get("quantity") or 0), 2)
        unit = item.get("unit") or ""

        if action == "add":
            new_qty = round(current_qty + amount, 2)
            summary = f"Restocked +{amount:g} {unit}"
        elif action == "subtract":
            new_qty = round(max(current_qty - amount, 0), 2)
            summary = f"Recorded use of {amount:g} {unit}" + (" (capped at 0)" if current_qty - amount < 0 else "")
        else:
            new_qty = amount
            summary = f"Stock count: {amount:g} {unit} on hand"
        change = round(new_qty - current_qty, 2)
        if change == 0:
            flash(f"'{item['item_name']}' already has {current_qty:g} {unit}. Nothing was changed.", "inventory-nochange")
            return redirect(url_for("admin_inventory"))

        supabase_admin.table("inventory").update({"quantity": new_qty}).eq("id", item_id).execute()
        supabase_admin.table("inventory_logs").insert({
            "inventory_id": item_id,
            "user_id": g.profile["id"],
            # Stock counts reuse the existing log types so usage history stays consistent.
            "action_type": "restock" if change > 0 else "manual_deduction",
            "quantity_changed": change,
            "previous_quantity": current_qty,
            "new_quantity": new_qty,
            "notes": summary + (f". Reason: {reason}" if reason else ""),
        }).execute()
        flash(f"{item['item_name']}: {current_qty:g} → {new_qty:g} {unit}.", "inventory-adjusted")
        return redirect(url_for("admin_inventory"))
    except Exception:
        app.logger.exception("Unable to update stock")
        return "Unable to update stock. Check the server logs for details.", 500


# =========================
# JOB ORDER HISTORY (the lab's paper job orders, imported from CSV)
# =========================
# Completed work from before DentaLink. The clinics have no DentaLink accounts, so these are
# kept apart from dental_cases; the forecast counts them as real demand. No patient names.
JOB_ORDER_TEXT_FIELDS = {
    "clinic": 160, "dentist": 120, "clinic_ref": 80, "cad_designer": 80, "porcelain_technician": 80,
    "case_type": 80, "material": 80, "teeth": 200, "shade": 30, "is_remake": 20, "notes": 500,
}
JOB_ORDER_DATE_FIELDS = ("date_sent", "lab_in", "lab_out", "due_date")
JOB_ORDER_MAX_ROWS = 2000


def _job_orders():
    """All imported job orders, newest received first; [] before the migration has run."""
    try:
        rows = _fetch_all(lambda: supabase_admin.table("job_order_history").select("*").order("jo_ref"))
    except Exception:
        return []
    return sorted(rows, key=lambda r: (r.get("lab_in") or r.get("date_sent") or "", r["jo_ref"]), reverse=True)


def _job_orders_from_csv(upload):
    """Validated rows from a job-order CSV (the format of DentaLink_data/extracted/job_orders.csv).

    Needs jo_id, material and units columns; the rest are optional. Raises ValueError with a
    message naming the line to fix.
    """
    import csv
    import io

    raw = upload.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError("That file is larger than 1 MB. Split it into smaller files.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Save the file as CSV UTF-8 and try again.")
    reader = csv.DictReader(io.StringIO(text))
    columns = {(c or "").strip() for c in (reader.fieldnames or [])}
    missing = {"jo_id", "material", "units"} - columns
    if missing:
        raise ValueError(f"The file needs these columns: {', '.join(sorted(missing))}.")

    rows, seen = [], set()
    for line, record in enumerate(reader, start=2):
        r = {(k or "").strip(): (v or "").strip() for k, v in record.items() if k}
        if not any(r.values()):
            continue
        ref = r.get("jo_id", "")[:40]
        if not ref:
            raise ValueError(f"Line {line}: jo_id is empty.")
        if ref in seen:
            raise ValueError(f"Line {line}: {ref} appears twice in the file.")
        seen.add(ref)
        try:
            units = int(float(r.get("units") or ""))
        except ValueError:
            raise ValueError(f"Line {line} ({ref}): units must be a number.")
        if units < 1:
            raise ValueError(f"Line {line} ({ref}): units must be at least 1.")
        if not r.get("material"):
            raise ValueError(f"Line {line} ({ref}): material is empty.")
        row = {"jo_ref": ref, "units": units}
        for field, limit in JOB_ORDER_TEXT_FIELDS.items():
            value = r.get(field, "")[:limit] or None
            row["dentist_name" if field == "dentist" else field] = value
        for field in JOB_ORDER_DATE_FIELDS:
            value = r.get(field, "")
            if value:
                try:
                    value = datetime.strptime(value, "%Y-%m-%d").date().isoformat()
                except ValueError:
                    raise ValueError(f"Line {line} ({ref}): {field} must be a date like 2026-10-29.")
            row[field] = value or None
        rows.append(row)
        if len(rows) > JOB_ORDER_MAX_ROWS:
            raise ValueError(f"Import at most {JOB_ORDER_MAX_ROWS} job orders at a time.")
    if not rows:
        raise ValueError("The file has no job orders.")
    return rows


def _completed_case_rows():
    """Delivered and completed DentaLink cases, shaped like Job Order History rows."""
    cases = _fetch_all(lambda: supabase_admin.table("dental_cases").select("*")
                       .in_("status", list(DELIVERED_STATUSES)).order("created_at"))
    if not cases:
        return []
    ids = [c["id"] for c in cases]
    teeth = _fetch_all(lambda: supabase_admin.table("case_teeth").select("case_id, tooth_number, material, shade")
                       .in_("case_id", ids).order("case_id"))
    history = _fetch_all(lambda: supabase_admin.table("case_status_history").select("case_id, created_at")
                         .in_("case_id", ids).order("created_at"))
    people_ids = list({pid for c in cases for pid in (c.get("dentist_id"), c.get("technician_id")) if pid})
    people = {p["id"]: p for p in (supabase_admin.table("profiles").select("id, display_name, first_name, last_name, clinic_name")
                                   .in_("id", people_ids).execute().data or [])} if people_ids else {}
    teeth_by_case, first_event = {}, {}
    for t in teeth:
        teeth_by_case.setdefault(t["case_id"], []).append(t)
    for h in history:
        first_event.setdefault(h["case_id"], h["created_at"])
    rows = []
    for c in cases:
        dentist = people.get(c.get("dentist_id"), {})
        case_teeth = teeth_by_case.get(c["id"], [])
        mix = {}
        for t in case_teeth:
            key = ((t.get("material") or "not specified").replace("_", " "), t.get("shade") or "")
            mix[key] = mix.get(key, 0) + 1
        out = c.get("dispatched_at") or c.get("delivered_at")
        rows.append({
            "source": "dentalink",
            "id": c["id"],
            "jo_ref": c["case_number"],
            "clinic": dentist.get("clinic_name") or "Independent practice",
            "dentist_name": _display_name(dentist, "") if dentist else None,
            "case_type": (c.get("case_type") or "").replace("_", " "),
            "units": len(case_teeth),
            "teeth": ", ".join(str(t.get("tooth_number")) for t in case_teeth),
            "material": ", ".join(f"{n} × {m}{' ' + sh if sh else ''}" for (m, sh), n in mix.items()) or "Not specified",
            "material_units": {m: sum(n for (mm, _sh), n in mix.items() if mm == m) for (m, _sh) in mix},
            "shade": None,
            # The lab's "IN" date: its first record of the case (usually assignment); "out": left with the courier.
            "lab_in": str(first_event.get(c["id"]) or c.get("created_at") or "")[:10] or None,
            "lab_out": str(out)[:10] if out else None,
            "delivered_on": str(c.get("delivered_at") or "")[:10] or None,
            "due_date": c.get("due_date"),
            "technician": _display_name(people.get(c.get("technician_id")), "") if c.get("technician_id") in people else None,
            "is_remake": "yes" if c.get("remake_of") else "no",
            "status": c.get("status"),
            "notes": None,
        })
    return rows


@app.route("/admin/job-orders")
@role_required("admin")
def admin_job_orders():
    """Every finished job order: the lab's imported paper ones and DentaLink's delivered cases."""
    try:
        supabase_admin.table("job_order_history").select("id").limit(1).execute()
        migrated = True
    except Exception:
        migrated = False
    paper = [dict(o, source="paper") for o in (_job_orders() if migrated else [])]
    try:
        digital = _completed_case_rows()
    except Exception:
        app.logger.exception("Unable to load completed DentaLink cases")
        digital = []
    orders = sorted(paper + digital, key=lambda r: (r.get("lab_in") or r.get("date_sent") or "", r["jo_ref"]), reverse=True)
    for o in orders:
        start, end = _parse_timestamp(o.get("lab_in")), _parse_timestamp(o.get("lab_out"))
        o["turnaround"] = (end - start).days if start and end else None
        o["month"] = (o.get("lab_in") or o.get("date_sent") or "")[:7]
    turnarounds = [o["turnaround"] for o in orders if o["turnaround"] is not None]
    by_material = {}
    for o in paper:
        family = (o.get("material") or "Other").split(" (")[0].strip().capitalize()
        by_material[family] = by_material.get(family, 0) + int(o.get("units") or 0)
    for o in digital:
        for material, n in o["material_units"].items():
            family = material.strip().capitalize()
            by_material[family] = by_material.get(family, 0) + n
    months = sorted({o["month"] for o in orders if o["month"]}, reverse=True)
    return render_template(
        "admin_job_orders.html",
        profile=g.profile,
        migrated=migrated,
        orders=orders,
        paper_count=len(paper),
        digital_count=len(digital),
        total_units=sum(int(o.get("units") or 0) for o in orders),
        clinics=len({o.get("clinic") for o in orders if o.get("clinic")}),
        avg_turnaround=round(sum(turnarounds) / len(turnarounds), 1) if turnarounds else None,
        by_material=sorted(by_material.items(), key=lambda kv: -kv[1]),
        months=months,
    )


@app.route("/admin/job-orders/import", methods=["POST"])
@role_required("admin")
def admin_import_job_orders():
    upload = request.files.get("csv_file")
    if not upload or not upload.filename:
        flash("Choose a CSV file to import.", "joborders-input")
        return redirect(url_for("admin_job_orders"))
    try:
        rows = _job_orders_from_csv(upload)
    except ValueError as error:
        flash(str(error), "joborders-input")
        return redirect(url_for("admin_job_orders"))
    try:
        refs = [r["jo_ref"] for r in rows]
        existing = {
            r["jo_ref"]: r["id"]
            for r in supabase_admin.table("job_order_history").select("id, jo_ref").in_("jo_ref", refs).execute().data or []
        }
        new_rows = [dict(r, imported_by=g.profile["id"]) for r in rows if r["jo_ref"] not in existing]
        if new_rows:
            supabase_admin.table("job_order_history").insert(new_rows).execute()
        for r in rows:
            if r["jo_ref"] in existing:
                supabase_admin.table("job_order_history").update(r).eq("id", existing[r["jo_ref"]]).execute()
    except Exception:
        app.logger.exception("Unable to import job orders")
        flash("Couldn't save the job orders. Run migration 20261101030000 in Supabase, then try again.", "joborders-input")
        return redirect(url_for("admin_job_orders"))
    added, updated = len(new_rows), len(rows) - len(new_rows)
    parts = [f"{added} job order{'s' if added != 1 else ''} added"] + ([f"{updated} updated"] if updated else [])
    flash(f"{' and '.join(parts)}. They now count as real demand in Forecast Analytics.", "joborders-imported")
    return redirect(url_for("admin_job_orders"))


@app.route("/admin/job-orders/<order_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_job_order(order_id):
    try:
        deleted = supabase_admin.table("job_order_history").delete().eq("id", order_id).execute().data or []
    except Exception:
        app.logger.exception("Unable to delete job order")
        return "Unable to delete this job order. Check the server logs for details.", 500
    if deleted:
        flash(f"{deleted[0]['jo_ref']} was removed from Job Order History.", "joborder-deleted")
    else:
        flash("That job order was already removed.", "joborders-input")
    return redirect(url_for("admin_job_orders"))


# =========================
# MACHINE LEARNING: MATERIAL DEMAND FORECAST
# =========================
# The engine (ml_models/engine.py) rebuilds material demand from job orders, backtests six
# models, and forecasts the next 4 weeks per zirconia disc shade and for e.max. A run takes
# about a minute, so it runs in a background thread and stores its result in forecast_runs;
# the page shows the latest result and compares it with the current inventory.
FORECAST_RUN_TIMEOUT = timedelta(minutes=10)
FORECAST_SOURCES = {"simulated", "real"}


def _fetch_all(query_fn, page_size=1000):
    """Every row of a Supabase select, page by page (one request returns at most 1,000)."""
    rows, start = [], 0
    while True:
        batch = query_fn().range(start, start + page_size - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < page_size:
            return rows
        start += page_size


def _real_case_units():
    """Real demand rows (date, material, shade, units): one per tooth of every non-draft
    DentaLink case, plus each imported paper job order (Job Order History)."""
    rows = []
    cases = _fetch_all(lambda: supabase_admin.table("dental_cases").select("id, created_at, status")
                       .not_.in_("status", ["draft", "cancelled"]).order("created_at"))
    dates = {c["id"]: str(c["created_at"])[:10] for c in cases if c.get("created_at")}
    if dates:
        teeth = _fetch_all(lambda: supabase_admin.table("case_teeth").select("case_id, material, shade").order("case_id"))
        rows += [{"date": dates[t["case_id"]], "material": t.get("material"), "shade": t.get("shade"), "units": 1}
                 for t in teeth if t.get("case_id") in dates]
    for jo in _job_orders():
        day = jo.get("lab_in") or jo.get("date_sent")
        if day:
            rows.append({"date": str(day)[:10], "material": jo.get("material"), "shade": jo.get("shade"),
                         "units": int(jo.get("units") or 0)})
    return rows


def _real_history_weeks(rows):
    if not rows:
        return 0
    first = datetime.strptime(min(r["date"] for r in rows), "%Y-%m-%d").date()
    today = datetime.now(LAB_TZ).date()
    return max(((today - timedelta(days=today.weekday())) - (first - timedelta(days=first.weekday()))).days // 7, 0)


def _forecast_runs(limit=5):
    """Latest forecast runs, newest first, or None if the forecast_runs table doesn't exist yet."""
    try:
        return (supabase_admin.table("forecast_runs").select("id, created_at, finished_at, status, source, error, requested_by")
                .order("created_at", desc=True).limit(limit).execute().data or [])
    except Exception:
        return None


def _run_is_stale(run):
    started = _parse_timestamp(run.get("created_at"))
    return run.get("status") == "running" and (not started or datetime.now(timezone.utc) - started > FORECAST_RUN_TIMEOUT)


def _forecast_worker(run_id, source, requested_by):
    """Background thread: run the engine and store the result (or the error) on the run row."""
    from ml_models.engine import run_forecast, stock_plan
    try:
        cases = _real_case_units() if source == "real" else None
        result = run_forecast(cases=cases, source=source)
        supabase_admin.table("forecast_runs").update({
            "status": "done", "result": result, "finished_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", run_id).execute()

        inventory = supabase_admin.table("inventory").select("item_name, quantity").execute().data or []
        urgent = [r for r in stock_plan(result, inventory) if r["status"] == "order_now"]
        if urgent:
            items = ", ".join(f"{r['order_qty']} × {r['label']}" for r in urgent)
            message = f"Forecast ready: order now to avoid running out: {items}."
        else:
            message = "Forecast ready: stock on hand covers the next order window."
        create_notification(requested_by, message, link="/admin/forecast-analytics")
    except Exception as exc:
        app.logger.exception("Forecast run failed")
        try:
            supabase_admin.table("forecast_runs").update({
                "status": "failed", "error": str(exc)[:500], "finished_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", run_id).execute()
        except Exception:
            app.logger.exception("Unable to record the failed forecast run")


def _latest_forecast():
    """(latest finished run with its result, latest run of any status, runs list or None)."""
    runs = _forecast_runs()
    if runs is None:
        return None, None, None
    done_id = next((r["id"] for r in runs if r["status"] == "done"), None)
    done = None
    if done_id:
        rows = supabase_admin.table("forecast_runs").select("*").eq("id", done_id).execute().data or []
        done = rows[0] if rows and isinstance(rows[0].get("result"), dict) else None
    latest = runs[0] if runs else None
    if latest and _run_is_stale(latest):
        latest = dict(latest, status="failed", error="The run took too long or the server restarted. Run it again.")
    return done, latest, runs


@app.route("/admin/forecast-analytics")
@role_required("admin")
def admin_forecast_analytics():
    from ml_models.engine import MIN_REAL_WEEKS, profile_summary, stock_plan
    try:
        done, latest, runs = _latest_forecast()
        inventory = supabase_admin.table("inventory").select("item_name, quantity, unit").execute().data or []
        plan = stock_plan(done["result"], inventory) if done else []
        try:
            real_rows = _real_case_units()
        except Exception:
            app.logger.exception("Unable to count real case units")
            real_rows = []
        real_weeks = _real_history_weeks(real_rows)
        return render_template(
            "admin_forecast.html",
            profile=g.profile,
            migrated=runs is not None,
            run=done,
            result=done["result"] if done else None,
            latest=latest,
            plan=plan,
            disc_rows=[r for r in plan if r["kind"] == "disc"],
            assumptions=done["result"]["assumptions"] if done else profile_summary(),
            real_units=sum(int(r["units"] or 0) for r in real_rows),
            real_weeks=real_weeks,
            min_real_weeks=MIN_REAL_WEEKS,
        )
    except Exception:
        app.logger.exception("Unable to load forecast analytics")
        return "Unable to load Forecast Analytics. Check the server logs for details.", 500


@app.route("/admin/forecast/run", methods=["POST"])
@role_required("admin")
def admin_run_forecast():
    import threading
    from ml_models.engine import MIN_REAL_WEEKS

    source = request.form.get("source", "simulated")
    if source not in FORECAST_SOURCES:
        source = "simulated"
    runs = _forecast_runs(limit=1)
    if runs is None:
        flash("Run the forecast_runs migration in Supabase first.", "forecast-stale")
        return redirect(url_for("admin_forecast_analytics"))
    if runs and runs[0]["status"] == "running" and not _run_is_stale(runs[0]):
        flash("A forecast is already running. This page updates by itself when it finishes.", "forecast-stale")
        return redirect(url_for("admin_forecast_analytics"))
    if source == "real" and _real_history_weeks(_real_case_units()) < MIN_REAL_WEEKS:
        flash(f"DentaLink needs at least {MIN_REAL_WEEKS} weeks of real cases before it can forecast from them alone.", "forecast-stale")
        return redirect(url_for("admin_forecast_analytics"))
    if runs and _run_is_stale(runs[0]):
        supabase_admin.table("forecast_runs").update({"status": "failed", "error": "Timed out"}).eq("id", runs[0]["id"]).execute()

    row = supabase_admin.table("forecast_runs").insert({
        "requested_by": g.profile["id"], "source": source, "status": "running",
    }).execute().data[0]
    threading.Thread(target=_forecast_worker, args=(row["id"], source, g.profile["id"]), daemon=True).start()
    flash("The forecast is running. It takes about a minute, and this page updates by itself when it's done.", "forecast-started")
    return redirect(url_for("admin_forecast_analytics"))


FORECAST_STATUS_TEXT = {"order_now": "Order now", "order_soon": "Order this month", "enough": "Enough stock",
                        "untracked": "Not in Inventory"}


def _forecast_generated_local(result):
    """When the forecast was run, in the lab's time zone (None if not recorded)."""
    ts = _parse_timestamp(result.get("generated_at"))
    return ts.astimezone(LAB_TZ) if ts else None


@app.route("/admin/forecast/export-excel")
@role_required("admin")
def admin_forecast_export_excel():
    """The forecast and order plan as a formatted Excel workbook (same layout as Reports)."""
    from ml_models.engine import stock_plan

    done, _, _ = _latest_forecast()
    if not done:
        return "No forecast has been run yet.", 404
    result = done["result"]
    inventory = supabase_admin.table("inventory").select("item_name, quantity").execute().data or []
    plan = stock_plan(result, inventory)
    run_at = _forecast_generated_local(result)
    start = _lab_day(result.get("forecast_start"))

    plan_rows = []
    for r in plan:
        unit = "discs" if r["kind"] == "disc" else "units"
        plan_rows.append([
            r["label"], round(r["units_4w"]), round(r["units_90"]), r["need_4w"], unit, r["reorder_point"],
            "Not tracked" if r["on_hand"] is None else round(float(r["on_hand"]), 2), FORECAST_STATUS_TEXT.get(r["status"], r["status"]),
            r["order_qty"] or 0, round(r["order_cost"]) if r.get("order_cost") else "—",
        ])
    weeks = [f["week"] for f in result["series"][0]["forecast"]] if result["series"] else []
    best = result["scores"].get(result["best_model"], {})
    report = {
        "sections": [
            {"title": "Order plan",
             "columns": [("Material", False), ("Forecast, 4 weeks (units)", True), ("Cautious, 90% (units)", True),
                         ("To be safe", True), ("Unit", False), ("Reorder point", True), ("On hand", True),
                         ("Status", False), ("Order quantity", True), ("Order cost (PHP)", True)],
             "rows": plan_rows, "empty": "No materials in the forecast.",
             "foot": ["Total order cost (zirconia discs)", "", "", "", "", "", "", "", "",
                      round(sum(r.get("order_cost") or 0 for r in plan if r["kind"] == "disc"))] if plan_rows else None},
            {"title": "Weekly forecast",
             "columns": [("Material", False)] + [(f"Week of {_fmt_day(_lab_day(w))}", True) for w in weeks],
             "rows": [[s["label"]] + [round(f["units"]) for f in s["forecast"]] for s in result["series"]],
             "empty": "No weekly forecast."},
            {"title": "Method comparison",
             "columns": [("Method", False), ("4-week miss (WAPE)", True), ("Next-week miss", True), ("Used for the plan", False)],
             "rows": [[name, f"{s['wape_4_weeks'] * 100:.1f}%", f"{s['wape_next_week'] * 100:.1f}%",
                       "Yes" if name == result["best_model"] else ""]
                      for name, s in sorted(result["scores"].items(), key=lambda kv: kv[1]["wape_4_weeks"])],
             "empty": "No methods compared."},
        ],
        "notes": [
            f"Method used: {result['best_model']} (about ±{round(best.get('wape_4_weeks', 0) * 100)}% typical 4-week miss). "
            f"Each method forecast each of the last {result.get('test_weeks')} weeks without seeing them.",
            ("Learned from " + (f"{result.get('history_weeks')} weeks of job orders simulated from the lab's own figures."
                                if result.get("source") == "simulated" else f"{result.get('history_weeks')} weeks of DentaLink cases.")
             + " Units are converted to discs at 18–25 units per disc; \"To be safe\" uses 18. Supplier delivery takes 2–4 working days."),
        ],
    }
    period_label = (f"4 weeks from {_fmt_day(start)}" if start else "Next 4 weeks") + (
        f" · forecast run {run_at:%b %d, %Y %I:%M %p}" if run_at else "")
    data = _report_xlsx("Material Forecast & Order Plan", period_label, report, g.profile, datetime.now(LAB_TZ))
    response = make_response(data)
    response.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    response.headers["Content-Disposition"] = f"attachment; filename=DentaLink_Forecast_{result.get('forecast_start', 'latest')}.xlsx"
    return response


@app.route("/admin/forecast/report-preview")
@role_required("admin")
def admin_forecast_report_preview():
    from ml_models.engine import stock_plan

    done, _, _ = _latest_forecast()
    if not done:
        return redirect(url_for("admin_forecast_analytics"))
    inventory = supabase_admin.table("inventory").select("item_name, quantity").execute().data or []
    return render_template("admin_forecast_report.html", profile=g.profile, run=done, result=done["result"],
                           plan=stock_plan(done["result"], inventory),
                           generated_local=_forecast_generated_local(done["result"]),
                           printed_at=datetime.now(LAB_TZ))


# =========================
# REPORTS: printable summaries for every role
# =========================
# Each report builder returns {"sections": [...], "notes": [...]};
# a section is {"title", "columns": [(label, is_number)], "rows": [[...]], "foot": [...] | None,
# "empty": text}. reports.html prints any of them, and ?format=xlsx downloads them as an Excel workbook.
REPORTS = {
    "admin": [
        ("job_orders", "Job Orders Summary", "Job orders received by month, prosthesis type, material and clinic, including imported paper job orders."),
        ("turnaround", "Turnaround & On-Time Delivery", "How long cases took and how many arrived by their due date."),
        ("remakes", "Remakes & Quality", "Problems clinics reported after delivery and the lab's decisions."),
        ("materials", "Material Usage & Stock", "Materials used on cases, restocks and current stock levels."),
    ],
    "technician": [
        ("my_production", "My Production Summary", "Cases you completed, by month, type and material."),
        ("my_workload", "My Workload Sheet", "Your open cases by due date, to print and keep at the bench."),
    ],
    "dentist": [
        ("my_cases", "Case History", "Job orders you sent to the lab; filter by patient for a patient's case record."),
        ("lab_service", "Lab Service Summary", "On-time delivery, turnaround, issues and remakes for your cases."),
    ],
}
REPORTS_WITHOUT_PERIOD = {"my_workload"}
REPORT_STATUS_LABELS = {
    "submitted": "Waiting for the lab", "assigned": "Assigned", "in_progress": "In progress",
    "cad_designing": "CAD design", "wax_up_or_try_in": "Wax-up / setup", "milling_or_printing": "Milling / printing",
    "packing_and_curing": "Packing & curing", "finishing_and_quality_check": "Finishing", "pending_admin_review": "Lab QC",
    "ready_for_dispatch": "Packed for dispatch", "in_transit": "In transit", "delivered": "Delivered",
    "completed": "Completed", "cancelled": "Cancelled", "needs_revision": "Returned to dentist",
}
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December")


def _month_start(d):
    return d.replace(day=1)


def _next_month(d):
    return d.replace(year=d.year + 1, month=1, day=1) if d.month == 12 else d.replace(month=d.month + 1, day=1)


def _report_period(kind, ref):
    """The reporting period chosen in the toolbar: a month, a quarter, a year, or all time.
    Returns start (inclusive) and end (exclusive) calendar days in the lab's time zone."""
    today = datetime.now(LAB_TZ).date()
    try:
        if kind == "all":
            start, end, label = today.replace(year=2000, month=1, day=1), today + timedelta(days=1), "All time"
        elif kind == "year":
            y = int(ref)
            start, end, label = today.replace(year=y, month=1, day=1), today.replace(year=y + 1, month=1, day=1), f"Year {y}"
        elif kind == "quarter":
            y, q = (int(x) for x in str(ref).split("-Q"))
            if not 1 <= q <= 4:
                raise ValueError(ref)
            start = today.replace(year=y, month=3 * q - 2, day=1)
            end = _next_month(_next_month(_next_month(start)))
            label = f"Q{q} {y} ({MONTH_NAMES[start.month - 1][:3]}–{MONTH_NAMES[start.month + 1][:3]})"
        else:
            kind = "month"
            y, m = (int(x) for x in str(ref).split("-"))
            start = today.replace(year=y, month=m, day=1)
            end, label = _next_month(start), f"{MONTH_NAMES[m - 1]} {y}"
    except (ValueError, TypeError, IndexError):
        kind, start = "month", _month_start(today)
        end, label = _next_month(start), f"{MONTH_NAMES[start.month - 1]} {start.year}"
    ref = {"month": f"{start:%Y-%m}", "quarter": f"{start.year}-Q{(start.month - 1) // 3 + 1}",
           "year": str(start.year), "all": ""}[kind]
    return {"kind": kind, "ref": ref, "start": start, "end": end, "label": label}


def _period_options():
    """Choices for the period pickers: the last 3 years of months, 12 quarters, 7 years."""
    today = datetime.now(LAB_TZ).date()
    months, d = [], _month_start(today)
    for _ in range(36):
        months.append((f"{d:%Y-%m}", f"{MONTH_NAMES[d.month - 1]} {d.year}"))
        d = (d - timedelta(days=1)).replace(day=1)
    quarters, y, q = [], today.year, (today.month - 1) // 3 + 1
    for _ in range(12):
        quarters.append((f"{y}-Q{q}", f"Q{q} {y}"))
        y, q = (y - 1, 4) if q == 1 else (y, q - 1)
    years = [(str(y), str(y)) for y in range(today.year, today.year - 7, -1)]
    return {"month": months, "quarter": quarters, "year": years}


def _lab_day(value):
    """Calendar day (lab time zone) of a stored timestamp, or of a plain YYYY-MM-DD date."""
    if not value:
        return None
    text = str(value)
    if len(text) <= 10:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    ts = _parse_timestamp(text)
    return ts.astimezone(LAB_TZ).date() if ts else None


def _in_period(day, period):
    return day is not None and period["start"] <= day < period["end"]


def _period_months(period, days):
    """Month rows for a by-month table: every month of the period, or for "All time" the
    months that have data."""
    if period["kind"] == "all":
        return sorted({_month_start(d) for d in days if d})
    months, d = [], period["start"]
    while d < period["end"]:
        months.append(d)
        d = _next_month(d)
    return months


def _fmt_day(day):
    return f"{MONTH_NAMES[day.month - 1][:3]} {day.day}, {day.year}" if day else "—"


def _fmt_month(day):
    return f"{MONTH_NAMES[day.month - 1]} {day.year}"


def _pct(part, whole):
    return f"{round(100 * part / whole)}%" if whole else "—"


def _avg(values):
    return f"{sum(values) / len(values):.1f}" if values else "—"


def _material_family(name):
    return (name or "Not specified").replace("_", " ").split(" (")[0].strip().capitalize()


def _clinic_key(name):
    """Grouping key for a clinic name: case, spacing and stray punctuation don't matter, so
    "SmileCare  Dental Clinic." and "smilecare dental clinic" are the same clinic."""
    import re
    text = re.sub(r"[.,;:'’\"]+", "", (name or "").casefold())
    return " ".join(text.replace("&", " and ").split())


def _clinic_of(dentist, dentist_id):
    """(key, label) for a dentist's clinic; a dentist without a clinic name stands alone."""
    name = " ".join(((dentist or {}).get("clinic_name") or "").split())
    if name:
        return _clinic_key(name), name
    return f"dentist:{dentist_id}", f"{_display_name(dentist, 'Unknown dentist')} (no clinic name)"


def _report_cases(dentist_id=None, technician_id=None):
    """Sent (non-draft) cases with their teeth and names, plus the derived days reports use."""
    def query():
        q = supabase_admin.table("dental_cases").select("*").neq("status", "draft")
        if dentist_id:
            q = q.eq("dentist_id", dentist_id)
        if technician_id:
            q = q.eq("technician_id", technician_id)
        return q.order("created_at")

    cases = _fetch_all(query)
    ids = [c["id"] for c in cases]
    teeth = []
    for i in range(0, len(ids), 150):  # keep each request URL short
        chunk = ids[i:i + 150]
        teeth += _fetch_all(lambda chunk=chunk: supabase_admin.table("case_teeth")
                            .select("case_id, tooth_number, material, shade").in_("case_id", chunk).order("case_id"))
    teeth_by_case = {}
    for t in teeth:
        teeth_by_case.setdefault(t["case_id"], []).append(t)
    people_ids = list({pid for c in cases for pid in (c.get("dentist_id"), c.get("technician_id")) if pid})
    people = {}
    if people_ids:
        rows = (supabase_admin.table("profiles").select("id, display_name, first_name, last_name, clinic_name")
                .in_("id", people_ids).execute().data or [])
        people = {p["id"]: p for p in rows}

    for c in cases:
        c["teeth"] = sorted(teeth_by_case.get(c["id"], []), key=lambda t: str(t.get("tooth_number")))
        c["units"] = len(c["teeth"])
        c["received"] = _lab_day(c.get("created_at"))
        c["delivered"] = _lab_day(c.get("delivered_at")) if c.get("status") in DELIVERED_STATUSES else None
        c["due"] = _lab_day(c.get("due_date"))
        c["turnaround"] = (c["delivered"] - c["received"]).days if c["delivered"] and c["received"] else None
        c["late_days"] = max(0, (c["delivered"] - c["due"]).days) if c["delivered"] and c["due"] else None
        dentist = people.get(c.get("dentist_id"))
        c["dentist_name"] = _display_name(dentist, "—")
        c["clinic_key"], c["clinic"] = _clinic_of(dentist, c.get("dentist_id"))
        c["technician_name"] = _display_name(people.get(c.get("technician_id")), "Unassigned")
        c["status_label"] = REPORT_STATUS_LABELS.get(c.get("status"), (c.get("status") or "").replace("_", " ").capitalize())
        c["type_label"] = (c.get("case_type") or "—").replace("_", " ").title()
        c["material_units"] = {}
        for t in c["teeth"]:
            fam = _material_family(t.get("material"))
            c["material_units"][fam] = c["material_units"].get(fam, 0) + 1
        groups = {}
        for t in c["teeth"]:
            key = (_material_family(t.get("material")), t.get("shade") or "")
            groups.setdefault(key, []).append(str(t.get("tooth_number")))
        c["material_text"] = "; ".join(f"{', '.join(nums)}: {m}{' ' + s if s else ''}" for (m, s), nums in groups.items()) or "—"
        c["teeth_text"] = ", ".join(str(t.get("tooth_number")) for t in c["teeth"]) or "—"
    return cases


def _report_issue_reports():
    try:
        return _fetch_all(lambda: supabase_admin.table("case_issue_reports").select("*").order("created_at"))
    except Exception:
        app.logger.warning("case_issue_reports unavailable; run the issue reports migration")
        return []


def _count_table(title, label, counts, unit="Job orders", empty="Nothing in this period."):
    """A two-column table from {name: count}, largest first, with a total row."""
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    total = sum(counts.values())
    return {"title": title, "columns": [(label, False), (unit, True), ("Share", True)],
            "rows": [[k, v, _pct(v, total)] for k, v in rows],
            "foot": ["Total", total, "100%" if total else "—"] if rows else None, "empty": empty}


def _delivery_by_month(period, delivered):
    """By-month delivery table: delivered, on time, late, on-time %, average days."""
    rows = []
    for m in _period_months(period, [c["delivered"] for c in delivered]):
        month = [c for c in delivered if _month_start(c["delivered"]) == m]
        timed = [c for c in month if c["late_days"] is not None]
        on_time = sum(1 for c in timed if c["late_days"] == 0)
        rows.append([_fmt_month(m), len(month), on_time, len(timed) - on_time, _pct(on_time, len(timed)),
                     _avg([c["turnaround"] for c in month if c["turnaround"] is not None])])
    timed = [c for c in delivered if c["late_days"] is not None]
    on_time = sum(1 for c in timed if c["late_days"] == 0)
    return {"title": "By month", "columns": [("Month", False), ("Delivered", True), ("On time", True), ("Late", True),
                                             ("On-time rate", True), ("Avg. days", True)],
            "rows": rows, "empty": "No deliveries in this period.",
            "foot": ["Total", len(delivered), on_time, len(timed) - on_time, _pct(on_time, len(timed)),
                     _avg([c["turnaround"] for c in delivered if c["turnaround"] is not None])] if rows else None}


# ---- Admin reports ----
def _report_job_orders(period, **_):
    cases = [c for c in _report_cases() if _in_period(c["received"], period)]
    entries = [{
        "ref": c["case_number"], "day": c["received"], "source": "DentaLink", "clinic": c["clinic"],
        "type": c["type_label"], "units": c["units"], "materials": c["material_units"],
        "material_text": c["material_text"], "technician": c["technician_name"], "status": c["status_label"],
        "out": c["delivered"], "remake": bool(c.get("remake_of")), "cancelled": c.get("status") == "cancelled",
    } for c in cases]
    for o in _job_orders():
        day = _lab_day(o.get("lab_in") or o.get("date_sent"))
        if not _in_period(day, period):
            continue
        units = int(o.get("units") or 0)
        entries.append({
            "ref": o["jo_ref"], "day": day, "source": "Paper", "clinic": o.get("clinic") or "—",
            "type": (o.get("case_type") or "—").replace("_", " ").title(), "units": units,
            "materials": {_material_family(o.get("material")): units}, "material_text": o.get("material") or "—",
            "technician": o.get("technician") or "—", "status": "Completed" if o.get("lab_out") else "Recorded",
            "out": _lab_day(o.get("lab_out")), "remake": str(o.get("is_remake") or "").strip().lower() in ("yes", "y", "true", "1"),
            "cancelled": False,
        })
    entries.sort(key=lambda e: (e["day"], e["ref"]))
    active = [e for e in entries if not e["cancelled"]]

    month_rows = []
    for m in _period_months(period, [e["day"] for e in entries]):
        month = [e for e in active if _month_start(e["day"]) == m]
        month_rows.append([_fmt_month(m), len(month), sum(e["units"] for e in month),
                           sum(1 for e in month if e["source"] == "DentaLink"), sum(1 for e in month if e["source"] == "Paper"),
                           sum(1 for e in month if e["remake"])])
    by_type, by_material, by_clinic, clinic_label = {}, {}, {}, {}
    for e in active:
        by_type[e["type"]] = by_type.get(e["type"], 0) + 1
        key = _clinic_key(e["clinic"]) or e["clinic"]   # one row per clinic, however it was typed
        label = clinic_label.setdefault(key, e["clinic"])
        by_clinic[label] = by_clinic.get(label, 0) + 1
        for fam, n in e["materials"].items():
            by_material[fam] = by_material.get(fam, 0) + n

    return {
        "sections": [
            {"title": "Job orders by month", "columns": [("Month", False), ("Job orders", True), ("Units", True),
                                                          ("DentaLink", True), ("Paper", True), ("Remakes", True)],
             "rows": month_rows, "empty": "No job orders in this period.",
             "foot": ["Total"] + [sum(r[i] for r in month_rows) for i in range(1, 6)] if month_rows else None},
            _count_table("By prosthesis type", "Prosthesis type", by_type),
            _count_table("By material", "Material", by_material, unit="Units"),
            _count_table("By clinic", "Clinic", by_clinic),
            {"title": "Job order list", "columns": [("Ref", False), ("Received", False), ("Source", False), ("Clinic", False),
                                                     ("Type", False), ("Units", True), ("Material", False), ("Technician", False),
                                                     ("Status", False), ("Out / delivered", False)],
             "rows": [[e["ref"] + (" (remake)" if e["remake"] else ""), _fmt_day(e["day"]), e["source"], e["clinic"], e["type"],
                       e["units"], e["material_text"], e["technician"], e["status"], _fmt_day(e["out"])] for e in entries],
             "empty": "No job orders in this period.", "wide": True},
        ],
        "notes": ["DentaLink job orders count from the day they were sent to the lab; paper job orders from their lab-in date "
                  "(or date sent). Cancelled job orders are listed but not counted in the totals."],
    }


def _report_turnaround(period, **_):
    delivered = [c for c in _report_cases() if _in_period(c["delivered"], period)]
    timed = [c for c in delivered if c["late_days"] is not None]
    by_tech = {}
    for c in delivered:
        by_tech.setdefault(c["technician_name"], []).append(c)
    tech_rows = []
    for name, rows in sorted(by_tech.items()):
        t = [c for c in rows if c["late_days"] is not None]
        ok = sum(1 for c in t if c["late_days"] == 0)
        tech_rows.append([name, len(rows), sum(c["units"] for c in rows), _pct(ok, len(t)),
                          _avg([c["turnaround"] for c in rows if c["turnaround"] is not None])])
    late = sorted((c for c in timed if c["late_days"]), key=lambda c: -c["late_days"])
    return {
        "sections": [
            _delivery_by_month(period, delivered),
            {"title": "By technician", "columns": [("Technician", False), ("Delivered", True), ("Units", True),
                                                    ("On-time rate", True), ("Avg. days", True)],
             "rows": tech_rows, "empty": "No deliveries in this period."},
            {"title": "Late deliveries", "columns": [("Case", False), ("Clinic", False), ("Technician", False), ("Due", False),
                                                      ("Delivered", False), ("Days late", True)],
             "rows": [[c["case_number"], c["clinic"], c["technician_name"], _fmt_day(c["due"]), _fmt_day(c["delivered"]),
                       c["late_days"]] for c in late],
             "empty": "Every case in this period was delivered on time."},
        ],
        "notes": ["Turnaround is counted from the day the dentist sent the job order to the day the clinic confirmed receipt. "
                  "Only DentaLink cases are included; paper job orders don't record delivery to the clinic."],
    }


def _report_remakes(period, **_):
    cases = _report_cases()
    by_id = {c["id"]: c for c in cases}
    delivered = [c for c in cases if _in_period(c["delivered"], period)]
    reports = [r for r in _report_issue_reports() if _in_period(_lab_day(r.get("created_at")), period)]
    types = {}
    for r in reports:
        row = types.setdefault(ISSUE_TYPES.get(r.get("issue_type"), "Other problem"), [0, 0, 0, 0])
        row[0] += 1
        row[{"remake_accepted": 1, "no_action": 2}.get(r.get("status"), 3)] += 1
    month_rows = []
    for m in _period_months(period, [_lab_day(r.get("created_at")) for r in reports] + [c["delivered"] for c in delivered]):
        month_reports = [r for r in reports if _month_start(_lab_day(r.get("created_at"))) == m]
        month_delivered = sum(1 for c in delivered if _month_start(c["delivered"]) == m)
        month_remakes = sum(1 for r in month_reports if r.get("status") == "remake_accepted")
        month_rows.append([_fmt_month(m), month_delivered, len(month_reports), month_remakes, _pct(month_remakes, month_delivered)])
    decision = {"remake_accepted": "Remake accepted", "no_action": "No remake needed", "open": "Under review"}
    return {
        "sections": [
            {"title": "By month", "columns": [("Month", False), ("Delivered", True), ("Issues reported", True),
                                               ("Remakes", True), ("Remake rate", True)],
             "rows": month_rows, "empty": "Nothing in this period."},
            {"title": "By type of problem", "columns": [("Problem", False), ("Reported", True), ("Remade", True),
                                                         ("No remake", True), ("Under review", True)],
             "rows": [[k] + v for k, v in sorted(types.items(), key=lambda kv: -kv[1][0])],
             "empty": "No issues were reported in this period."},
            {"title": "Issue list", "columns": [("Reported", False), ("Case", False), ("Clinic", False), ("Technician", False),
                                                 ("Problem", False), ("Details", False), ("Decision", False), ("Remake", False)],
             "rows": [[_fmt_day(_lab_day(r.get("created_at"))), by_id.get(r["case_id"], {}).get("case_number", "—"),
                       by_id.get(r["case_id"], {}).get("clinic", "—"), by_id.get(r["case_id"], {}).get("technician_name", "—"),
                       ISSUE_TYPES.get(r.get("issue_type"), "Other problem"), r.get("details") or "—",
                       decision.get(r.get("status"), r.get("status") or "—") + (f" ({r['admin_note']})" if r.get("admin_note") else ""),
                       by_id.get(r.get("remake_case_id"), {}).get("case_number", "—")] for r in reports],
             "empty": "No issues were reported in this period.", "wide": True},
        ],
        "notes": ["Remake rate = remakes accepted ÷ cases delivered in the same period."],
    }


def _report_materials(period, **_):
    start = datetime(period["start"].year, period["start"].month, period["start"].day, tzinfo=LAB_TZ).isoformat()
    end = datetime(period["end"].year, period["end"].month, period["end"].day, tzinfo=LAB_TZ).isoformat()
    items = supabase_admin.table("inventory").select("*").order("item_name").execute().data or []
    logs = _fetch_all(lambda: supabase_admin.table("inventory_logs").select("inventory_id, action_type, quantity_changed, created_at")
                      .gte("created_at", start).lt("created_at", end).order("created_at"))
    totals = {}
    for l in logs:
        q = float(l.get("quantity_changed") or 0)
        t = totals.setdefault(l.get("inventory_id"), {"used": 0.0, "restocked": 0.0, "adjusted": 0.0, "uses": 0})
        action = l.get("action_type")
        if action in ("case_deduction", "auto_created_and_deducted", "disc_opened", "disc_use"):
            t["used"] += -q            # an opened disc counts as one whole disc used
            t["uses"] += action != "disc_opened"
        elif q > 0:
            t["restocked"] += q
        else:
            t["adjusted"] += q
    rows = []
    for it in items:
        t = totals.get(it["id"], {"used": 0.0, "restocked": 0.0, "adjusted": 0.0, "uses": 0})
        qty, reorder = float(it.get("quantity") or 0), float(it.get("minimum_threshold") or 0)
        status = "Out of stock" if qty <= 0 else ("Low" if qty <= reorder else "OK")
        rows.append([it.get("item_name"), (it.get("category") or "—").replace("_", " ").capitalize(), it.get("unit") or "unit",
                     t["uses"], f"{t['used']:g}", f"{t['restocked']:g}", f"{t['adjusted']:g}" if t["adjusted"] else "—",
                     f"{qty:g}", f"{reorder:g}", status])
    try:
        requests_ = [r for r in _fetch_all(lambda: supabase_admin.table("material_requests")
                                           .select("item_name, status, created_at, technician_id, case_id").order("created_at"))
                     if _in_period(_lab_day(r.get("created_at")), period)]
    except Exception:
        requests_ = []
    req_counts = {}
    for r in requests_:
        row = req_counts.setdefault(r.get("item_name") or "—", [0, 0, 0, 0])
        row[0] += 1
        row[{"fulfilled": 1, "dismissed": 2}.get(r.get("status"), 3)] += 1
    return {
        "sections": [
            {"title": "Materials", "columns": [("Material", False), ("Category", False), ("Unit", False), ("Uses", True),
                                                ("Used", True), ("Restocked", True), ("Adjusted", True), ("On hand now", True),
                                                ("Reorder at", True), ("Status", False)],
             "rows": rows, "empty": "No materials in inventory.", "wide": True},
            {"title": "Restock requests from technicians", "columns": [("Material", False), ("Requests", True),
                                                                        ("Restocked", True), ("Dismissed", True), ("Pending", True)],
             "rows": [[k] + v for k, v in sorted(req_counts.items(), key=lambda kv: -kv[1][0])],
             "empty": "No restock requests in this period."},
        ],
        "notes": ["Used, Restocked and Adjusted cover the selected period; On hand and Status are today's stock. "
                  "Adjusted is the net of stock-count corrections. For what to order next, see the Forecast Analytics report.",
                  "Zirconia discs count as used when they are opened (one whole disc each); Uses counts the cases milled "
                  "from them. Real units per disc are on the Inventory page under Zirconia discs."],
    }


# ---- Technician reports ----
def _report_my_production(period, profile, **_):
    cases = _report_cases(technician_id=profile["id"])
    done = [c for c in cases if _in_period(c["delivered"], period)]
    timed = [c for c in done if c["late_days"] is not None]
    on_time = sum(1 for c in timed if c["late_days"] == 0)
    by_type, by_material = {}, {}
    for c in done:
        by_type[c["type_label"]] = by_type.get(c["type_label"], 0) + 1
        for fam, n in c["material_units"].items():
            by_material[fam] = by_material.get(fam, 0) + n
    month_rows = []
    for m in _period_months(period, [c["delivered"] for c in done]):
        month = [c for c in done if _month_start(c["delivered"]) == m]
        t = [c for c in month if c["late_days"] is not None]
        month_rows.append([_fmt_month(m), len(month), sum(c["units"] for c in month), _pct(sum(1 for c in t if c["late_days"] == 0), len(t)),
                           _avg([c["turnaround"] for c in month if c["turnaround"] is not None])])
    return {
        "sections": [
            {"title": "Completed by month", "columns": [("Month", False), ("Cases", True), ("Units", True),
                                                         ("On-time rate", True), ("Avg. days", True)],
             "rows": month_rows, "empty": "No cases completed in this period.",
             "foot": ["Total", len(done), sum(c["units"] for c in done), _pct(on_time, len(timed)),
                      _avg([c["turnaround"] for c in done if c["turnaround"] is not None])] if month_rows else None},
            _count_table("By prosthesis type", "Prosthesis type", by_type, unit="Cases"),
            _count_table("By material", "Material", by_material, unit="Units"),
            {"title": "Completed cases", "columns": [("Case", False), ("Clinic", False), ("Type", False), ("Units", True),
                                                      ("Material / shade", False), ("Received", False), ("Due", False),
                                                      ("Delivered", False), ("Days late", True)],
             "rows": [[c["case_number"] + (" (remake)" if c.get("remake_of") else ""), c["clinic"], c["type_label"], c["units"],
                       c["material_text"], _fmt_day(c["received"]), _fmt_day(c["due"]), _fmt_day(c["delivered"]),
                       c["late_days"] if c["late_days"] is not None else "—"] for c in sorted(done, key=lambda c: c["delivered"])],
             "empty": "No cases completed in this period.", "wide": True},
        ],
        "notes": ["Cases count in the month the clinic received them."],
    }


def _report_my_workload(period, profile, **_):
    today = datetime.now(LAB_TZ).date()
    cases = [c for c in _report_cases(technician_id=profile["id"]) if c.get("status") in BENCH_STATUSES]
    cases.sort(key=lambda c: (not c.get("is_rush"), c["due"] or today.replace(year=today.year + 50), c["case_number"]))
    stage = dict(BENCH_FLOW)
    rows = []
    for c in cases:
        flags = [f for f, on in (("RUSH", c.get("is_rush")), ("Remake", c.get("remake_of"))) if on]
        if c["due"] and c["due"] < today:
            flags.append(f"Overdue {(today - c['due']).days}d")
        rows.append(["☐", c["case_number"], _fmt_day(c["due"]), c["clinic"], c.get("patient_name") or "—", c["type_label"],
                     c["material_text"], stage.get(c.get("status"), c["status_label"]), ", ".join(flags) or "—",
                     c.get("instructions") or "—"])
    return {
        "sections": [
            {"title": "Open cases (rush first, then by due date)",
             "columns": [("Done", False), ("Case", False), ("Due", False), ("Clinic", False), ("Patient", False), ("Type", False),
                         ("Teeth: material / shade", False), ("Stage", False), ("Flags", False), ("Instructions", False)],
             "rows": rows, "empty": "No open cases on your bench.", "wide": True},
        ],
        "notes": [f"Snapshot of your bench on {_fmt_day(today)}. Cases waiting for admin QC or dispatch are not listed."],
    }


# ---- Dentist reports ----
def _report_my_cases(period, profile, patient="", **_):
    cases = [c for c in _report_cases(dentist_id=profile["id"]) if _in_period(c["received"], period)]
    if patient:
        needle = " ".join(patient.split()).casefold()
        cases = [c for c in cases if needle in " ".join((c.get("patient_name") or "").split()).casefold()]
    active = [c for c in cases if c.get("status") != "cancelled"]
    month_rows = []
    for m in _period_months(period, [c["received"] for c in cases]):
        month = [c for c in active if _month_start(c["received"]) == m]
        month_rows.append([_fmt_month(m), len(month), sum(c["units"] for c in month),
                           sum(1 for c in month if c["delivered"]), sum(1 for c in month if c.get("remake_of"))])
    return {
        "sections": [
            {"title": "By month", "columns": [("Month", False), ("Job orders", True), ("Units", True), ("Delivered", True), ("Remakes", True)],
             "rows": month_rows, "empty": "No job orders in this period.",
             "foot": ["Total"] + [sum(r[i] for r in month_rows) for i in range(1, 5)] if month_rows else None},
            {"title": "Job orders", "columns": [("Case", False), ("Sent", False), ("Patient", False), ("Type", False),
                                                 ("Teeth: material / shade", False), ("Units", True), ("Status", False),
                                                 ("Due", False), ("Delivered", False)],
             "rows": [[c["case_number"] + (" (remake)" if c.get("remake_of") else ""), _fmt_day(c["received"]),
                       c.get("patient_name") or "—", c["type_label"], c["material_text"], c["units"], c["status_label"],
                       _fmt_day(c["due"]), _fmt_day(c["delivered"])] for c in cases],
             "empty": "No job orders match." if patient else "No job orders in this period.", "wide": True},
        ],
        "notes": ["Drafts that were never sent are not included."],
    }


def _report_lab_service(period, profile, **_):
    cases = _report_cases(dentist_id=profile["id"])
    by_id = {c["id"]: c for c in cases}
    delivered = [c for c in cases if _in_period(c["delivered"], period)]
    reports = [r for r in _report_issue_reports() if r.get("case_id") in by_id and _in_period(_lab_day(r.get("created_at")), period)]
    decision = {"remake_accepted": "Remake accepted", "no_action": "No remake needed", "open": "Under review"}
    return {
        "sections": [
            _delivery_by_month(period, delivered),
            {"title": "Issues you reported", "columns": [("Reported", False), ("Case", False), ("Patient", False), ("Problem", False),
                                                          ("Lab's decision", False), ("Remake", False)],
             "rows": [[_fmt_day(_lab_day(r.get("created_at"))), by_id[r["case_id"]]["case_number"],
                       by_id[r["case_id"]].get("patient_name") or "—", ISSUE_TYPES.get(r.get("issue_type"), "Other problem"),
                       decision.get(r.get("status"), "—") + (f" ({r['admin_note']})" if r.get("admin_note") else ""),
                       by_id.get(r.get("remake_case_id"), {}).get("case_number", "—")] for r in reports],
             "empty": "You didn't report any issues in this period.", "wide": True},
        ],
        "notes": ["Turnaround is counted from the day you sent the job order to the day your clinic confirmed receipt."],
    }


REPORT_BUILDERS = {
    "job_orders": _report_job_orders, "turnaround": _report_turnaround, "remakes": _report_remakes,
    "materials": _report_materials, "my_production": _report_my_production, "my_workload": _report_my_workload,
    "my_cases": _report_my_cases, "lab_service": _report_lab_service,
}


REPORT_CELL_CHARS = 140  # longer text is shortened on the printed page (the Excel file keeps it all)


def _clip_cell(cell):
    if isinstance(cell, str) and len(cell) > REPORT_CELL_CHARS:
        return cell[:REPORT_CELL_CHARS - 1].rstrip() + "…"
    return cell


REPORT_PAGE_SIZES = (15, 20, 25, 30, 40)
REPORT_DEFAULT_PAGE_SIZE = 25


def _paginate_report(report, limit):
    """Split a report's tables into printed pages of about `limit` table rows each.

    A table that doesn't fit continues on the next page ("continued", headings repeated);
    its total row appears only after its last row. Rows of wide detail lists count 1.5,
    since their text wraps. Each section heading costs 3 rows; the notes and sign-off on
    the last page cost 6. Returns a list of pages, each a list of section chunks.
    """
    pages, page, used = [], [], 0.0

    def turn_page():
        nonlocal page, used
        pages.append(page)
        page, used = [], 0.0

    for s in report["sections"]:
        weight = 1.5 if s.get("wide") else 1.0
        rows = s["rows"]
        if not rows:
            if page and used + 3 > limit:
                turn_page()
            page.append(dict(s, continued=False, show_foot=False))
            used += 3
            continue
        start = 0
        while start < len(rows):
            room = int((limit - used - 3) // weight)
            left = len(rows) - start
            # Don't start a table with just a row or two at the bottom of a page.
            if page and room < min(3, left):
                turn_page()
                continue
            chunk = rows[start:start + max(room, 1)]
            end = start + len(chunk)
            last = end >= len(rows)
            chunk = [[_clip_cell(cell) for cell in row] for row in chunk]
            page.append(dict(s, rows=chunk, continued=start > 0, show_foot=last and bool(s.get("foot")),
                             first_row=start + 1, total_rows=len(rows)))
            used += 3 + len(chunk) * weight + (1 if last and s.get("foot") else 0)
            start = end
            if not last:
                turn_page()
    if page and used + 6 > limit:
        turn_page()
    pages.append(page)
    return pages


def _excel_value(cell):
    """Numbers and percentages as real Excel numbers (so they sum and sort); text unchanged.
    Returns (value, number_format or None)."""
    import re
    if isinstance(cell, bool) or cell is None:
        return cell, None
    if isinstance(cell, int):
        return cell, "#,##0"
    if isinstance(cell, float):
        return cell, "#,##0.0"
    text = str(cell)
    if re.fullmatch(r"-?\d+(\.\d+)?%", text):
        return float(text[:-1]) / 100, "0.0%" if "." in text else "0%"
    if re.fullmatch(r"-?\d+", text):
        return int(text), "#,##0"
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text), "#,##0.0"
    return text, None


def _report_xlsx(title, period_label, report, profile, generated_at):
    """The report as a formatted Excel workbook: one worksheet per table, each with the
    report heading, a styled header row (repeated when printed, filterable, frozen),
    borders, real numbers, a bold total row, fitted column widths and the report notes."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    dark, accent, muted = "0F172A", "0284C7", "64748B"
    thin = Side(style="thin", color="CBD5E1")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_fill = PatternFill("solid", fgColor=accent)
    total_fill = PatternFill("solid", fgColor="F1F5F9")
    stripe_fill = PatternFill("solid", fgColor="F8FAFC")
    prepared = f"Prepared for {profile.get('display_name') or profile.get('first_name') or 'user'} · generated {generated_at:%b %d, %Y %I:%M %p}"

    wb = Workbook()
    wb.remove(wb.active)
    used_names = set()
    for section in report["sections"]:
        name = "".join(ch for ch in section["title"] if ch not in '[]:*?/\\')[:31] or "Sheet"
        base, n = name, 2
        while name.lower() in used_names:
            name = f"{base[:28]} {n}"
            n += 1
        used_names.add(name.lower())
        ws = wb.create_sheet(name)
        ncols = len(section["columns"])
        last_col = get_column_letter(ncols)

        ws["A1"] = title
        ws["A1"].font = Font(size=14, bold=True, color=dark)
        ws["A2"] = f"Mrkzter Dental Laboratory · DentaLink LIS · {period_label}"
        ws["A2"].font = Font(size=10, color=muted)
        ws["A3"] = prepared
        ws["A3"].font = Font(size=9, italic=True, color=muted)
        ws["A5"] = section["title"]
        ws["A5"].font = Font(size=12, bold=True, color=dark)

        head_row = 6
        for col, (label, is_num) in enumerate(section["columns"], start=1):
            c = ws.cell(row=head_row, column=col, value=label)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = header_fill
            c.border = border
            c.alignment = Alignment(horizontal="right" if is_num else "left", vertical="center", wrap_text=True)
        ws.row_dimensions[head_row].height = 20

        r = head_row
        for i, row in enumerate(section["rows"]):
            r += 1
            for col, cell in enumerate(row, start=1):
                value, fmt = _excel_value(cell)
                c = ws.cell(row=r, column=col, value=value)
                c.border = border
                is_num = section["columns"][col - 1][1]
                c.alignment = Alignment(horizontal="right" if is_num else "left", vertical="top", wrap_text=not is_num)
                if fmt and is_num:
                    c.number_format = fmt
                if i % 2:
                    c.fill = stripe_fill
        if not section["rows"]:
            r += 1
            ws.cell(row=r, column=1, value=section.get("empty") or "No data.").font = Font(italic=True, color=muted)
        elif section.get("foot"):
            r += 1
            for col, cell in enumerate(section["foot"], start=1):
                value, fmt = _excel_value(cell)
                c = ws.cell(row=r, column=col, value=value)
                c.font = Font(bold=True, color=dark)
                c.fill = total_fill
                c.border = Border(left=thin, right=thin, bottom=thin, top=Side(style="medium", color=dark))
                is_num = section["columns"][col - 1][1]
                c.alignment = Alignment(horizontal="right" if is_num else "left")
                if fmt and is_num:
                    c.number_format = fmt

        if report.get("notes"):
            r += 2
            for note in report["notes"]:
                c = ws.cell(row=r, column=1, value=note)
                c.font = Font(size=9, italic=True, color=muted)
                c.alignment = Alignment(wrap_text=True, vertical="top")
                ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=max(ncols, 4))
                ws.row_dimensions[r].height = 30
                r += 1

        # Column widths from the longest value in each column (title rows excluded).
        for col in range(1, ncols + 1):
            values = [section["columns"][col - 1][0]] + [row[col - 1] for row in section["rows"]] + list(section.get("foot") or [])[col - 1:col]
            longest = max((len(str(v)) for v in values if v is not None), default=8)
            ws.column_dimensions[get_column_letter(col)].width = min(max(longest + 2, 10), 50)

        if section["rows"]:
            ws.auto_filter.ref = f"A{head_row}:{last_col}{head_row + len(section['rows'])}"
        ws.freeze_panes = ws.cell(row=head_row + 1, column=1)
        ws.print_title_rows = f"{head_row}:{head_row}"
        ws.page_setup.orientation = "landscape" if ncols > 6 else "portrait"
        ws.page_setup.paperSize = ws.PAPERSIZE_A4
        ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.oddFooter.center.text = "Page &P of &N"
        ws.sheet_view.showGridLines = False

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


@app.route("/reports")
@role_required("admin", "technician", "dentist")
def reports():
    """Printable reports; each role sees only its own reports and its own data."""
    role = g.profile.get("role")
    available = REPORTS[role]
    keys = [key for key, _, _ in available]
    key = request.args.get("report") if request.args.get("report") in keys else keys[0]
    title, description = next((t, d) for k, t, d in available if k == key)
    period = _report_period(request.args.get("period", "month"), request.args.get("ref"))
    patient = " ".join(request.args.get("patient", "").split())[:120] if key == "my_cases" else ""
    try:
        report = REPORT_BUILDERS[key](period, profile=g.profile, patient=patient)
    except Exception:
        app.logger.exception("Unable to build report %s", key)
        return "Unable to build this report. Check the server logs for details.", 500

    period_label = "Current snapshot" if key in REPORTS_WITHOUT_PERIOD else period["label"]
    if patient:
        period_label += f" · Patient: {patient}"
    if request.args.get("format") == "xlsx":
        response = make_response(_report_xlsx(title, period_label, report, g.profile, datetime.now(LAB_TZ)))
        response.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        response.headers["Content-Disposition"] = (
            f"attachment; filename=DentaLink_{title.replace(' & ', '_').replace(' ', '_')}_{period['ref'] or 'all_time'}.xlsx")
        return response

    patient_names = []
    if key == "my_cases":
        rows = (supabase_admin.table("dental_cases").select("patient_name").eq("dentist_id", g.profile["id"])
                .neq("status", "draft").execute().data or [])
        patient_names = sorted({" ".join(r["patient_name"].split()) for r in rows if r.get("patient_name")}, key=str.casefold)
    back = {"admin": url_for("admin_dashboard"), "technician": url_for("technician_dashboard"),
            "dentist": url_for("dentist_dashboard")}[role]
    try:
        page_size = int(request.args.get("rows", REPORT_DEFAULT_PAGE_SIZE))
    except ValueError:
        page_size = REPORT_DEFAULT_PAGE_SIZE
    if page_size not in REPORT_PAGE_SIZES:
        page_size = REPORT_DEFAULT_PAGE_SIZE
    # "Clear filters" shows only when something differs from the defaults (this month, all patients, 25 rows).
    default_period = _report_period("month", None)
    filters_active = (
        (key not in REPORTS_WITHOUT_PERIOD and (period["kind"], period["ref"]) != (default_period["kind"], default_period["ref"]))
        or bool(patient)
        or page_size != REPORT_DEFAULT_PAGE_SIZE
    )
    return render_template(
        "reports.html",
        profile=g.profile,
        role=role,
        available=available,
        key=key,
        title=title,
        description=description,
        period=period,
        period_label=period_label,
        uses_period=key not in REPORTS_WITHOUT_PERIOD,
        period_options=_period_options(),
        patient=patient,
        patient_names=patient_names,
        report=report,
        pages=_paginate_report(report, page_size),
        page_size=page_size,
        page_sizes=REPORT_PAGE_SIZES,
        filters_active=filters_active,
        back=back,
        generated_at=datetime.now(LAB_TZ),
    )


@app.route("/job-orders/<case_id>/print")
@role_required("admin", "technician", "dentist")
def print_job_order(case_id):
    """One-page printable job order slip (prescription, teeth, instructions, sign-off lines)."""
    try:
        cases = supabase_admin.table("dental_cases").select("*").eq("id", case_id).execute().data or []
        if not cases or not _can_view_case(g.profile, case_id):
            return "This job order isn't available to you.", 404
        case = cases[0]
        ctx = _job_order_context(case, include_internal=g.profile.get("role") != "dentist")
        dentist = {}
        if case.get("dentist_id"):
            rows = (supabase_admin.table("profiles").select("display_name, first_name, last_name, clinic_name, phone, professional_id")
                    .eq("id", case["dentist_id"]).execute().data or [])
            dentist = rows[0] if rows else {}
        return render_template(
            "job_order_print.html",
            profile=g.profile,
            case=case,
            dentist=dentist,
            status_label=REPORT_STATUS_LABELS.get(case.get("status"), (case.get("status") or "").replace("_", " ").capitalize()),
            generated_at=datetime.now(LAB_TZ),
            **ctx,
        )
    except Exception:
        app.logger.exception("Unable to print job order")
        return "Unable to load this job order. Check the server logs for details.", 500


# =========================
# ADMIN: ANALYTICS (on-screen charts; DentaLink cases only, paper job orders not counted)
# =========================
QUIET_CLINIC_DAYS = 60


def _analytics(month):
    """Everything the Analytics page shows for `month` (a first-of-month date): 12-month trends
    ending there, the month's KPIs against the month before, the clinic ranking, new partner
    clinics and clinics that have gone quiet. A clinic is the dentists sharing a clinic name
    (see _clinic_key); it joins when its first dentist account is approved."""
    today = datetime.now(LAB_TZ).date()
    months = [month]
    for _ in range(11):
        months.insert(0, (months[0] - timedelta(days=1)).replace(day=1))
    prev = months[-2]
    month_end = _next_month(month)
    ref_day = min(today, month_end - timedelta(days=1))   # "now" for this month

    cases = [c for c in _report_cases() if c.get("status") != "cancelled" and c["received"]]
    by_id = {c["id"]: c for c in cases}
    reports = _report_issue_reports()
    remakes = [r for r in reports if r.get("status") == "remake_accepted" and r.get("case_id") in by_id]

    dentists = (supabase_admin.table("profiles")
                .select("id, display_name, first_name, last_name, clinic_name, professional_id, approved_at, created_at")
                .eq("role", "dentist").eq("status", "approved").execute().data or [])
    clinics = {}
    for d in dentists:
        key, label = _clinic_of(d, d["id"])
        joined = _lab_day(d.get("approved_at") or d.get("created_at"))
        cl = clinics.setdefault(key, {"key": key, "label": label, "dentists": [], "joined": joined})
        cl["dentists"].append(d)
        if joined and (cl["joined"] is None or joined < cl["joined"]):
            cl["joined"] = joined
    for c in cases:   # a clinic whose dentist is no longer approved still has its orders
        clinics.setdefault(c["clinic_key"], {"key": c["clinic_key"], "label": c["clinic"], "dentists": [], "joined": None})

    def in_month(day, m):
        return day is not None and m <= day < _next_month(m)

    def on_time(delivered):
        timed = [c for c in delivered if c["late_days"] is not None]
        return round(100 * sum(1 for c in timed if c["late_days"] == 0) / len(timed)) if timed else None

    trend = []
    for m in months:
        received = [c for c in cases if in_month(c["received"], m)]
        delivered = [c for c in cases if in_month(c["delivered"], m)]
        remade = sum(1 for r in remakes if in_month(_lab_day(r.get("created_at")), m))
        trend.append({
            "month": f"{m:%Y-%m}", "label": f"{MONTH_NAMES[m.month - 1][:3]} {m.year}",
            "orders": len(received), "units": sum(c["units"] for c in received),
            "active_clinics": len({c["clinic_key"] for c in received}),
            "new_clinics": sum(1 for cl in clinics.values() if in_month(cl["joined"], m)),
            "delivered": len(delivered), "on_time": on_time(delivered),
            "remakes": remade, "remake_rate": round(100 * remade / len(delivered)) if delivered else None,
        })
    now, before = trend[-1], trend[-2]

    month_cases = [c for c in cases if in_month(c["received"], month)]
    prev_counts = {}
    for c in cases:
        if in_month(c["received"], prev):
            prev_counts[c["clinic_key"]] = prev_counts.get(c["clinic_key"], 0) + 1
    ranking = []
    for key in {c["clinic_key"] for c in month_cases}:
        mine = [c for c in month_cases if c["clinic_key"] == key]
        delivered = [c for c in cases if c["clinic_key"] == key and in_month(c["delivered"], month)]
        last = max(c["received"] for c in cases if c["clinic_key"] == key and c["received"] < month_end)
        ranking.append({
            "label": clinics[key]["label"], "orders": len(mine), "units": sum(c["units"] for c in mine),
            "share": round(100 * len(mine) / len(month_cases)) if month_cases else 0,
            "change": len(mine) - prev_counts.get(key, 0), "last_order": last,
            "dentists": len(clinics[key]["dentists"]) or 1, "on_time": on_time(delivered),
            "remakes": sum(1 for r in remakes if in_month(_lab_day(r.get("created_at")), month)
                           and by_id[r["case_id"]]["clinic_key"] == key),
        })
    ranking.sort(key=lambda r: (-r["orders"], -r["units"], r["label"].lower()))

    new_partners = []
    for cl in clinics.values():
        if in_month(cl["joined"], month):
            mine = sorted((c for c in cases if c["clinic_key"] == cl["key"]), key=lambda c: c["received"])
            new_partners.append({
                "label": cl["label"], "joined": cl["joined"],
                "dentists": ", ".join(_display_name(d, "Dentist") for d in cl["dentists"]),
                "licenses": ", ".join(d.get("professional_id") or "—" for d in cl["dentists"]),
                "first_order": mine[0]["received"] if mine else None, "orders": len(mine),
            })
    new_partners.sort(key=lambda p: p["joined"])

    quiet = []
    for key in {c["clinic_key"] for c in cases}:
        past = [c for c in cases if c["clinic_key"] == key and c["received"] <= ref_day]
        if not past:
            continue
        last = max(c["received"] for c in past)
        idle = (ref_day - last).days
        if idle >= QUIET_CLINIC_DAYS:
            quiet.append({"label": clinics[key]["label"], "last_order": last, "days": idle, "orders": len(past)})
    quiet.sort(key=lambda q: -q["days"])

    mix = {}
    for c in month_cases:
        mix[c["type_label"]] = mix.get(c["type_label"], 0) + 1
    return {
        "trend": trend, "now": now, "before": before,
        "ranking": ranking, "new_partners": new_partners, "quiet": quiet,
        "mix": sorted(mix.items(), key=lambda kv: (-kv[1], kv[0])),
        "month_label": f"{MONTH_NAMES[month.month - 1]} {month.year}",
        "prev_label": f"{MONTH_NAMES[prev.month - 1]} {prev.year}",
        "ref_day": ref_day,
    }


@app.route("/admin/analytics")
@role_required("admin")
def admin_analytics():
    today = datetime.now(LAB_TZ).date()
    try:
        y, m = (int(x) for x in request.args.get("month", "").split("-"))
        month = today.replace(year=y, month=m, day=1)
        if month > today:
            raise ValueError
    except (ValueError, TypeError):
        month = today.replace(day=1)
    try:
        data = _analytics(month)
    except Exception:
        app.logger.exception("Unable to build analytics")
        return "Unable to load analytics. Check the server logs for details.", 500
    options, d = [], today.replace(day=1)
    for _ in range(24):
        options.append((f"{d:%Y-%m}", f"{MONTH_NAMES[d.month - 1]} {d.year}"))
        d = (d - timedelta(days=1)).replace(day=1)
    return render_template("admin_analytics.html", profile=g.profile, a=data, month=f"{month:%Y-%m}",
                           month_options=options, quiet_days=QUIET_CLINIC_DAYS, fmt_day=_fmt_day)


# Scheduled backups: checks every minute in the background (see run_due_backup).
start_backup_scheduler()


# =========================
# START FLASK
# =========================

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))