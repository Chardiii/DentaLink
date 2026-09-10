import os

from flask import Flask, render_template, request, redirect, url_for, session, flash
from supabase import create_client, Client
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
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
            .order("created_at", desc=True)
            .execute()
        )
        restock_requests = restock_res.data or []

        # ==========================================
        # 5. LAB PERFORMANCE METRICS
        # ==========================================
        now_str = datetime.now().strftime("%Y-%m-%d")

        completed_res = supabase_admin.table("dental_cases").select("id", count="exact").eq("status", "completed").execute()
        completed_this_month = completed_res.count or 0

        overdue_res = (
            supabase_admin
            .table("dental_cases")
            .select("id", count="exact")
            .lt("due_date", now_str)
            .neq("status", "completed")
            .neq("status", "cancelled")
            .execute()
        )
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
        active_cases_res = (
            supabase_admin
            .table("dental_cases")
            .select("*")
            .neq("status", "draft")
            .neq("status", "submitted")
            .order("created_at", desc=True)
            .execute()
        )
        active_cases = active_cases_res.data or []
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

        # Stage slices for audio triggers and quick template bindings
        milling_cases = [c for c in active_cases if c.get("status") == "milling_or_printing"]
        qc_cases = [c for c in active_cases if c.get("status") == "finishing_and_quality_check"]

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
            completed_this_month=completed_this_month,
            overdue_count=overdue_count,
            unassigned_cases=unassigned_cases,
            technicians=technicians,
            active_cases=active_cases,
            milling_cases=milling_cases,
            qc_cases=qc_cases,
            notifications=notifications,
            online_count=online_count,
            restock_requests=restock_requests
        )

    except Exception as e:
        return f"Admin dashboard error: {str(e)}", 500
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
        profile_res = supabase_admin.table("profiles").select("role, display_name").eq("id", user_id).execute()
        if not profile_res.data or profile_res.data[0]["role"] != "technician":
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
            "completed"
        ]
        
        if new_status not in allowed_statuses:
            return "Invalid status selection.", 400

        # Fetch Case info
        case_res = supabase_admin.table("dental_cases").select("dentist_id, case_number").eq("id", case_id).eq("technician_id", user_id).execute()
        if not case_res.data:
            return "Case not found or not assigned to you.", 404

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
            "completed": "Completed"
        }
        readable_status = status_labels.get(new_status, new_status)

        # Update status in database
        supabase_admin.table("dental_cases").update({
            "status": new_status
        }).eq("id", case_id).execute()

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
        case_res = supabase_admin.table("dental_cases").select("case_number").eq("id", case_id).execute()
        case_number = case_res.data[0]["case_number"] if case_res.data else "Unknown"

        tech_res = supabase_admin.table("profiles").select("display_name").eq("id", new_tech_id).execute()
        tech_name = tech_res.data[0]["display_name"] if tech_res.data else "New Technician"

        # UPDATE ONLY THE TECHNICIAN_ID (Status is intentionally preserved)
        supabase_admin.table("dental_cases").update({
            "technician_id": new_tech_id
        }).eq("id", case_id).execute()

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


