# app/auth.py
from datetime import datetime, timedelta
from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    current_app,
    session,
)
from flask_login import login_user, logout_user, login_required, current_user
from .models import User, LoginAudit, Message
from . import db
import re
import secrets
from .security import too_many_failed_logins, get_client_ip


auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


def _audit_login_attempt(
    *, username_or_email: str, user_id: int | None, success: bool
) -> None:
    try:
        host = (request.host or "").strip()
        ip = get_client_ip()
        ua = (request.headers.get("User-Agent") or "")[:500]

        row = LoginAudit(
            username_or_email=username_or_email,
            user_id=user_id,
            success=success,
            host=host,
            ip=ip,
            user_agent=ua,
        )
        db.session.add(row)
        db.session.commit()
    except Exception:
        db.session.rollback()


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    if request.method == "POST":
        # username_or_email = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        # Turnstile token from the form
        token = request.form.get("cf-turnstile-response", "")
        # Verify with Cloudflare
        import requests

        secret = current_app.config["TURNSTILE_SECRET_KEY"]
        verify_url = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
        resp = requests.post(
            verify_url,
            data={
                "secret": secret,
                "response": token,
                "remoteip": get_client_ip(),
            },
            timeout=5,
        )
        data = resp.json()
        if not data.get("success"):
            flash("Captcha verification failed. Try again.", "danger")
            return render_template(
                "auth_register.html",
                turnstile_site_key=current_app.config["TURNSTILE_SITE_KEY"],
            )
        username = request.form.get("username", "").strip()
        # Normal validation
        if not username or not email or not password:
            flash("All fields are required.", "danger")
        elif password != confirm:
            flash("Passwords do not match.", "danger")
        elif User.query.filter(
            (User.username == username) | (User.email == email)
        ).first():
            flash("Username or email already taken.", "danger")
        else:
            user = User(username=username, email=email)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash("Registration successful. Please log in.", "success")
            return redirect(url_for("auth.login"))
        # If we fall through due to validation errors:
        return render_template(
            "auth_register.html",
            turnstile_site_key=current_app.config["TURNSTILE_SITE_KEY"],
        )

    # GET request
    return render_template(
        "auth_register.html",
        turnstile_site_key=current_app.config["TURNSTILE_SITE_KEY"],
    )


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    if request.method == "POST":
        ip = get_client_ip()
        if too_many_failed_logins(ip):
            flash(
                "Too many failed login attempts. Please wait a few minutes and try again.",
                "danger",
            )
            # log the rate-limited attempt too (optional but useful)
            username_or_email = request.form.get("username", "").strip()
            _audit_login_attempt(
                username_or_email=username_or_email,
                user_id=None,
                success=False,
            )
            return render_template("auth_login.html"), 429
        username_or_email = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter(
            (User.username == username_or_email)
            | (User.email == username_or_email.lower())
        ).first()

        if user and user.check_password(password):
            _audit_login_attempt(
                username_or_email=username_or_email, user_id=user.id, success=True
            )
            login_user(user, remember=True)
            session.permanent = True
            return redirect(request.args.get("next") or url_for("main.index"))
        else:
            _audit_login_attempt(
                username_or_email=username_or_email,
                user_id=(user.id if user else None),
                success=False,
            )
            flash("Invalid credentials.", "danger")

    return render_template("auth_login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("main.index"))


@auth_bp.route("/login-help", methods=["GET", "POST"])
def login_help():
    # If they are logged in, they can DM normally
    if current_user.is_authenticated:
        flash("You’re already logged in. Use Messages instead.", "info")
        return redirect(url_for("main.inbox"))

    if request.method == "POST":
        # Honeypot (cheap bot filter)
        if (request.form.get("website") or "").strip():
            return ("", 204)

        username_field = (request.form.get("username") or "").strip()[:150]
        email_field = (request.form.get("email") or "").strip()[:254]
        problem_field = (request.form.get("problem") or "").strip()
        otherinfo_field = (request.form.get("otherinfo") or "").strip()

        # Required: email + problem
        if not email_field:
            flash("Email is required.", "danger")
            return render_template("auth_login_help.html")

        # Server-side email verification (HTML5 alone is not enforcement)
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_field):
            flash("Email format looks invalid.", "danger")
            return render_template("auth_login_help.html")

        if not problem_field:
            flash("Problem is required.", "danger")
            return render_template("auth_login_help.html")

        # Size limits
        if len(problem_field) > 2000 or len(otherinfo_field) > 2000:
            flash("Too long. Keep each box under 2000 characters.", "danger")
            return render_template("auth_login_help.html")

        # Rate limit: max 3 submissions per 10 mins per browser session
        now = datetime.utcnow()
        window_seconds = 10 * 60
        history = session.get("login_help_times", [])
        history = [t for t in history if (now.timestamp() - t) < window_seconds]
        if len(history) >= 3:
            flash("Too many requests. Try again in a bit.", "danger")
            session["login_help_times"] = history
            return render_template("auth_login_help.html")
        history.append(now.timestamp())
        session["login_help_times"] = history

        # Recipient (admin)
        admin = User.query.filter_by(username="supergalley").first()
        if not admin:
            flash("Admin user 'supergalley' not found.", "danger")
            return render_template("auth_login_help.html")

        # Sender system user
        sender = User.query.filter_by(username="LoginProblem").first()
        if not sender:
            # Create a system user with a random password (won't be used)
            sender = User(
                username="LoginProblem", email="loginproblem@bannedfromheaven.com"
            )
            sender.set_password(secrets.token_urlsafe(32))
            db.session.add(sender)
            db.session.commit()

        subject = "Login Problems (User Clicked Link on Signin Page)"
        body = (
            f"USERNAME={username_field}\n"
            f"eMAIL={email_field}\n"
            f"PROBLEM={problem_field}\n"
            f"OTHERINFO={otherinfo_field}"
        )

        msg = Message(
            sender_id=sender.id,
            recipient_id=admin.id,
            subject=subject,
            body=body,
        )
        db.session.add(msg)
        db.session.commit()

        flash("Message sent to admin.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth_login_help.html")
