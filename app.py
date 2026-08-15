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
    # GET USER PROFILE
    # =========================

    response = (
        supabase
        .table("profiles")
        .select("*")
        .eq("id", user_id)
        .single()
        .execute()
    )

    profile = response.data


    # =========================
    # CHECK PROFILE
    # =========================

    if not profile:

        session.clear()

        return "Profile not found."


    # =========================
    # CHECK ROLE
    # =========================

    if profile["role"] != "admin":

        return "Access denied. Administrator privileges required.", 403


    # =========================
    # CHECK APPROVAL
    # =========================

    if profile["status"] != "approved":

        return "Your administrator account is not approved.", 403


    # =========================
    # ADMIN DASHBOARD
    # =========================

    return render_template(
        "admin_dashboard.html",
        profile=profile
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