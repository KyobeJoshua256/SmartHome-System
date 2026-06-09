import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from flask import (
    current_app, flash, make_response, redirect, request,
    session as flask_session, url_for, jsonify, render_template,
)
from flask_login import login_user, logout_user
from flask_wtf.csrf import generate_csrf, validate_csrf
from sqlalchemy import or_, exc as sa_exc

from HomeServer.models.users import User, OTPSession, now_utc, ensure_aware
from HomeServer import database as db


# =============================================================================
# SECTION 1: Constants
# =============================================================================

USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{3,50}$")
SESSION_TIMEOUTS: Dict[str, int] = {
    'token':         300, 
    'login':       21600,   
    'login_attempt': 300,  
}

MAX_ATTEMPTS: Dict[str, int] = {
    'token': 3,
    'login': 5,
}

SECURITY_HEADERS: Dict[str, str] = {
    'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
    'X-Content-Type-Options':    'nosniff',
    'X-Frame-Options':           'DENY',
    'X-XSS-Protection':          '1; mode=block',
    'Referrer-Policy':           'strict-origin-when-cross-origin',
    'Permissions-Policy':        'geolocation=(), microphone=(), camera=()',
}

UGANDA_TZ = timezone(timedelta(hours=3))

_session_storage: Dict[str, str] = {}
_session_storage_lock = threading.Lock()
_SESSION_STORAGE_MAX_KEYS = 10_000


# =============================================================================
# SECTION 2: Custom Exceptions
# =============================================================================

class AuthInitializationError(Exception):
    """Raised when the authentication subsystem encounters a fatal error."""


# =============================================================================
# SECTION 3: Utility Helpers
# =============================================================================

def get_session_storage() -> Dict[str, str]:
    """Return the shared in-process rate-limit dict (reads are GIL-safe)."""
    return _session_storage


def add_security_headers(response: Any) -> Any:
    """Attach standard security headers to *response* and return it."""
    for header, value in SECURITY_HEADERS.items():
        response.headers[header] = value
    return response


