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

    user = session.get("user")

    return render_template(
        "home.html",
        user=user
    )

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


    # =========================
    # GET ADMIN PROFILE
    # =========================

    response = (
        supabase_admin
        .table("profiles")
        .select("*")
        .eq("id", user_id)
        .single()
        .execute()
    )

    profile = response.data


    if not profile:

        session.clear()

        return "Profile not found."


    # =========================
    # CHECK ADMIN ROLE
    # =========================

    if profile["role"] != "admin":

        return "Access denied.", 403


    # =========================
    # CHECK APPROVAL
    # =========================

    if profile["status"] != "approved":

        return "Administrator account is not approved.", 403


    # =========================
    # GET PENDING USERS
    # =========================

    pending_response = (
        supabase_admin
        .table("profiles")
        .select("*")
        .eq("status", "pending")
        .order("created_at", desc=True)
        .execute()
    )

    pending_users = pending_response.data or []


    # =========================
    # GET ALL USERS
    # =========================

    all_response = (
        supabase_admin
        .table("profiles")
        .select("id, status")
        .execute()
    )

    all_users = all_response.data or []


    pending_count = len([
        user for user in all_users
        if user["status"] == "pending"
    ])

    approved_count = len([
        user for user in all_users
        if user["status"] == "approved"
    ])

    total_count = len(all_users)


    return render_template(
        "admin_dashboard.html",

        profile=profile,

        pending_users=pending_users,

        pending_count=pending_count,

        approved_count=approved_count,

        total_count=total_count
    )

# =========================
# APPROVE USER
# =========================

@app.route("/admin/approve/<user_id>", methods=["POST"])
def approve_user(user_id):

    # Check admin is logged in

    admin_id = session.get("user_id")

    if not admin_id:

        return redirect(url_for("login"))


    # Get admin profile

    response = (
        supabase_admin
        .table("profiles")
        .select("*")
        .eq("id", admin_id)
        .single()
        .execute()
    )

    admin_profile = response.data


    if not admin_profile:

        return "Admin profile not found.", 403


    # Verify admin

    if admin_profile["role"] != "admin":

        return "Access denied.", 403


    if admin_profile["status"] != "approved":

        return "Access denied.", 403


    # Approve user

    supabase_admin \
        .table("profiles") \
        .update({
            "status": "approved"
        }) \
        .eq("id", user_id) \
        .execute()


    return redirect(url_for("admin_dashboard"))
# =========================
# REJECT USER
# =========================
@app.route("/admin/reject/<user_id>", methods=["POST"])
def reject_user(user_id):

    # Check admin is logged in

    admin_id = session.get("user_id")

    if not admin_id:

        return redirect(url_for("login"))


    # Get admin profile

    response = (
        supabase_admin
        .table("profiles")
        .select("*")
        .eq("id", admin_id)
        .single()
        .execute()
    )

    admin_profile = response.data


    if not admin_profile:

        return "Admin profile not found.", 403


    # Verify admin

    if admin_profile["role"] != "admin":

        return "Access denied.", 403


    if admin_profile["status"] != "approved":

        return "Access denied.", 403


    # Reject user

    supabase_admin \
        .table("profiles") \
        .update({
            "status": "rejected"
        }) \
        .eq("id", user_id) \
        .execute()


    return redirect(url_for("admin_dashboard"))
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

        email = request.form["email"]
        password = request.form["password"]

        try:

            response = supabase.auth.sign_in_with_password({
                "email": email,
                "password": password
            })

            if not response.user:

                return "Login failed."

            # Store the Supabase user ID in Flask session
            session["user_id"] = response.user.id

            return redirect(url_for("admin_dashboard"))

        except Exception as e:

            return f"Login error: {str(e)}"

    return render_template("login.html")

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

@app.route("/logout")
def logout():

    try:
        supabase.auth.sign_out()
    except Exception:
        pass

    session.clear()

    return redirect(url_for("home"))


# =========================
# START FLASK
# =========================

if __name__ == "__main__":
    app.run(debug=True)