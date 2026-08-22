import os

from flask import Flask, render_template, request, redirect, url_for, session
from supabase import create_client, Client
from dotenv import load_dotenv

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
@app.route("/admin")
def admin_dashboard():

    # =========================
    # CHECK LOGIN
    # =========================

    user_id = session.get("user_id")

    if not user_id:

        return redirect(url_for("login"))


    try:

        # =========================
        # GET ADMIN PROFILE
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


        # =========================
        # CHECK ADMIN ROLE
        # =========================

        if profile["role"] != "admin":

            return "Access denied.", 403


        # =========================
        # CHECK ADMIN APPROVAL
        # =========================

        if profile["status"] != "approved":

            return "Administrator account is not approved.", 403


        # =========================
        # GET ALL USERS
        # =========================

        all_response = (
            supabase_admin
            .table("profiles")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        )

        all_users = all_response.data or []


        # =========================
        # SEPARATE USERS BY STATUS
        # =========================

        pending_users = [
            user for user in all_users
            if user["status"] == "pending"
        ]


        approved_users = [
            user for user in all_users
            if user["status"] == "approved"
        ]


        rejected_users = [
            user for user in all_users
            if user["status"] == "rejected"
        ]


        # =========================
        # COUNTS
        # =========================

        pending_count = len(pending_users)

        approved_count = len(approved_users)

        rejected_count = len(rejected_users)

        total_count = len(all_users)


        # =========================
        # SHOW ADMIN DASHBOARD
        # =========================

        return render_template(
            "admin_dashboard.html",

            profile=profile,

            pending_users=pending_users,

            approved_users=approved_users,

            rejected_users=rejected_users,

            pending_count=pending_count,

            approved_count=approved_count,

            rejected_count=rejected_count,

            total_count=total_count
        )


    except Exception as e:

        return f"Admin dashboard error: {str(e)}", 500

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

                return (
                    "Your account is still waiting "
                    "for administrator approval.",
                    403
                )

            if profile["status"] == "rejected":

                return (
                    "Your account application was rejected.",
                    403
                )

            return "Your account is not approved.", 403


        # =========================
        # GET MY DENTAL CASES
        # =========================
        #
        # IMPORTANT:
        # Only retrieve cases where
        # dentist_id matches the
        # currently logged-in dentist.
        #
        # This prevents Dentist B
        # from seeing Dentist A's cases.
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

            case["teeth"] = (
                teeth_response.data or []
            )


        # =========================
        # DENTIST DASHBOARD
        # =========================

        return render_template(
            "dentist_dashboard.html",

            profile=profile,

            dental_cases=dental_cases
        )


    except Exception as e:

        return (
            f"Dentist dashboard error: {str(e)}",
            500
        )

@app.route("/dentist/cases/new", methods=["GET", "POST"])
def create_dental_case():

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

                return (
                    "Your account is still waiting "
                    "for administrator approval.",
                    403
                )

            if profile["status"] == "rejected":

                return (
                    "Your account application was rejected.",
                    403
                )

            return "Your account is not approved.", 403


        # =========================
        # CREATE CASE
        # =========================

        if request.method == "POST":

            patient_name = request.form["patient_name"].strip()

            patient_reference = (
                request.form.get("patient_reference", "").strip()
                or None
            )

            case_type = request.form["case_type"].strip()

            material = (
                request.form.get("material", "").strip()
                or None
            )

            shade = (
                request.form.get("shade", "").strip()
                or None
            )

            instructions = (
                request.form.get("instructions", "").strip()
                or None
            )


            # =========================
            # BASIC VALIDATION
            # =========================

            if not patient_name:

                return "Patient name is required.", 400


            if not case_type:

                return "Case type is required.", 400


            # =========================
            # GENERATE CASE NUMBER
            # =========================

            existing_cases_response = (
                supabase_admin
                .table("dental_cases")
                .select("id")
                .execute()
            )

            existing_cases = (
                existing_cases_response.data or []
            )

            case_number = (
                f"DL-{len(existing_cases) + 1:05d}"
            )


            # =========================
            # CREATE DENTAL CASE
            # =========================

            case_response = (
                supabase_admin
                .table("dental_cases")
                .insert({
                    "case_number": case_number,
                    "dentist_id": user_id,
                    "patient_name": patient_name,
                    "patient_reference": patient_reference,
                    "case_type": case_type,
                    "material": material,
                    "shade": shade,
                    "instructions": instructions,
                    "status": "draft"
                })
                .execute()
            )


            created_cases = case_response.data or []


            if not created_cases:

                return "Failed to create dental case.", 500


            case = created_cases[0]


            # =========================
            # GET TEETH FROM FORM
            # =========================

            tooth_numbers = request.form.getlist(
                "tooth_number"
            )

            restoration_types = request.form.getlist(
                "restoration_type"
            )

            tooth_notes = request.form.getlist(
                "tooth_notes"
            )


            # =========================
            # CREATE CASE TEETH
            # =========================

            teeth_to_insert = []


            for index, tooth_number in enumerate(
                tooth_numbers
            ):

                tooth_number = tooth_number.strip()


                if not tooth_number:

                    continue


                restoration_type = ""


                if index < len(restoration_types):

                    restoration_type = (
                        restoration_types[index].strip()
                    )


                notes = None


                if index < len(tooth_notes):

                    notes = (
                        tooth_notes[index].strip()
                        or None
                    )


                if not restoration_type:

                    continue


                teeth_to_insert.append({

                    "case_id": case["id"],

                    "tooth_number": tooth_number,

                    "restoration_type": restoration_type,

                    "notes": notes

                })


            # =========================
            # REQUIRE AT LEAST ONE TOOTH
            # =========================

            if not teeth_to_insert:

                # Remove the case if no teeth
                # were provided.

                (
                    supabase_admin
                    .table("dental_cases")
                    .delete()
                    .eq("id", case["id"])
                    .execute()
                )

                return (
                    "At least one tooth is required.",
                    400
                )


            # =========================
            # INSERT TEETH
            # =========================

            (
                supabase_admin
                .table("case_teeth")
                .insert(teeth_to_insert)
                .execute()
            )


            # =========================
            # REDIRECT TO DENTIST
            # DASHBOARD
            # =========================

            return redirect(
                url_for("dentist_dashboard")
            )


        # =========================
        # SHOW CREATE CASE PAGE
        # =========================

        return render_template(
            "create_dental_case.html",
            profile=profile
        )


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

@app.route("/technician")
def technician_dashboard():

    # =========================
    # CHECK LOGIN
    # =========================

    user_id = session.get("user_id")

    if not user_id:

        return redirect(url_for("login"))


    try:

        # =========================
        # GET TECHNICIAN PROFILE
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

        if profile["role"] != "technician":

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
        # TECHNICIAN DASHBOARD
        # =========================

        return render_template(
            "technician_dashboard.html",
            profile=profile
        )


    except Exception as e:

        return f"Technician dashboard error: {str(e)}", 500


# =========================
# START FLASK
# =========================

if __name__ == "__main__":
    app.run(debug=True)