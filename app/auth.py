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
from werkzeug.security import generate_password_hash
from .models import User, LoginAudit, Message, PendingRegistration
from . import db
from .mailer import send_email
from .banning import is_blacklisted
import re
import secrets
from .security import too_many_failed_logins, get_client_ip, ip_is_blocked


auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_VERIFY_MAX_ATTEMPTS = 10
_RESEND_COOLDOWN_SECONDS = 60


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


def _verify_turnstile(token: str) -> bool:
    import requests

    secret = current_app.config["TURNSTILE_SECRET_KEY"]
    verify_url = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    try:
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
        return bool(data.get("success"))
    except Exception:
        return False


def _new_verify_code() -> str:
    # 6-digit numeric code, zero-padded
    return f"{secrets.randbelow(1_000_000):06d}"


def _new_verify_token() -> str:
    return secrets.token_urlsafe(32)


def _purge_expired_pending() -> None:
    try:
        PendingRegistration.query.filter(
            PendingRegistration.expires_at < datetime.utcnow()
        ).delete(synchronize_session=False)
        db.session.commit()
    except Exception:
        db.session.rollback()


def _send_verification_email(pending: PendingRegistration) -> None:
    # Prefer the configured public domain so links don't use container hostnames.
    path = url_for("auth.verify_token", token=pending.token)
    domain = (current_app.config.get("DOMAIN") or "").strip().rstrip("/")
    if domain:
        if "://" not in domain:
            domain = "https://" + domain
        verify_url = domain + path
    else:
        verify_url = url_for("auth.verify_token", token=pending.token, _external=True)
    subject = "Your BannedFromHeaven sign-up code"
    text_body = (
        f"Hello {pending.username},\n\n"
        f"Your verification code is: {pending.code}\n\n"
        f"Or open this link to finish creating your account:\n"
        f"{verify_url}\n\n"
        f"This code expires in "
        f"{current_app.config.get('MAIL_VERIFY_CODE_MINUTES', 30)} minutes.\n\n"
        "IMPORTANT: If you do not see this message in your Inbox, check your "
        "Junk / Spam folder. We send mail from a private mail server with little "
        "reputation, so some providers park it in junk until you mark it as "
        "Not Spam / safe sender (satan@bannedfromheaven.com).\n\n"
        "If you did not request an account, you can ignore this email.\n\n"
        "— BannedFromHeaven\n"
    )
    html_body = f"""\
<html><body style="font-family: system-ui, sans-serif; line-height: 1.5;">
  <p>Hello <strong>{pending.username}</strong>,</p>
  <p>Your verification code is:</p>
  <p style="font-size: 1.6em; letter-spacing: 0.15em; font-weight: bold;">{pending.code}</p>
  <p>Or click the button below to finish creating your account:</p>
  <p><a href="{verify_url}"
        style="display:inline-block;padding:10px 18px;background:#6b0f1a;color:#fff;
               text-decoration:none;border-radius:6px;">
        Verify my email
      </a></p>
  <p style="font-size:0.95em;color:#444;">
    <strong>Important:</strong> if this is not in your Inbox, check your
    <strong>Junk / Spam</strong> folder. We send mail from a private mail server
    with little reputation, so providers often quarantine the first message.
    Add <code>satan@bannedfromheaven.com</code> as a safe sender if you can.
  </p>
  <p style="color:#666;font-size:0.9em;">
    Link (copy/paste if the button fails):<br>
    <a href="{verify_url}">{verify_url}</a>
  </p>
  <p style="color:#888;font-size:0.85em;">
    If you did not request an account, ignore this email.
  </p>
</body></html>
"""
    send_email(
        to=pending.email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )


