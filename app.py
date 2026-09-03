import os

from flask import Flask, render_template, request, redirect, url_for, session
from supabase import create_client, Client
from dotenv import load_dotenv
from werkzeug.utils import secure_filename

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
        response = supabase_admin.table("profiles").select("*").eq("id", user_id).execute()
        profiles = response.data or []
        if not profiles:
            session.clear()
            return "Profile not found.", 404

        profile = profiles[0]

        if profile["role"] != "admin" or profile["status"] != "approved":
            return "Access denied.", 403

        # Get all users
        all_response = supabase_admin.table("profiles").select("*").order("created_at", desc=True).execute()
        all_users = all_response.data or []

        pending_users = [u for u in all_users if u["status"] == "pending"]
        approved_users = [u for u in all_users if u["status"] == "approved"]
        rejected_users = [u for u in all_users if u["status"] == "rejected"]

        # 1. Unassigned Cases, Teeth, and Files for Full Review & Assignment
        unassigned_res = supabase_admin.table("dental_cases").select("*").eq("status", "submitted").execute()
        unassigned_cases = unassigned_res.data or []

        for case in unassigned_cases:
            teeth_res = supabase_admin.table("case_teeth").select("*").eq("case_id", case["id"]).execute()
            case["teeth"] = teeth_res.data or []

            files_res = supabase_admin.table("case_files").select("*").eq("case_id", case["id"]).execute()
            files = files_res.data or []
            
            for f in files:
                try:
                    signed_url_res = supabase_admin.storage.from_("dental-case-files").create_signed_url(f["file_path"], 3600)
                    f["download_url"] = signed_url_res.get("signedURL") or signed_url_res.get("signed_url")
                except Exception:
                    f["download_url"] = "#"
            case["files"] = files

        # 2. Approved Technicians & Active Workloads
        techs_res = supabase_admin.table("profiles").select("id, display_name, first_name").eq("role", "technician").eq("status", "approved").execute()
        technicians = techs_res.data or []

        for tech in technicians:
            workload_res = (
                supabase_admin
                .table("dental_cases")
                .select("id", count="exact")
                .eq("technician_id", tech["id"])
                .in_("status", ["assigned", "in_progress", "cad_designing", "wax_up_or_try_in", "milling_or_printing", "packing_and_curing", "finishing_and_quality_check", "needs_revision"])
                .execute()
            )
            tech["active_workload"] = workload_res.count or 0

        # 3. GET ALL ACTIVE CASES (To monitor live lab statuses & assigned techs)
        active_cases_res = (
            supabase_admin
            .table("dental_cases")
            .select("*, profiles!dental_cases_technician_id_fkey(display_name)")
            .neq("status", "draft")
            .neq("status", "submitted")
            .order("created_at", desc=True)
            .execute()
        )
        active_cases = active_cases_res.data or []

        # Notifications
        notif_response = supabase_admin.table("notifications").select("*").eq("user_id", user_id).order("created_at", desc=True).limit(5).execute()
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
            unassigned_cases=unassigned_cases,
            technicians=technicians,
            active_cases=active_cases,
            notifications=notifications
        )

    except Exception as e:
        return f"Admin dashboard error: {str(e)}", 500

# =========================
# TECHNICIAN: UPDATE CASE STAGE
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
        return f"Status update error: {str(e)}", 500

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

    # =========================
    # CHECK LOGIN
    # =========================

    admin_id = session.get("user_id")

    if not admin_id:

        return redirect(url_for("login"))


    try:

        # =========================
        # GET CURRENT ADMIN PROFILE
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
        # PREVENT APPROVING ADMIN
        # =========================

        if user["role"] == "admin":

            return "Admin accounts cannot be approved here.", 403


        # =========================
        # APPROVE USER
        # =========================

        supabase_admin \
            .table("profiles") \
            .update({
                "status": "approved"
            }) \
            .eq("id", user_id) \
            .execute()


        return redirect(url_for("admin_dashboard"))


    except Exception as e:

        return f"Approval error: {str(e)}", 500