# =========================
# TECHNICIAN: COMPLETE CASE
# =========================
@app.route("/technician/cases/<case_id>/complete", methods=["POST"])
def complete_case(case_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    try:
        profile_res = supabase_admin.table("profiles").select("role, display_name").eq("id", user_id).execute()
        if not profile_res.data or profile_res.data[0]["role"] != "technician":
            return "Access denied.", 403

        case_res = supabase_admin.table("dental_cases").select("dentist_id, case_number").eq("id", case_id).eq("technician_id", user_id).execute()
        if not case_res.data:
            return "Case not found or not assigned to you.", 404

        dentist_id = case_res.data[0]["dentist_id"]
        case_number = case_res.data[0]["case_number"]

        # Mark case as fully completed
        supabase_admin.table("dental_cases").update({
            "status": "completed"
        }).eq("id", case_id).execute()

        # Notify Dentist
        create_notification(
            user_id=dentist_id,
            message=f"Case Completed: Case {case_number} has been finished by the lab.",
            link="/dentist"
        )

        return redirect(url_for("technician_dashboard"))

    except Exception as e:
        return f"Complete case error: {str(e)}", 500
# =========================
# ADMIN: FINALIZE & COMPLETE CASE
# =========================
@app.route("/admin/cases/<case_id>/finalize", methods=["POST"])
def admin_finalize_case(case_id):
    admin_id = session.get("user_id")
    if not admin_id:
        return redirect(url_for("login"))

    try:
        admin_res = supabase_admin.table("profiles").select("role").eq("id", admin_id).execute()
        if not admin_res.data or admin_res.data[0]["role"] != "admin":
            return "Access denied.", 403

        # Fetch case data for notification
        case_res = supabase_admin.table("dental_cases").select("dentist_id, case_number").eq("id", case_id).execute()
        if not case_res.data:
            return "Case not found.", 404

        dentist_id = case_res.data[0]["dentist_id"]
        case_number = case_res.data[0]["case_number"]

        # Mark case as completed
        supabase_admin.table("dental_cases").update({
            "status": "completed"
        }).eq("id", case_id).execute()

        # Notify Dentist that case is fully complete
        create_notification(
            user_id=dentist_id,
            message=f"Case Completed: Case {case_number} has passed admin inspection and is fully completed.",
            link="/dentist"
        )

        return redirect(url_for("admin_dashboard"))

    except Exception as e:
        return f"Finalize case error: {str(e)}", 500

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
        from datetime import datetime
        now_iso = datetime.utcnow().isoformat()
        try:
            supabase_admin.table("profiles").update({"last_seen": now_iso}).eq("id", user_id).execute()
        except Exception:
            pass

        # =========================
        # 4. GET DENTIST CASES (WITH TECH DISPLAY NAME)
        # =========================
        cases_response = (
            supabase_admin
            .table("dental_cases")
            .select("*, profiles!dental_cases_technician_id_fkey(display_name)")
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
        qc_cases = [c for c in dental_cases if c.get("status") == "finishing_and_quality_check"]
        completed_cases = [c for c in dental_cases if c.get("status") == "completed"]
        draft_cases = [c for c in dental_cases if c.get("status") == "draft"]
        ready_cases = [c for c in dental_cases if c.get("status") in ["ready_for_delivery", "delivered", "completed"]]

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
            ready_cases=ready_cases,
            completed_cases=completed_cases,
            draft_cases=draft_cases,
            notifications=notifications
        )

    except Exception as e:
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

            # Generate Case Number
            existing_cases_response = supabase_admin.table("dental_cases").select("id").execute()
            existing_cases = existing_cases_response.data or []
            case_number = f"DL-{len(existing_cases) + 1:05d}"

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
        email = request.form["email"].strip().lower()
        password = request.form["password"]

        try:
            # =========================
            # SUPABASE LOGIN
            # =========================
            response = supabase.auth.sign_in_with_password({
                "email": email,
                "password": password
            })

            if not response.user:
                flash("Login failed. Please check your credentials.", "error")
                return render_template("login.html")

            user_id = response.user.id
            session["user_id"] = user_id

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
                session.clear()
                flash("User profile not found in the system.", "error")
                return render_template("login.html")

            profile = profiles[0]

            # =========================
            # CHECK ACCOUNT STATUS
            # =========================
            if profile["status"] == "pending":
                return render_template("account_pending.html")

            if profile["status"] == "rejected":
                flash("Your account application was rejected.", "error")
                return render_template("login.html")

            if profile["status"] != "approved":
                flash("Your account is not yet approved by the administrator.", "error")
                return render_template("login.html")

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
            if profile["role"] == "admin":
                return redirect(url_for("admin_dashboard"))

            if profile["role"] == "dentist":
                return redirect(url_for("dentist_dashboard"))

            if profile["role"] == "technician":
                return redirect(url_for("technician_dashboard"))

            session.clear()
            flash("Invalid account role assigned.", "error")
            return render_template("login.html")

        except Exception as e:
            # Catch wrong password, invalid email format, or connection drops from Supabase
            error_message = str(e)
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
            "item_name": item_name
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
        completed_cases = [c for c in assigned_cases if c.get("status") in ["ready_for_delivery", "completed"]]

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
        profile_res = supabase_admin.table("profiles").select("role").eq("id", user_id).execute()
        if not profile_res.data or profile_res.data[0].get("role") != "technician":
            return "Access denied.", 403

        # Get the technician's revision notes from the form
        revision_notes = request.form.get("revision_notes", "").strip()
        if not revision_notes:
            return "You must provide a reason for sending the case back.", 400

        # Fetch case to get the dentist_id for the notification
        case_res = supabase_admin.table("dental_cases").select("dentist_id, case_number").eq("id", case_id).execute()
        if not case_res.data:
            return "Case not found.", 404
            
        dentist_id = case_res.data[0]["dentist_id"]
        case_number = case_res.data[0]["case_number"]

        # Update the case status
        supabase_admin.table("dental_cases").update({
            "status": "needs_revision",
            "revision_notes": revision_notes
        }).eq("id", case_id).execute()

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
# TECHNICIAN: SUBMIT FOR ADMIN REVIEW
# =========================
@app.route("/technician/cases/<case_id>/submit-review", methods=["POST"])
def submit_for_admin_review(case_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    try:
        # Verify Technician
        profile_res = supabase_admin.table("profiles").select("role, display_name").eq("id", user_id).execute()
        if not profile_res.data or profile_res.data[0]["role"] != "technician":
            return "Access denied.", 403

        tech_name = profile_res.data[0]["display_name"]

        # Fetch Case info
        case_res = supabase_admin.table("dental_cases").select("case_number").eq("id", case_id).eq("technician_id", user_id).execute()
        if not case_res.data:
            return "Case not found or not assigned to you.", 404
            
        case_number = case_res.data[0]["case_number"]

        # Update status to pending admin review
        supabase_admin.table("dental_cases").update({
            "status": "pending_admin_review"
        }).eq("id", case_id).execute()

        # Find all approved admins to notify them for double checking
        admins_res = supabase_admin.table("profiles").select("id").eq("role", "admin").eq("status", "approved").execute()
        admins = admins_res.data or []

        for admin in admins:
            create_notification(
                user_id=admin["id"],
                message=f"Quality Check Required: Technician {tech_name} has finished Case {case_number}. Please double-check and finalize.",
                link="/admin"
            )

        return redirect(url_for("technician_dashboard"))

    except Exception as e:
        return f"Submit review error: {str(e)}", 500
    
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
        admin_res = supabase_admin.table("profiles").select("role").eq("id", admin_id).execute()
        if not admin_res.data or admin_res.data[0]["role"] != "admin":
            return "Access denied.", 403

        technician_id = request.form.get("technician_id")
        if not technician_id:
            return "Technician selection is required.", 400

        # Fetch Case info for notification
        case_res = supabase_admin.table("dental_cases").select("case_number").eq("id", case_id).execute()
        if not case_res.data:
            return "Case not found.", 404
        case_number = case_res.data[0]["case_number"]

        # Update case status to assigned and set technician_id
        supabase_admin.table("dental_cases").update({
            "technician_id": technician_id,
            "status": "assigned"
        }).eq("id", case_id).execute()

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
# START FLASK
# =========================

if __name__ == "__main__":
    app.run(debug=True)