def _notify_admin_new_user(user: User) -> None:
    """Drop a heads-up in satan's inbox when someone finishes sign-up."""
    admin_to = (
        current_app.config.get("MAIL_NOTIFY_TO")
        or current_app.config.get("MAIL_FROM")
        or "satan@bannedfromheaven.com"
    )
    created = (
        user.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        if user.created_at
        else datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    )
    # Build profile URL without requiring an active request context
    from urllib.parse import quote

    profile_path = f"/user/{quote(user.username)}"
    domain = (current_app.config.get("DOMAIN") or "").strip().rstrip("/")
    if domain:
        if "://" not in domain:
            domain = "https://" + domain
        profile_url = f"{domain}{profile_path}"
    else:
        try:
            profile_url = url_for(
                "main.user_profile", username=user.username, _external=True
            )
        except RuntimeError:
            profile_url = profile_path

    subject = f"New BFH user: {user.username}"
    text_body = (
        "A new user has registered on BannedFromHeaven.\n\n"
        f"User ID:   {user.id}\n"
        f"Username:  {user.username}\n"
        f"Email:     {user.email}\n"
        f"Created:   {created}\n"
        f"Needs mod: {user.needs_moderator}\n"
        f"Profile:   {profile_url}\n"
    )
    html_body = f"""\
<html><body style="font-family: system-ui, sans-serif; line-height: 1.5;">
  <h2 style="margin:0 0 0.5em;">New user registered</h2>
  <table style="border-collapse:collapse;">
    <tr><td style="padding:4px 12px 4px 0;"><strong>User ID</strong></td><td>{user.id}</td></tr>
    <tr><td style="padding:4px 12px 4px 0;"><strong>Username</strong></td><td>{user.username}</td></tr>
    <tr><td style="padding:4px 12px 4px 0;"><strong>Email</strong></td>
        <td><a href="mailto:{user.email}">{user.email}</a></td></tr>
    <tr><td style="padding:4px 12px 4px 0;"><strong>Created</strong></td><td>{created}</td></tr>
    <tr><td style="padding:4px 12px 4px 0;"><strong>Needs moderator</strong></td>
        <td>{user.needs_moderator}</td></tr>
  </table>
  <p style="margin-top:1em;"><a href="{profile_url}">Open profile</a></p>
</body></html>
"""
    send_email(
        to=admin_to,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )


def _finalize_registration(pending: PendingRegistration):
    """Create the real user from a verified pending row. Returns the User."""
    # Re-check uniqueness in case someone sniped the name/email after pending started
    if User.query.filter(
        (User.username == pending.username) | (User.email == pending.email)
    ).first():
        db.session.delete(pending)
        db.session.commit()
        return None

    # Blacklist may have been applied after the pending row was created
    if (
        is_blacklisted("email", pending.email)
        or is_blacklisted("username", pending.username)
        or is_blacklisted("ip", get_client_ip())
        or ip_is_blocked(get_client_ip())
    ):
        db.session.delete(pending)
        db.session.commit()
        return None

    user = User(
        username=pending.username,
        email=pending.email,
        password_hash=pending.password_hash,
        needs_moderator=True,
    )
    db.session.add(user)
    db.session.delete(pending)
    db.session.commit()

    # Notify admin — never block sign-up if mail fails
    try:
        _notify_admin_new_user(user)
    except Exception:
        current_app.logger.exception(
            "Failed to notify admin of new user id=%s username=%s",
            user.id,
            user.username,
        )

    return user


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        token = request.form.get("cf-turnstile-response", "")
        username = request.form.get("username", "").strip()

        if not _verify_turnstile(token):
            flash("Captcha verification failed. Try again.", "danger")
            return render_template(
                "auth_register.html",
                turnstile_site_key=current_app.config["TURNSTILE_SITE_KEY"],
            )

        client_ip = get_client_ip()

        if not username or not email or not password:
            flash("All fields are required.", "danger")
        elif len(username) > 32:
            flash("Username is too long (max 32 characters).", "danger")
        elif not _EMAIL_RE.match(email):
            flash("Email format looks invalid.", "danger")
        elif password != confirm:
            flash("Passwords do not match.", "danger")
        elif len(password) < 6:
            flash("Password must be at least 6 characters.", "danger")
        elif ip_is_blocked(client_ip) or is_blacklisted("ip", client_ip):
            flash("Registration is not available from your network.", "danger")
        elif is_blacklisted("email", email):
            flash("That email cannot be used to register.", "danger")
        elif is_blacklisted("username", username):
            flash("That username is not available.", "danger")
        elif User.query.filter(
            (User.username == username) | (User.email == email)
        ).first():
            flash("Username or email already taken.", "danger")
        else:
            _purge_expired_pending()
            # Replace any prior unfinished attempt for this email/username
            PendingRegistration.query.filter(
                (PendingRegistration.email == email)
                | (PendingRegistration.username == username)
            ).delete(synchronize_session=False)

            minutes = int(current_app.config.get("MAIL_VERIFY_CODE_MINUTES") or 30)
            now = datetime.utcnow()
            pending = PendingRegistration(
                username=username,
                email=email,
                password_hash=generate_password_hash(password),
                code=_new_verify_code(),
                token=_new_verify_token(),
                created_at=now,
                expires_at=now + timedelta(minutes=minutes),
                last_sent_at=now,
                attempts=0,
            )
            db.session.add(pending)
            db.session.commit()

            try:
                _send_verification_email(pending)
            except Exception as e:
                current_app.logger.exception("Failed to send verification email")
                db.session.delete(pending)
                db.session.commit()
                flash(
                    "Could not send the verification email. "
                    "Please try again in a minute, or contact admin if it keeps failing.",
                    "danger",
                )
                return render_template(
                    "auth_register.html",
                    turnstile_site_key=current_app.config["TURNSTILE_SITE_KEY"],
                )

            session["pending_reg_id"] = pending.id
            flash(
                "We emailed you a verification code. "
                "Check your Inbox — and your Junk/Spam folder if it is not there.",
                "success",
            )
            return redirect(url_for("auth.verify"))

        return render_template(
            "auth_register.html",
            turnstile_site_key=current_app.config["TURNSTILE_SITE_KEY"],
        )

    return render_template(
        "auth_register.html",
        turnstile_site_key=current_app.config["TURNSTILE_SITE_KEY"],
    )