@app.route("/admin/users/<user_id>/reject", methods=["POST"])
def reject_user(user_id):

    # =========================
    # CHECK LOGIN
    # =========================

    admin_id = session.get("user_id")

    if not admin_id:

        return redirect(url_for("login"))


    try:

        # =========================
        # GET CURRENT ADMIN PROFILE
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
        # PREVENT REJECTING ADMIN
        # =========================

        if user["role"] == "admin":

            return "Admin accounts cannot be rejected here.", 403


        # =========================
        # REJECT USER
        # =========================

        supabase_admin \
            .table("profiles") \
            .update({
                "status": "rejected"
            }) \
            .eq("id", user_id) \
            .execute()


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
    # CHECK LOGIN
    # =========================

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    try:

        # =========================
        # GET DENTIST PROFILE
        # =========================

        response = (
            supabase_admin
            .table("profiles")
            .select("*")
            .eq("id", user_id)
            .execute()
        )

        profiles = response.data or []


        # =========================
        # PROFILE NOT FOUND
        # =========================

        if not profiles:
            session.clear()
            return "Profile not found.", 404

        profile = profiles[0]


        # =========================
        # CHECK ROLE
        # =========================

        if profile["role"] != "dentist":
            return "Access denied.", 403


        # =========================
        # CHECK APPROVAL
        # =========================

        if profile["status"] != "approved":
            if profile["status"] == "pending":
                return "Your account is still waiting for administrator approval.", 403
            if profile["status"] == "rejected":
                return "Your account application was rejected.", 403
            return "Your account is not approved.", 403


        # =========================
        # GET MY DENTAL CASES (WITH TECH INFO)
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


        # =========================
        # GET TEETH FOR EACH CASE
        # =========================

        for case in dental_cases:
            teeth_response = (
                supabase_admin
                .table("case_teeth")
                .select("*")
                .eq("case_id", case["id"])
                .order("created_at")
                .execute()
            )
            case["teeth"] = teeth_response.data or []


        # =========================
        # GET NOTIFICATIONS
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
        # DENTIST DASHBOARD
        # =========================

        return render_template(
            "dentist_dashboard.html",
            profile=profile,
            dental_cases=dental_cases,
            notifications=notifications
        )

    except Exception as e:
        return f"Dentist dashboard error: {str(e)}", 500