def to_uganda_time(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert *dt* (naive UTC or aware) to Uganda time (UTC+3)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(UGANDA_TZ)


# =============================================================================
# SECTION 4: In-memory session helpers (rate-limiting only)
# =============================================================================

def create_session_data(session_type: str, data: Dict[str, Any]) -> str:
    """Store *data* in the in-memory dict under a new session_id and return it."""
    try:
        session_id  = secrets.token_urlsafe(32)
        session_key = f"session:{session_type}:{session_id}"
        payload     = {
            'type':       session_type,
            'created_at': time.time(),
            'data':       data,
        }
        with _session_storage_lock:
            storage = get_session_storage()
            if len(storage) >= _SESSION_STORAGE_MAX_KEYS:
                # Evict expired entries before refusing
                now_ = time.time()
                expired = [
                    k for k, v in list(storage.items())
                    if k.startswith('session:')
                    and now_ - json.loads(v).get('created_at', 0)
                    > SESSION_TIMEOUTS.get(json.loads(v).get('type', ''), 300)
                ]
                for k in expired:
                    storage.pop(k, None)
                if len(storage) >= _SESSION_STORAGE_MAX_KEYS:
                    raise AuthInitializationError("Session storage full")
            storage[session_key] = json.dumps(payload)
        return session_id
    except AuthInitializationError:
        raise
    except Exception as error:
        current_app.logger.error(f"Failed to create session: {error}")
        raise AuthInitializationError(f"Session creation failed: {error}")


def get_session_data(session_type: str, session_id: str) -> Optional[Dict[str, Any]]:
    """Return the session payload or None if missing/expired."""
    try:
        key     = f"session:{session_type}:{session_id}"
        storage = get_session_storage()
        raw     = storage.get(key)
        if not raw:
            return None
        payload = json.loads(raw)
        if time.time() - payload['created_at'] > SESSION_TIMEOUTS.get(session_type, 300):
            with _session_storage_lock:
                storage.pop(key, None)
            return None
        return payload
    except (json.JSONDecodeError, KeyError) as error:
        current_app.logger.warning(f"Invalid session data {session_type}:{session_id}: {error}")
        return None
    except Exception as error:
        current_app.logger.error(f"Failed to get session data: {error}")
        return None


def delete_session(session_type: str, session_id: str) -> None:
    """Remove a session entry from the in-memory store."""
    try:
        key = f"session:{session_type}:{session_id}"
        with _session_storage_lock:
            get_session_storage().pop(key, None)
    except Exception as error:
        current_app.logger.error(f"Failed to delete session: {error}")


def cleanup_expired_sessions() -> None:
    """Evict all expired entries from the in-memory store."""
    try:
        now_ = time.time()
        with _session_storage_lock:
            storage = get_session_storage()
            for key in list(storage.keys()):
                if key.startswith('session:'):
                    try:
                        payload = json.loads(storage.get(key) or '{}')
                        stype   = payload.get('type', '')
                        if now_ - payload.get('created_at', 0) > SESSION_TIMEOUTS.get(stype, 300):
                            del storage[key]
                    except (json.JSONDecodeError, KeyError):
                        pass
    except Exception as error:
        current_app.logger.error(f"Error cleaning up sessions: {error}")


# =============================================================================
# SECTION 5: Security & Rate-Limiting
# =============================================================================

def validate_csrf_token() -> Tuple[bool, str]:
    """Validate the CSRF token present in the current request."""
    try:
        csrf_token = request.form.get('csrf_token') or request.args.get('csrf_token')
        if not csrf_token:
            return False, "Missing CSRF token. Please refresh the page and try again."
        validate_csrf(csrf_token)
        return True, ""
    except Exception as error:
        current_app.logger.warning(f"CSRF validation failed: {error}")
        return False, "Invalid or expired security token. Please refresh and try again."


def rate_limit_check(identifier: str, limit_type: str = 'login') -> Tuple[bool, str]:
    """
    Sliding-window rate limiter backed by the in-memory store.
    Returns (True, '') when under the limit, (False, message) otherwise.
    """
    try:
        window = SESSION_TIMEOUTS.get(limit_type, 300)
        limit  = MAX_ATTEMPTS.get(limit_type, 5)
        key    = f"rate_limit:{limit_type}:{identifier}"
        now_   = time.time()

        with _session_storage_lock:
            storage    = get_session_storage()
            raw        = storage.get(key)
            timestamps: list = json.loads(raw) if raw else []
            timestamps = [t for t in timestamps if now_ - t < window]

            if len(timestamps) >= limit:
                wait = int(window - (now_ - timestamps[0])) + 1
                return False, f"Too many attempts. Please try again in {wait} seconds."

            timestamps.append(now_)
            storage[key] = json.dumps(timestamps)

        return True, ""
    except Exception as error:
        current_app.logger.error(f"Rate limit check failed: {error}")
        return False, "Rate limit check failed. Please try again later."


# =============================================================================
# SECTION 6: User Lookup
# =============================================================================

def find_active_user(identifier: str) -> Optional[User]:
    """
    Look up a non-deleted user by username (case-insensitive),
    e-mail (case-insensitive), or exact phone number.
    Returns the User or None.
    """
    return User.query.filter(
        or_(
            User.username.ilike(identifier),
            User.email.ilike(identifier),
            User.phone == identifier,
        ),
        User.is_deleted == False,  # noqa: E712
    ).first()

# =============================================================================
# SECTION 7: Account Status Check
# =============================================================================

def check_account_status(user: User) -> Tuple[bool, str, Optional[datetime]]:
    """
    Gate on account health before attempting any credential check.

    Returns (ok, message, locked_until_or_None).
    Side-effect: clears expired locks and commits.
    """
    if not user.is_active:
        return False, "Account is disabled. Please contact an administrator.", None
    if user.is_deleted:
        return False, "Account not found.", None

    if user.is_locked and user.locked_until:
        now_t = now_utc()
        locked_until = ensure_aware(user.locked_until)

        if locked_until > now_t:
            return False, "Account locked.", locked_until

        # Lock expired — clear it
        user.is_locked = False
        user.locked_until = None
        user.failed_login_attempts = 0
        try:
            db.session.commit()
            current_app.logger.info(f"Expired lock cleared for user {user.id}")
            return True, "Account unlocked.", None
        except Exception as error:
            current_app.logger.error(f"Failed to clear expired lock for user {user.id}: {error}")
            db.session.rollback()
            return False, "Account lock status error.", None

    return True, "Account is active.", None

# =============================================================================
# SECTION 8: SMS Dispatch
# =============================================================================

def send_sms_token(phone_number: str, token: str) -> bool:
    """
    Dispatch a 6-digit login token via SMS.
    Returns True on success.

    TODO: Replace the logger stub with your real GSM / HTTP SMS provider.
          E.g. Africa's Talking, Twilio, or a local AT command to the GSM module.
    """
    try:
        # ── Stub: log instead of sending ──────────────────────────────────────
        current_app.logger.info(
            f"[SMS STUB] Token {token} dispatched to {phone_number}"
        )
        # ── Real implementation would go here, e.g.: ──────────────────────────
        # sms_client.send(to=phone_number, body=f"ElectroNora login code: {token}")
        return True
    except Exception as error:
        current_app.logger.error(f"Failed to send SMS token to {phone_number}: {error}")
        return False


# =============================================================================
# SECTION 9: Login Completion
# =============================================================================
def complete_login(user, remember_me=False, login_method='password'):
    """
    Finalise a successful authentication:
    - reset lockout counters
    - call Flask-Login's login_user
    - flash welcome message
    - redirect based on role (admin → /admin, others → dashboard)

    No UserSession row is written (model removed in new schema).
    """
    try:
        current_app.logger.info(f"=== COMPLETING LOGIN FOR USER {user.id} ===")

        # Reset failed-attempt state
        user.failed_login_attempts = 0
        user.is_locked             = False
        user.locked_until          = None

        db.session.commit()

        login_user(user, remember=remember_me,
                   duration=timedelta(days=30 if remember_me else 1))

        current_app.logger.info(f"User {user.id} logged in via {login_method}")

        # Safe next-page redirect
        from urllib.parse import urlparse
        next_page = request.args.get('next')
        if next_page:
            parsed = urlparse(next_page)
            if not parsed.scheme and not parsed.netloc and next_page.startswith('/'):
                return redirect(next_page)
            current_app.logger.warning(f"Blocked open-redirect attempt: {next_page}")

        flash(f'Welcome back, {user.username}!', 'success')
        
        # =============================================================
        # ROLE-BASED REDIRECTION
        # =============================================================
        # Check if user has admin role
        if user.role == 'admin':
            current_app.logger.info(f"Admin user {user.id} ({user.username}) redirecting to main dashboard")
            response = redirect(url_for('main.dashboard')) # Same has normal users but has a link for admins only to admin panel
        else:
            current_app.logger.info(f"Non-admin user {user.id} ({user.username}) redirecting to dashboard")
            response = redirect(url_for('main.dashboard'))
        
        return add_security_headers(response)

    except sa_exc.SQLAlchemyError as db_error:
        current_app.logger.error(
            f"DB error during login for user {user.id}: {db_error}", exc_info=True
        )
        try:
            db.session.rollback()
        except Exception:
            pass
        flash('Database error during login. Please try again.', 'error')
        return redirect(url_for('auth.login'))

    except Exception as error:
        current_app.logger.error(
            f"complete_login error for user {user.id}: {error}", exc_info=True
        )
        try:
            db.session.rollback()
        except Exception:
            pass
        flash('Login completion failed. Please try again.', 'error')
        return redirect(url_for('auth.login'))

# =============================================================================
# SECTION 10: Password Login Handler (COMPLETE WORKING VERSION)
# =============================================================================

def handle_password_login():
    """
    Process a POST to /?type=password (or /login?type=password).

    Lookup order for identifier: username → email → phone.
    Delegates lockout tracking to User.authenticate_with_password().
    """
    from HomeServer.forms.authentication import LoginForm

    try:
        current_app.logger.info("=== PASSWORD LOGIN ATTEMPT ===")

        form = LoginForm()

        csrf_valid, csrf_error = validate_csrf_token()
        if not csrf_valid:
            flash(csrf_error, 'error')
            return redirect(url_for('auth.login', type='password'))

        identifier = (form.identifier.data or request.form.get('identifier', '')).strip()
        password = form.password.data or request.form.get('password', '')
        remember_me = form.remember_me.data or (request.form.get('remember_me') == 'on')

        current_app.logger.info(f"Password login attempt for identifier: '{identifier}'")

        # Rate limiting by IP
        rate_ok, rate_error = rate_limit_check(f"login:{request.remote_addr}", 'login')
        if not rate_ok:
            flash(rate_error, 'warning')
            return redirect(url_for('auth.login', type='password'))

        if not identifier or not password:
            flash('Please enter your identifier and password.', 'error')
            return redirect(url_for('auth.login', type='password'))

        # ── User lookup by username, email, or phone ──────────────────────────
        user = None
        identifier_clean = identifier.strip()
        
        # Clean phone number if it contains spaces/dashes
        phone_clean = re.sub(r'[\s\-\(\)]', '', identifier_clean)
        
        # Try phone match first (normalized)
        try:
            user = User.query.filter(
                User.phone == phone_clean,
                User.is_deleted == False
            ).first()
            
            # Try with + prefix if not found
            if not user and not phone_clean.startswith('+'):
                user = User.query.filter(
                    User.phone == '+' + phone_clean,
                    User.is_deleted == False
                ).first()
            
            # Try without + prefix if has it
            if not user and phone_clean.startswith('+'):
                user = User.query.filter(
                    User.phone == phone_clean[1:],
                    User.is_deleted == False
                ).first()
        except Exception as e:
            current_app.logger.debug(f"Phone search error: {e}")
        
        # Try username or email if phone not found
        if not user:
            user = User.query.filter(
                or_(
                    User.username.ilike(identifier_clean),
                    User.email.ilike(identifier_clean)
                ),
                User.is_deleted == False
            ).first()

        if not user:
            current_app.logger.info(f"User not found for identifier: '{identifier}'")
            flash('Invalid credentials.', 'error')
            return redirect(url_for('auth.login', type='password'))

        # ── Account health ────────────────────────────────────────────────────
        status_ok, status_msg, locked_until = check_account_status(user)
        if not status_ok:
            if locked_until:
                remaining = max(1, int((ensure_aware(locked_until) - now_utc()).total_seconds() / 60))
                if remaining > 60:
                    flash(f'Account locked. Try again in {remaining // 60} hour(s).', 'warning')
                else:
                    flash(f'Account locked. Try again in {remaining} minute(s).', 'warning')
            else:
                flash(status_msg, 'error')
            return redirect(url_for('auth.login', type='password'))

        # ── Check if user has password set ────────────────────────────────────
        if not user.has_password():
            current_app.logger.info(f"User {user.id} has no password set")
            flash('This account has no password set. Please use OTP login or contact the administrator.', 'error')
            return redirect(url_for('auth.login', type='token'))

        # ── Credential check (delegates attempt tracking to model) ────────────
        ok, model_msg, lockout_info = user.authenticate_with_password(password)

        try:
            db.session.commit()
        except Exception as commit_error:
            current_app.logger.error(f"Commit error: {commit_error}")
            db.session.rollback()

        if not ok:
            current_app.logger.info(f"Password login failed for user {user.id}: {model_msg}")
            
            # Distinguish lockout from wrong password
            if user.is_locked_out():
                locked_until = user.locked_until
                if locked_until:
                    locked_until = ensure_aware(locked_until)
                    remaining = max(1, int((locked_until - now_utc()).total_seconds() / 60))
                    if remaining > 60:
                        flash(f'Account locked. Try again in {remaining // 60} hour(s).', 'warning')
                    else:
                        flash(f'Account locked. Try again in {remaining} minute(s).', 'warning')
                else:
                    flash('Account locked. Please try again later.', 'warning')
            else:
                remaining_attempts = user.max_failed_attempts - user.failed_login_attempts
                if remaining_attempts <= 2:
                    flash(
                        f'Invalid credentials. {remaining_attempts} attempt(s) remaining before lockout.',
                        'warning',
                    )
                else:
                    flash('Invalid credentials.', 'error')
            return redirect(url_for('auth.login', type='password'))

        # ── Successful login ──────────────────────────────────────────────────
        current_app.logger.info(f"Password login successful for user {user.id}")
        return complete_login(user, remember_me, 'password')

    except Exception as error:
        current_app.logger.error(f"Password login error: {error}", exc_info=True)
        try:
            db.session.rollback()
        except Exception:
            pass
        flash('An error occurred during login. Please try again.', 'error')
        return redirect(url_for('auth.login', type='password'))


# =============================================================================
# SECTION 11: OTP Login — Step 1: Request Token (PHONE NUMBERS ONLY)
# =============================================================================

def handle_token_login_request():
    """
    Process a POST to /?type=token.
    ONLY accepts phone numbers for OTP login.
    Username and email are NOT allowed for token login.
    """
    from HomeServer.forms.authentication import TokenRequestForm

    try:
        csrf_valid, csrf_error = validate_csrf_token()
        if not csrf_valid:
            flash(csrf_error, 'error')
            return redirect(url_for('auth.login', type='token'))

        form = TokenRequestForm()
        
        # Get the phone number from form
        phone_input = (form.phone.data or request.form.get('phone', '')).strip()
        
        if not phone_input:
            flash('Please enter your phone number for SMS login.', 'error')
            return render_template(
                'auth/login.html',
                auth_type='token',
                form=form,
                csrf_token=generate_csrf(),
            )

        # ── VALIDATE: This MUST be a phone number, not email or username ───────
        # Clean phone number
        phone_clean = re.sub(r'[\s\-\(\)]', '', phone_input)
        
        # Check if it looks like a phone number (starts with + or has 10-15 digits)
        is_phone = False
        if phone_clean.startswith('+') and phone_clean[1:].isdigit():
            is_phone = True
        elif phone_clean.isdigit() and 10 <= len(phone_clean) <= 15:
            is_phone = True
        
        if not is_phone:
            flash('Please enter a valid phone number (e.g., +256706992657). Email addresses are not accepted for SMS login.', 'error')
            return redirect(url_for('auth.login', type='token'))

        # IP-level rate limiting
        rate_ok, rate_error = rate_limit_check(f"token:{request.remote_addr}", 'token')
        if not rate_ok:
            flash(rate_error, 'warning')
            return redirect(url_for('auth.login', type='token'))

        # ── User lookup by PHONE ONLY ─────────────────────────────────────────
        # Try multiple phone formats
        user = None
        phone_variants = [
            phone_clean,                           # +256706992657
            phone_clean.lstrip('+'),               # 256706992657
            '0' + phone_clean.lstrip('+'),         # 0706992657
            '+' + phone_clean.lstrip('+'),         # Ensure leading +
        ]
        
        for variant in set(phone_variants):
            user = User.query.filter(
                User.phone == variant,
                User.is_deleted == False
            ).first()
            if user:
                current_app.logger.info(f"Found user by phone: '{variant}'")
                # Normalize phone if needed
                if user.phone != phone_clean:
                    user.phone = phone_clean
                    db.session.commit()
                break
        
        # ── CHECK 1: User exists with this phone number? ──────────────────────
        if not user:
            flash('No account found with that phone number. Please use password login or register.', 'error')
            return redirect(url_for('auth.login', type='token'))

        # ── CHECK 2: Account locked? ──────────────────────────────────────────
        if user.is_locked_out():
            remaining_seconds = user.get_lockout_remaining_seconds()
            minutes = max(1, (remaining_seconds + 59) // 60)
            flash(f'Account is locked. Please try again in {minutes} minute(s).', 'error')
            return redirect(url_for('auth.login', type='token'))
        
        # ── CHECK 3: Account active? ──────────────────────────────────────────
        if not user.is_active:
            flash('Account is disabled. Please contact an administrator.', 'error')
            return redirect(url_for('auth.login', type='token'))
        
        # ── CHECK 4: OTP enabled? ─────────────────────────────────────────────
        if not user.otp_enabled:
            flash(
                'SMS login is not enabled for this account. '
                'Please use password login instead.',
                'warning'
            )
            return redirect(url_for('auth.login', type='token'))
        
        # ── CHECK 5: Phone verified? ──────────────────────────────────────────
        if not user.phone_verified:
            flash(
                'Your phone number is not verified. '
                'Please use password login or contact support.',
                'warning'
            )
            return redirect(url_for('auth.login', type='token'))

        # ── Per-user OTP rate limit ───────────────────────────────────────────
        can_request, otp_rate_msg = user.can_request_otp()
        if not can_request:
            flash(otp_rate_msg, 'warning')
            return redirect(url_for('auth.login', type='token'))

        # ── Generate OTP + persist as OTPSession ──────────────────────────────
        try:
            otp_plain, otp_hash = user.generate_otp()
            otp_session = OTPSession.create(
                user_id=user.id,
                otp_hash=otp_hash,
                purpose='login',
                expiry_minutes=10,
            )
            user.record_otp_request()
            db.session.add(otp_session)
            db.session.commit()
            session_id = str(otp_session.id)
        except Exception as error:
            db.session.rollback()
            current_app.logger.error(f"OTPSession creation failed for user {user.id}: {error}")
            flash('Session creation failed. Please try again.', 'error')
            return redirect(url_for('auth.login', type='token'))

        # ── SMS dispatch ──────────────────────────────────────────────────────
        if not send_sms_token(user.phone, otp_plain):
            try:
                db.session.delete(otp_session)
                db.session.commit()
            except Exception:
                db.session.rollback()
            flash('Failed to send login code. Please try again or use password login.', 'error')
            return redirect(url_for('auth.login', type='token'))

        current_app.logger.info(f"OTP session {session_id} created for user {user.id}")
        flash(f'Login code sent to {user.phone}', 'success')

        # Store user info in session for verification step
        flask_session['otp_user_id'] = user.id
        flask_session['otp_user_identifier'] = phone_input
        flask_session['otp_sent_at'] = now_utc().timestamp()
        flask_session['otp_session_id'] = session_id

        # Create and populate verify form
        from HomeServer.forms.authentication import TokenVerifyForm
        verify_form = TokenVerifyForm()
        verify_form.session_id.data = session_id

        return render_template(
            'auth/login.html',
            auth_type='token',
            form=verify_form,
            session_id=session_id,
            show_verify=True,
            csrf_token=generate_csrf(),
        )

    except Exception as error:
        current_app.logger.error(f"Token request error: {error}", exc_info=True)
        flash('An error occurred. Please try again.', 'error')
        return redirect(url_for('auth.login', type='token'))
        
# =============================================================================
# SECTION 12: OTP Login — Step 2: Verify Token (CORRECTED for your form)
# =============================================================================

def handle_token_verify():
    """
    Verify the 6-digit code submitted by the user.

    Looks up OTPSession by id, delegates hash comparison to
    User.authenticate_with_otp(), marks the session used, then calls
    complete_login.
    """
    from HomeServer.forms.authentication import TokenVerifyForm

    try:
        form = TokenVerifyForm()
        
        if not form.validate_on_submit():
            # Form validation failed
            for field, errors in form.errors.items():
                for error in errors:
                    flash(f"{field}: {error}", 'error')
            return redirect(url_for('auth.login', type='token'))

        session_id_raw = form.session_id.data.strip()
        token = form.token.data.strip()
        remember_me = form.remember_me.data

        # ── Load OTPSession ───────────────────────────────────────────────────
        try:
            session_id = int(session_id_raw)
            otp_session = OTPSession.query.get(session_id)
        except (ValueError, TypeError):
            flash('Invalid session. Please request a new code.', 'error')
            return redirect(url_for('auth.login', type='token'))
        except sa_exc.SQLAlchemyError as db_err:
            current_app.logger.error(f"DB error loading OTPSession {session_id_raw}: {db_err}")
            flash('Database error. Please try again.', 'error')
            return redirect(url_for('auth.login', type='token'))

        if not otp_session or not otp_session.is_valid():
            flash('Login code has expired or was already used. Please request a new one.', 'error')
            return redirect(url_for('auth.login', type='token'))

        # ── Load linked user ──────────────────────────────────────────────────
        try:
            user = User.query.get(otp_session.user_id)
        except sa_exc.SQLAlchemyError as db_err:
            current_app.logger.error(f"DB error loading user for OTPSession {session_id}: {db_err}")
            flash('Database error. Please try again.', 'error')
            return redirect(url_for('auth.login', type='token'))

        if not user or user.is_deleted or not user.is_active:
            flash('Account not found or inactive.', 'error')
            return redirect(url_for('auth.login', type='token'))

        # ── Account health ────────────────────────────────────────────────────
        if user.is_locked_out():
            remaining_seconds = user.get_lockout_remaining_seconds()
            minutes = max(1, (remaining_seconds + 59) // 60)
            flash(f'Account is temporarily locked. Try again in {minutes} minute(s).', 'warning')
            return redirect(url_for('auth.login', type='token'))

        # ── Delegate to model (tracks failed attempts + lockout) ──────────────
        ok, model_msg, lockout_info = user.authenticate_with_otp(token, otp_session.otp_hash)

        if ok:
            # Mark consumed BEFORE commit — prevents replay on partial failure
            otp_session.mark_used()

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash('Database error. Please try again.', 'error')
            return redirect(url_for('auth.login', type='token'))

        if not ok:
            if user.is_locked_out():
                remaining_seconds = user.get_lockout_remaining_seconds()
                minutes = max(1, (remaining_seconds + 59) // 60)
                flash(f'Account locked due to too many failed attempts. Try again in {minutes} minute(s).', 'warning')
            else:
                remaining_attempts = user.max_failed_attempts - user.failed_login_attempts
                if remaining_attempts <= 2:
                    flash(
                        f'Invalid code. {remaining_attempts} attempt(s) remaining before lockout.',
                        'warning',
                    )
                else:
                    flash('Invalid login code. Please try again.', 'error')
            
            # Re-render verify form so user doesn't need to re-enter phone
            from HomeServer.forms.authentication import TokenVerifyForm
            verify_form = TokenVerifyForm()
            verify_form.session_id.data = session_id_raw
            return render_template(
                'auth/login.html',
                auth_type='token',
                form=verify_form,
                session_id=session_id_raw,
                show_verify=True,
                csrf_token=generate_csrf(),
            )

        # Clear OTP session data
        flask_session.pop('otp_user_id', None)
        flask_session.pop('otp_sent_at', None)
        flask_session.pop('otp_session_id', None)

        return complete_login(user, remember_me, 'sms_token')

    except Exception as error:
        current_app.logger.error(f"Token verification error: {error}", exc_info=True)
        flash('An error occurred. Please try again.', 'error')
        return redirect(url_for('auth.login', type='token'))


# =============================================================================
# SECTION 13: OTP Resend Handler (UPDATED)
# =============================================================================

def handle_token_resend():
    """
    Re-generate and re-send the OTP, enforcing a 30-second cooldown
    and the per-user hourly rate limit (User.can_request_otp).
    """
    try:
        csrf_valid, csrf_error = validate_csrf_token()
        if not csrf_valid:
            return jsonify({'success': False, 'error': csrf_error}), 400

        session_id_raw = request.form.get('session_id', '').strip()
        if not session_id_raw:
            return jsonify({'success': False, 'error': 'Invalid session.'}), 400

        # ── Load existing OTPSession ──────────────────────────────────────────
        try:
            session_id = int(session_id_raw)
            otp_session = OTPSession.query.get(session_id)
        except (ValueError, TypeError):
            return jsonify({'success': False, 'error': 'Invalid session. Please start over.'}), 400

        if not otp_session:
            return jsonify({'success': False, 'error': 'Session not found. Please request a new code.'}), 400

        # ── Load user ─────────────────────────────────────────────────────────
        user = User.query.get(otp_session.user_id)
        if not user or user.is_deleted or not user.is_active:
            return jsonify({'success': False, 'error': 'Account not found or inactive.'}), 400

        # ── Check lockout ─────────────────────────────────────────────────────
        if user.is_locked_out():
            remaining_seconds = user.get_lockout_remaining_seconds()
            minutes = max(1, (remaining_seconds + 59) // 60)
            return jsonify({
                'success': False,
                'error': f'Account is locked. Please try again in {minutes} minute(s).'
            }), 403

        # ── 30-second cooldown (use last_otp_request on User model) ──────────
        can_request, otp_rate_msg = user.can_request_otp()
        if not can_request:
            return jsonify({'success': False, 'error': otp_rate_msg}), 429

        # ── Invalidate old session, create new one ────────────────────────────
        try:
            otp_session.mark_used()       # expire the old session immediately
            new_otp, new_hash = user.generate_otp()
            new_session = OTPSession.create(
                user_id=user.id,
                otp_hash=new_hash,
                purpose='login',
                expiry_minutes=10,
            )
            user.record_otp_request()
            db.session.add(new_session)
            db.session.commit()
            new_session_id = str(new_session.id)
        except Exception as error:
            db.session.rollback()
            current_app.logger.error(f"OTP resend session creation failed: {error}")
            return jsonify({'success': False, 'error': 'Failed to create a new session. Please start over.'}), 500

        if not send_sms_token(user.phone, new_otp):
            # Roll back new session if SMS fails
            try:
                db.session.delete(new_session)
                db.session.commit()
            except Exception:
                db.session.rollback()
            return jsonify({'success': False, 'error': 'Failed to send SMS. Please try again.'}), 500

        # Update session storage
        flask_session['otp_session_id'] = new_session_id

        return jsonify({
            'success': True,
            'message': 'New login code sent to your phone.',
            'session_id': new_session_id
        })

    except Exception as error:
        current_app.logger.error(f"Token resend error: {error}", exc_info=True)
        return jsonify({'success': False, 'error': 'An error occurred. Please try again.'}), 500