@auth_bp.route("/verify", methods=["GET", "POST"])
def verify():
    """Enter the emailed code to complete registration."""
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    pending_id = session.get("pending_reg_id")
    pending = PendingRegistration.query.get(pending_id) if pending_id else None

    if request.method == "POST":
        code = (request.form.get("code") or "").strip().replace(" ", "")
        # Allow completing without session if they paste email+code (optional: email field)
        email = (request.form.get("email") or "").strip().lower()

        if not pending and email:
            pending = (
                PendingRegistration.query.filter_by(email=email)
                .order_by(PendingRegistration.created_at.desc())
                .first()
            )

        if not pending:
            flash(
                "No pending registration found. Please sign up again.",
                "danger",
            )
            return redirect(url_for("auth.register"))

        if pending.is_expired():
            db.session.delete(pending)
            db.session.commit()
            session.pop("pending_reg_id", None)
            flash("That code has expired. Please sign up again.", "danger")
            return redirect(url_for("auth.register"))

        if pending.attempts >= _VERIFY_MAX_ATTEMPTS:
            db.session.delete(pending)
            db.session.commit()
            session.pop("pending_reg_id", None)
            flash("Too many incorrect attempts. Please sign up again.", "danger")
            return redirect(url_for("auth.register"))

        if code != pending.code:
            pending.attempts += 1
            db.session.commit()
            remaining = _VERIFY_MAX_ATTEMPTS - pending.attempts
            flash(
                f"Incorrect code. {remaining} attempt(s) left. "
                "Also check your Junk/Spam folder for the email.",
                "danger",
            )
            return render_template(
                "auth_verify.html",
                email=pending.email,
                username=pending.username,
            )

        user = _finalize_registration(pending)
        session.pop("pending_reg_id", None)
        if not user:
            flash(
                "Username or email was taken before you finished verifying. "
                "Please sign up again.",
                "danger",
            )
            return redirect(url_for("auth.register"))

        flash("Email verified — account created. Please log in.", "success")
        return redirect(url_for("auth.login"))

    # GET
    if not pending:
        # Still show the form so a user with the email can type code after session loss
        return render_template("auth_verify.html", email="", username="")

    if pending.is_expired():
        db.session.delete(pending)
        db.session.commit()
        session.pop("pending_reg_id", None)
        flash("That code has expired. Please sign up again.", "danger")
        return redirect(url_for("auth.register"))

    return render_template(
        "auth_verify.html",
        email=pending.email,
        username=pending.username,
    )