@app.route("/dentist/cases/new", methods=["GET", "POST"])
def create_dental_case():

    # ==========================================
    # STORAGE
    # ==========================================

    CASE_FILES_BUCKET = "dental-case-files"


    # ==========================================
    # CHECK LOGIN
    # ==========================================

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))


    try:

        # ==========================================
        # GET DENTIST PROFILE
        # ==========================================

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


        # ==========================================
        # CHECK ROLE
        # ==========================================

        if profile.get("role") != "dentist":

            return "Access denied.", 403


        # ==========================================
        # CHECK APPROVAL
        # ==========================================

        if profile.get("status") != "approved":

            if profile.get("status") == "pending":

                return (
                    "Your account is still waiting "
                    "for administrator approval.",
                    403
                )

            if profile.get("status") == "rejected":

                return (
                    "Your account application was rejected.",
                    403
                )

            return "Your account is not approved.", 403


        # ==========================================
        # POST
        # ==========================================

        if request.method == "POST":

            # ======================================
            # BASIC CASE INFORMATION
            # ======================================

            patient_name = (
                request.form.get(
                    "patient_name",
                    ""
                ).strip()
            )


            patient_reference = (
                request.form.get(
                    "patient_reference",
                    ""
                ).strip()
                or None
            )


            case_type = (
                request.form.get(
                    "case_type",
                    ""
                ).strip()
            )


            instructions = (
                request.form.get(
                    "instructions",
                    ""
                ).strip()
                or None
            )


            # ======================================
            # SUBMIT ACTION
            #
            # SAVE DRAFT = draft
            # SEND       = submitted
            # ======================================

            submit_action = (
                request.form.get(
                    "submit_action",
                    "draft"
                ).strip()
            )


            if submit_action == "send":

                case_status = "submitted"

            else:

                case_status = "draft"


            # ======================================
            # VALIDATION
            # ======================================

            if not patient_name:

                return (
                    "Patient name is required.",
                    400
                )


            if not case_type:

                return (
                    "Case type is required.",
                    400
                )


            # ======================================
            # GET SELECTED TEETH
            # ======================================

            tooth_numbers = request.form.getlist(
                "tooth_number"
            )


            materials = request.form.getlist(
                "material"
            )


            shades = request.form.getlist(
                "shade"
            )


            tooth_notes = request.form.getlist(
                "tooth_notes"
            )


            # ======================================
            # BUILD TEETH DATA
            # ======================================

            teeth_to_insert = []


            for index, tooth_number in enumerate(
                tooth_numbers
            ):

                tooth_number = (
                    tooth_number.strip()
                )


                if not tooth_number:
                    continue


                # ----------------------------------
                # MATERIAL
                # ----------------------------------

                material = None

                if index < len(materials):

                    material = (
                        materials[index]
                        .strip()
                        or None
                    )


                # ----------------------------------
                # SHADE
                # ----------------------------------

                shade = None

                if index < len(shades):

                    shade = (
                        shades[index]
                        .strip()
                        or None
                    )


                # ----------------------------------
                # NOTES
                # ----------------------------------

                notes = None

                if index < len(tooth_notes):

                    notes = (
                        tooth_notes[index]
                        .strip()
                        or None
                    )


                # ----------------------------------
                # RESTORATION TYPE
                # ----------------------------------

                restoration_type = case_type


                teeth_to_insert.append({

                    "tooth_number":
                        tooth_number,

                    "restoration_type":
                        restoration_type,

                    "material":
                        material,

                    "shade":
                        shade,

                    "notes":
                        notes

                })


            # ======================================
            # REQUIRE AT LEAST ONE TOOTH
            # ======================================

            if not teeth_to_insert:

                return (
                    "Please select at least one tooth.",
                    400
                )


            # ======================================
            # VALIDATE STL FILE
            # ======================================

            stl_file = request.files.get(
                "stl_file"
            )


            if (
                stl_file
                and stl_file.filename
            ):

                stl_filename = secure_filename(
                    stl_file.filename
                )


                if not stl_filename:

                    return (
                        "Invalid STL filename.",
                        400
                    )


                if not stl_filename.lower().endswith(
                    ".stl"
                ):

                    return (
                        "Only STL files are allowed "
                        "for the STL upload.",
                        400
                    )


            # ======================================
            # GET SMILE FILES
            # ======================================

            smile_files = request.files.getlist(
                "patient_smile"
            )


            allowed_image_extensions = {

                ".jpg",
                ".jpeg",
                ".png",
                ".webp"

            }


            for smile_file in smile_files:

                if not smile_file:
                    continue


                if not smile_file.filename:
                    continue


                smile_filename = secure_filename(
                    smile_file.filename
                )


                if not smile_filename:

                    return (
                        "Invalid patient smile "
                        "filename.",
                        400
                    )


                extension = ""


                if "." in smile_filename:

                    extension = (
                        "."
                        + smile_filename.rsplit(
                            ".",
                            1
                        )[1].lower()
                    )


                if (
                    extension
                    not in allowed_image_extensions
                ):

                    return (
                        "Only JPG, JPEG, PNG, "
                        "or WEBP images are allowed.",
                        400
                    )


            # ======================================
            # GENERATE CASE NUMBER
            # ======================================

            existing_cases_response = (
                supabase_admin
                .table("dental_cases")
                .select("id")
                .execute()
            )


            existing_cases = (
                existing_cases_response.data
                or []
            )


            case_number = (
                f"DL-{len(existing_cases) + 1:05d}"
            )


            # ======================================
            # CREATE DENTAL CASE
            # ======================================

            case_response = (
                supabase_admin
                .table("dental_cases")
                .insert({

                    "case_number":
                        case_number,

                    "dentist_id":
                        user_id,

                    "patient_name":
                        patient_name,

                    "patient_reference":
                        patient_reference,

                    "case_type":
                        case_type,

                    "instructions":
                        instructions,

                    "status":
                        case_status

                })
                .execute()
            )


            created_cases = (
                case_response.data
                or []
            )


            if not created_cases:

                return (
                    "Failed to create dental case.",
                    500
                )


            case = created_cases[0]


            # ======================================
            # ADD CASE ID TO TEETH
            # ======================================

            for tooth in teeth_to_insert:

                tooth["case_id"] = case["id"]


            # ======================================
            # INSERT CASE TEETH
            # ======================================

            try:

                teeth_response = (
                    supabase_admin
                    .table("case_teeth")
                    .insert(teeth_to_insert)
                    .execute()
                )

            except Exception as teeth_error:

                (
                    supabase_admin
                    .table("dental_cases")
                    .delete()
                    .eq(
                        "id",
                        case["id"]
                    )
                    .execute()
                )

                return (
                    "Failed to save selected teeth: "
                    f"{str(teeth_error)}",
                    500
                )


            if not teeth_response.data:

                (
                    supabase_admin
                    .table("dental_cases")
                    .delete()
                    .eq(
                        "id",
                        case["id"]
                    )
                    .execute()
                )

                return (
                    "Failed to save selected teeth.",
                    500
                )


            # ======================================
            # FILE RECORDS
            # ======================================

            uploaded_files = []


            # ======================================
            # STL FILE UPLOAD
            # ======================================

            if (
                stl_file
                and stl_file.filename
            ):

                original_name = (
                    stl_file.filename
                )


                safe_name = secure_filename(
                    original_name
                )


                storage_path = (
                    f"{user_id}/"
                    f"{case['id']}/"
                    f"stl/"
                    f"{safe_name}"
                )


                file_bytes = stl_file.read()


                mime_type = (
                    stl_file.mimetype
                    or "application/octet-stream"
                )


                file_size = len(file_bytes)


                try:

                    (
                        supabase_admin
                        .storage
                        .from_(CASE_FILES_BUCKET)
                        .upload(
                            storage_path,
                            file_bytes,
                            {
                                "content-type":
                                    mime_type,

                                "upsert":
                                    "false"
                            }
                        )
                    )

                except Exception as upload_error:

                    # ------------------------------
                    # ROLLBACK CASE TEETH
                    # ------------------------------

                    (
                        supabase_admin
                        .table("case_teeth")
                        .delete()
                        .eq(
                            "case_id",
                            case["id"]
                        )
                        .execute()
                    )


                    # ------------------------------
                    # ROLLBACK CASE
                    # ------------------------------

                    (
                        supabase_admin
                        .table("dental_cases")
                        .delete()
                        .eq(
                            "id",
                            case["id"]
                        )
                        .execute()
                    )


                    return (
                        "Failed to upload STL file: "
                        f"{str(upload_error)}",
                        500
                    )


                uploaded_files.append({

                    "case_id":
                        case["id"],

                    "uploaded_by":
                        user_id,

                    "file_name":
                        original_name,

                    "file_path":
                        storage_path,

                    "file_type":
                        "stl",

                    "mime_type":
                        mime_type,

                    "file_size":
                        file_size

                })


            # ======================================
            # PATIENT SMILE PHOTO UPLOADS
            # ======================================

            for smile_file in smile_files:

                if not smile_file:
                    continue


                if not smile_file.filename:
                    continue


                original_name = (
                    smile_file.filename
                )


                safe_name = secure_filename(
                    original_name
                )


                storage_path = (
                    f"{user_id}/"
                    f"{case['id']}/"
                    f"smile/"
                    f"{safe_name}"
                )


                file_bytes = smile_file.read()


                mime_type = (
                    smile_file.mimetype
                    or "image/jpeg"
                )


                file_size = len(file_bytes)


                try:

                    (
                        supabase_admin
                        .storage
                        .from_(CASE_FILES_BUCKET)
                        .upload(
                            storage_path,
                            file_bytes,
                            {
                                "content-type":
                                    mime_type,

                                "upsert":
                                    "false"
                            }
                        )
                    )

                except Exception as upload_error:

                    # ------------------------------
                    # REMOVE ALREADY UPLOADED FILES
                    # ------------------------------

                    for uploaded_file in uploaded_files:

                        try:

                            (
                                supabase_admin
                                .storage
                                .from_(
                                    CASE_FILES_BUCKET
                                )
                                .remove([
                                    uploaded_file[
                                        "file_path"
                                    ]
                                ])
                            )

                        except Exception:

                            pass


                    # ------------------------------
                    # ROLLBACK CASE TEETH
                    # ------------------------------

                    (
                        supabase_admin
                        .table("case_teeth")
                        .delete()
                        .eq(
                            "case_id",
                            case["id"]
                        )
                        .execute()
                    )


                    # ------------------------------
                    # ROLLBACK CASE
                    # ------------------------------

                    (
                        supabase_admin
                        .table("dental_cases")
                        .delete()
                        .eq(
                            "id",
                            case["id"]
                        )
                        .execute()
                    )


                    return (
                        "Failed to upload patient "
                        "smile photo: "
                        f"{str(upload_error)}",
                        500
                    )


                uploaded_files.append({

                    "case_id":
                        case["id"],

                    "uploaded_by":
                        user_id,

                    "file_name":
                        original_name,

                    "file_path":
                        storage_path,

                    "file_type":
                        "smile_image",

                    "mime_type":
                        mime_type,

                    "file_size":
                        file_size

                })


            # ======================================
            # SAVE FILE RECORDS
            # ======================================

            if uploaded_files:

                try:

                    file_response = (
                        supabase_admin
                        .table("case_files")
                        .insert(uploaded_files)
                        .execute()
                    )

                except Exception as file_error:

                    # ------------------------------
                    # REMOVE STORAGE FILES
                    # ------------------------------

                    for uploaded_file in uploaded_files:

                        try:

                            (
                                supabase_admin
                                .storage
                                .from_(
                                    CASE_FILES_BUCKET
                                )
                                .remove([
                                    uploaded_file[
                                        "file_path"
                                    ]
                                ])
                            )

                        except Exception:

                            pass


                    # ------------------------------
                    # ROLLBACK CASE TEETH
                    # ------------------------------

                    (
                        supabase_admin
                        .table("case_teeth")
                        .delete()
                        .eq(
                            "case_id",
                            case["id"]
                        )
                        .execute()
                    )


                    # ------------------------------
                    # ROLLBACK CASE
                    # ------------------------------

                    (
                        supabase_admin
                        .table("dental_cases")
                        .delete()
                        .eq(
                            "id",
                            case["id"]
                        )
                        .execute()
                    )


                    return (
                        "Failed to save uploaded "
                        "file records: "
                        f"{str(file_error)}",
                        500
                    )


                if not file_response.data:

                    # ------------------------------
                    # REMOVE STORAGE FILES
                    # ------------------------------

                    for uploaded_file in uploaded_files:

                        try:

                            (
                                supabase_admin
                                .storage
                                .from_(
                                    CASE_FILES_BUCKET
                                )
                                .remove([
                                    uploaded_file[
                                        "file_path"
                                    ]
                                ])
                            )

                        except Exception:

                            pass


                    # ------------------------------
                    # ROLLBACK CASE TEETH
                    # ------------------------------

                    (
                        supabase_admin
                        .table("case_teeth")
                        .delete()
                        .eq(
                            "case_id",
                            case["id"]
                        )
                        .execute()
                    )


                    # ------------------------------
                    # ROLLBACK CASE
                    # ------------------------------

                    (
                        supabase_admin
                        .table("dental_cases")
                        .delete()
                        .eq(
                            "id",
                            case["id"]
                        )
                        .execute()
                    )


                    return (
                        "Failed to save uploaded "
                        "file records.",
                        500
                    )


            # ======================================
            # NOTIFY TECHNICIANS (IF SUBMITTED)
            # ======================================

            if case_status == "submitted":
                try:
                    techs_res = (
                        supabase_admin
                        .table("profiles")
                        .select("id")
                        .eq("role", "technician")
                        .eq("status", "approved")
                        .execute()
                    )
                    technicians = techs_res.data or []
                    
                    dentist_name = profile.get("display_name") or f"Dr. {profile.get('first_name', '')}"

                    for tech in technicians:
                        create_notification(
                            user_id=tech["id"],
                            message=f"New Case Submitted: {case_number} ({case_type}) by {dentist_name}",
                            link=f"/technician/cases/{case['id']}"
                        )
                except Exception as notif_err:
                    print(f"Notification error: {str(notif_err)}")


            # ======================================
            # SUCCESS
            # ======================================

            return redirect(
                url_for(
                    "dentist_dashboard"
                )
            )


        # ==========================================
        # GET
        # ==========================================

        return render_template(
            "create_dental_case.html",
            profile=profile
        )


    # ==========================================
    # GENERAL ERROR
    # ==========================================

    except Exception as e:

        return (
            f"Create dental case error: {str(e)}",
            500
        )