@auth_bp.route("/verify/<token>", methods=["GET"])
def verify_token(token):
    """One-click link from the verification email."""
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    pending = PendingRegistration.query.filter_by(token=token).first()
    if not pending:
        flash("That verification link is invalid or already used.", "danger")
        return redirect(url_for("auth.register"))

    if pending.is_expired():
        db.session.delete(pending)
        db.session.commit()
        flash("That verification link has expired. Please sign up again.", "danger")
        return redirect(url_for("auth.register"))

    user = _finalize_registration(pending)
    session.pop("pending_reg_id", None)
    if not user:
        flash(
            "Username or email was taken before you finished verifying. "
            "Please sign up again.",
            "danger",
        )
        return redirect(url_for("auth.register"))

    flash("Email verified — account created. Please log in.", "success")
    return redirect(url_for("auth.login"))


@auth_bp.route("/verify/resend", methods=["POST"])
def verify_resend():
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    pending_id = session.get("pending_reg_id")
    pending = PendingRegistration.query.get(pending_id) if pending_id else None
    if not pending:
        email = (request.form.get("email") or "").strip().lower()
        if email:
            pending = (
                PendingRegistration.query.filter_by(email=email)
                .order_by(PendingRegistration.created_at.desc())
                .first()
            )

    if not pending:
        flash("No pending registration found. Please sign up again.", "danger")
        return redirect(url_for("auth.register"))

    if pending.is_expired():
        db.session.delete(pending)
        db.session.commit()
        session.pop("pending_reg_id", None)
        flash("That sign-up expired. Please register again.", "danger")
        return redirect(url_for("auth.register"))

    now = datetime.utcnow()
    elapsed = (now - (pending.last_sent_at or pending.created_at)).total_seconds()
    if elapsed < _RESEND_COOLDOWN_SECONDS:
        wait = int(_RESEND_COOLDOWN_SECONDS - elapsed)
        flash(f"Please wait {wait}s before resending.", "warning")
        return redirect(url_for("auth.verify"))

    # Rotate code on resend so old codes stop working
    minutes = int(current_app.config.get("MAIL_VERIFY_CODE_MINUTES") or 30)
    pending.code = _new_verify_code()
    pending.token = _new_verify_token()
    pending.expires_at = now + timedelta(minutes=minutes)
    pending.last_sent_at = now
    pending.attempts = 0
    db.session.commit()

    try:
        _send_verification_email(pending)
    except Exception:
        current_app.logger.exception("Failed to resend verification email")
        flash("Could not resend the email. Try again shortly.", "danger")
        return redirect(url_for("auth.verify"))

    session["pending_reg_id"] = pending.id
    flash(
        "Verification email resent. Check Inbox and Junk/Spam.",
        "success",
    )
    return redirect(url_for("auth.verify"))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    if request.method == "POST":
        ip = get_client_ip()
        if ip_is_blocked(ip):
            _audit_login_attempt(
                username_or_email=request.form.get("username", "").strip(),
                user_id=None,
                success=False,
            )
            flash("Login unavailable.", "danger")
            return render_template("auth_login.html"), 403
        if too_many_failed_logins(ip):
            flash(
                "Too many failed login attempts. Please wait a few minutes and try again.",
                "danger",
            )
            username_or_email = request.form.get("username", "").strip()
            _audit_login_attempt(
                username_or_email=username_or_email,
                user_id=None,
                success=False,
            )
            return render_template("auth_login.html"), 429
        username_or_email = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        # Blacklisted identity or IP — refuse without revealing which rule hit
        if (
            is_blacklisted("username", username_or_email)
            or is_blacklisted("email", username_or_email.lower())
            or ip_is_blocked(ip)
        ):
            _audit_login_attempt(
                username_or_email=username_or_email,
                user_id=None,
                success=False,
            )
            flash("Invalid credentials.", "danger")
            return render_template("auth_login.html")

        user = User.query.filter(
            (User.username == username_or_email)
            | (User.email == username_or_email.lower())
        ).first()

        if user and (
            is_blacklisted("username", user.username)
            or is_blacklisted("email", user.email)
        ):
            _audit_login_attempt(
                username_or_email=username_or_email,
                user_id=user.id,
                success=False,
            )
            flash("Invalid credentials.", "danger")
            return render_template("auth_login.html")

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
        if not _EMAIL_RE.match(email_field):
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