# =========================
# REGISTER
# =========================

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        # =========================
        # GET FORM DATA
        # =========================

        first_name = request.form["first_name"]
        last_name = request.form["last_name"]
        display_name = request.form["display_name"]
        phone = request.form["phone"]

        role = request.form["role"]
        professional_id = request.form["professional_id"]

        email = request.form["email"]
        password = request.form["password"]
        confirm_password = request.form["confirm_password"]


        # =========================
        # VALIDATE PASSWORD
        # =========================

        if password != confirm_password:

            return "Passwords do not match."


        # =========================
        # PREVENT ADMIN REGISTRATION
        # =========================

        if role not in ["dentist", "technician"]:

            return "Invalid role."


        try:

            # =========================
            # CREATE SUPABASE AUTH USER
            # =========================

            response = supabase.auth.sign_up({
                "email": email,
                "password": password
            })


            if not response.user:

                return "Registration failed."


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

            return f"Registration error: {str(e)}"


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

                return "Login failed."


            # =========================
            # GET USER ID
            # =========================

            user_id = response.user.id


            # =========================
            # STORE USER ID IN SESSION
            # =========================

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


            # =========================
            # PROFILE NOT FOUND
            # =========================

            if not profiles:

                session.clear()

                return "Profile not found.", 404


            profile = profiles[0]


            # =========================
            # CHECK ACCOUNT STATUS
            # =========================

            if profile["status"] == "pending":

                return render_template("account_pending.html")


            if profile["status"] == "rejected":

                return "Your account application was rejected.", 403


            if profile["status"] != "approved":

                return "Your account is not approved.", 403


            # =========================
            # REDIRECT BY ROLE
            # =========================

            if profile["role"] == "admin":

                return redirect(
                    url_for("admin_dashboard")
                )


            if profile["role"] == "dentist":

                return redirect(
                    url_for("dentist_dashboard")
                )


            if profile["role"] == "technician":

                return redirect(
                    url_for("technician_dashboard")
                )


            # =========================
            # INVALID ROLE
            # =========================

            session.clear()

            return "Invalid account role.", 403


        except Exception as e:

            return f"Login error: {str(e)}"


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
        # CHECK ADMIN ROLE
        # =========================

        if admin_profile["role"] != "admin":

            return "Access denied.", 403


        # =========================
        # CHECK ADMIN APPROVAL
        # =========================

        if admin_profile["status"] != "approved":

            return "Administrator account is not approved.", 403


        # =========================
        # GET USER APPLICATION
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
            user=user
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

# =========================
# TECHNICIAN DASHBOARD
# =========================

# =========================
# TECHNICIAN DASHBOARD
# =========================
@app.route("/technician")
def technician_dashboard():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    try:
        # Get Technician Profile
        response = supabase_admin.table("profiles").select("*").eq("id", user_id).execute()
        profiles = response.data or []
        if not profiles or profiles[0]["role"] != "technician" or profiles[0]["status"] != "approved":
            return "Access denied.", 403

        profile = profiles[0]

        # GET ONLY CASES ASSIGNED TO THIS TECHNICIAN
        cases_response = (
            supabase_admin
            .table("dental_cases")
            .select("*")
            .eq("technician_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        assigned_cases = cases_response.data or []

        # Get Notifications
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
            "technician_dashboard.html",
            profile=profile,
            cases=assigned_cases,
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

            # Create a secure temporary URL valid for 1 hour (3600 seconds)
            signed_url = (
                supabase_admin
                .storage
                .from_(CASE_FILES_BUCKET)
                .create_signed_url(file_record["file_path"], 3600)
            )
            
            # Extract the actual URL string from the response
            if isinstance(signed_url, dict):
                file_record["download_url"] = signed_url.get("signedURL")
            else:
                file_record["download_url"] = signed_url


        return render_template(
            "technician_case_details.html",
            case=case,
            teeth=teeth,
            files=files
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
@app.route("/admin/cases/<case_id>/assign", methods=["POST"])
def admin_assign_case(case_id):
    admin_id = session.get("user_id")
    if not admin_id:
        return redirect(url_for("login"))

    try:
        # Verify Admin
        admin_res = supabase_admin.table("profiles").select("role").eq("id", admin_id).execute()
        if not admin_res.data or admin_res.data[0]["role"] != "admin":
            return "Access denied.", 403

        technician_id = request.form.get("technician_id")
        if not technician_id:
            return "Please select a technician.", 400

        # Fetch case and technician details for notification
        case_res = supabase_admin.table("dental_cases").select("case_number").eq("id", case_id).execute()
        tech_res = supabase_admin.table("profiles").select("display_name").eq("id", technician_id).execute()
        
        if not case_res.data or not tech_res.data:
            return "Case or technician not found.", 404

        case_number = case_res.data[0]["case_number"]
        tech_name = tech_res.data[0]["display_name"]

        # Assign case and update status
        supabase_admin.table("dental_cases").update({
            "technician_id": technician_id,
            "status": "assigned"
        }).eq("id", case_id).execute()

        # Notify the assigned technician
        create_notification(
            user_id=technician_id,
            message=f"New Assignment: You have been assigned Case {case_number}.",
            link=f"/technician/cases/{case_id}"
        )

        return redirect(url_for("admin_dashboard"))

    except Exception as e:
        return f"Assignment error: {str(e)}", 500
# =========================
# START FLASK
# =========================

if __name__ == "__main__":
    app.run(debug=True)