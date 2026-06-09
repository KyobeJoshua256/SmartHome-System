"""
login.py — Auth Blueprint Route Handlers (ElectroNora Smart Home)
Flask login handlers ONLY.
No password hashing, no OTP generation, no DB queries.
This layer: validates requests → delegates to authentication.py → renders/redirects.
"""
import time
import re
from typing import Any
from flask import (
    Blueprint, render_template, redirect, url_for, flash,
    request, session as flask_session, current_app, make_response,
)
from flask_login import login_required, current_user
from flask_wtf.csrf import generate_csrf
from flask import jsonify
from sqlalchemy import exc, or_
from HomeServer.models.users import User, now_utc, ensure_aware
from HomeServer import database as db
from HomeServer.forms.authentication import LoginForm, TokenRequestForm, TokenVerifyForm
from HomeServer.routes.auth.authentication import (
    cleanup_expired_sessions,
    validate_csrf_token,
    add_security_headers,
    to_uganda_time,
    complete_login,
    handle_password_login,
    handle_token_login_request,
    handle_token_verify,
    handle_token_resend,
    find_active_user,
    MAX_ATTEMPTS,
)
from HomeServer.routes.users.dashboard_data import get_user_dashboard_data
# =============================================================================
# Helper: Normalize phone number to E.164 format
# =============================================================================

def normalize_phone(phone: str) -> str:
    """Normalize phone number to E.164 format (+XXXXXXXXXXX)."""
    if not phone:
        return phone
    # Remove spaces, dashes, parentheses
    clean = re.sub(r'[\s\-\(\)]', '', phone)
    # Ensure + prefix
    if clean and not clean.startswith('+'):
        clean = '+' + clean
    return clean


def find_user_by_phone(phone: str):
    """Find user by phone number with multiple format attempts."""
    if not phone:
        return None
    
    normalized = normalize_phone(phone)
    variants = [
        normalized,                           # +256706992657
        normalized.lstrip('+'),               # 256706992657
        '0' + normalized.lstrip('+'),         # 0706992657
        normalized.lstrip('+').lstrip('0'),   # 256706992657
    ]
    
    for variant in set(variants):
        user = User.query.filter(
            User.phone == variant,
            User.is_deleted == False
        ).first()
        if user:
            # Update to normalized format if different
            if user.phone != normalized:
                user.phone = normalized
                db.session.commit()
            return user
    
    return None


# =============================================================================
# Blueprint Definitions
# =============================================================================
auth_bp = Blueprint('auth', __name__)
main_bp = Blueprint('main', __name__)
api_bp = Blueprint('api', __name__)


# =============================================================================
# Authentication Routes (auth_bp)
# =============================================================================

@auth_bp.route('/', methods=['GET', 'POST'])
def login() -> Any:
    """Show the login page (GET) or dispatch credentials (POST)."""
    try:
        if int(time.time()) % 300 == 0:
            cleanup_expired_sessions()
            
        if current_user.is_authenticated:
            return redirect(url_for('main.dashboard'))

        auth_type = request.args.get('type', 'password')

        if request.method == 'GET':
            if auth_type == 'token':
                form = TokenRequestForm()
            else:
                form = LoginForm()

            return render_template(
                'auth/login.html',
                auth_type=auth_type,
                form=form,
                csrf_token=generate_csrf(),
            )

        if auth_type == 'password':
            return handle_password_login()
        if auth_type == 'token':
            return handle_token_login_request()

        flash('Invalid authentication type.', 'error')
        return redirect(url_for('auth.login'))

    except Exception as error:
        current_app.logger.error(f"Login route error: {error}", exc_info=True)
        flash('An error occurred. Please try again.', 'error')
        return redirect(url_for('auth.login'))


@auth_bp.route('/auth/token/verify', methods=['POST'])
def token_verify() -> Any:
    """Verify the 6-digit OTP submitted by the user."""
    return handle_token_verify()


@auth_bp.route('/auth/token/resend', methods=['POST'])
def token_resend() -> Any:
    """Re-generate and re-send the OTP (30-second cooldown enforced)."""
    return handle_token_resend()


@auth_bp.route('/auth/logout')
@login_required
def logout() -> Any:
    """Revoke the active session, clear cookies, and redirect to login."""
    user_id = current_user.id
    username = current_user.username
    current_app.logger.info(f"=== STARTING LOGOUT FOR USER {user_id} ({username}) ===")
    try:
        flask_session.clear()
        from flask_login import logout_user
        logout_user()

        response = make_response(redirect(url_for('auth.login')))

        cookies_to_clear = {
            'session', 'remember_token', 'session_id',
            'csrf_token', 'user_id', 'username', 'flask-session',
            current_app.config.get('SESSION_COOKIE_NAME', 'session'),
        }
        for name in cookies_to_clear:
            response.delete_cookie(
                name, path='/', secure=request.is_secure,
                httponly=True, samesite='Lax',
            )

        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        response = add_security_headers(response)

        flash('You have been logged out successfully.', 'info')
        current_app.logger.info(f"=== LOGOUT COMPLETED FOR USER {user_id} ({username}) ===")
        return response

    except Exception as error:
        current_app.logger.error(f"Logout error: {error}", exc_info=True)
        try:
            db.session.rollback()
        except Exception:
            pass
        flask_session.clear()
        from flask_login import logout_user
        logout_user()
        return redirect(url_for('auth.login'))


# =============================================================================
# Dashboard Route (main_bp)
# =============================================================================
@main_bp.route('/dashboard')
@login_required
def dashboard() -> Any:
    """Render the personalised user dashboard."""
    try:
        dashboard_data = get_user_dashboard_data(current_user)
        dashboard_data['csrfToken'] = generate_csrf()
        response = make_response(render_template('users/dashboard.html', **dashboard_data))
        response = add_security_headers(response)
        
        return response
        
    except Exception as error:
        current_app.logger.error(f"Dashboard error: {error}", exc_info=True)
        flash('An error occurred while loading the dashboard. Please try again.', 'error')
        return redirect(url_for('auth.login'))


# =============================================================================
# Real-time identifier check — called as user types (debounced by JS)
# =============================================================================

@auth_bp.route('/auth/api/check-identifier', methods=['GET'])
def check_identifier():
    """Lightweight real-time lookup called on every debounced keystroke."""
    try:
        raw = (request.args.get('q') or '').strip()
        tab_type = request.args.get('tab', 'password')  # 'password' or 'token'
        
        if not raw or len(raw) < 3:
            return jsonify({'status': 'too_short'})

        current_app.logger.info(f"Checking identifier: '{raw}' for tab: {tab_type}")
        
        # =============================================================
        # TOKEN TAB: ONLY PHONE NUMBERS ALLOWED
        # =============================================================
        if tab_type == 'token':
            # Clean the input to check if it's a phone number
            phone_clean = re.sub(r'[\s\-\(\)]', '', raw)
            
            # Check if it looks like a valid phone number
            is_phone = False
            
            # Format 1: +256706992657 (E.164)
            if phone_clean.startswith('+') and phone_clean[1:].isdigit():
                is_phone = True
            # Format 2: 256706992657 (without +)
            elif phone_clean.isdigit() and 10 <= len(phone_clean) <= 15:
                is_phone = True
            # Format 3: 0706992657 (local with leading zero)
            elif phone_clean.isdigit() and len(phone_clean) == 10:
                is_phone = True
            
            if not is_phone:
                return jsonify({
                    'status': 'invalid_for_token',
                    'message': 'Please enter a valid phone number for SMS login (e.g., +256706992657)'
                })
            
            # Search for user by phone number only
            user = None
            phone_variants = [
                phone_clean,                           # +256706992657 or 0706992657
                phone_clean.lstrip('+'),               # 256706992657
                '+' + phone_clean.lstrip('+'),         # Ensure leading +
                '0' + phone_clean.lstrip('+').lstrip('0'),  # 0706992657
            ]
            
            for variant in set(phone_variants):
                user = User.query.filter(
                    User.phone == variant,
                    User.is_deleted == False
                ).first()
                if user:
                    current_app.logger.info(f"[TOKEN TAB] Found user by phone: '{variant}' -> {user.username}")
                    # Normalize phone in database if needed
                    if user.phone != phone_clean:
                        user.phone = phone_clean
                        db.session.commit()
                    break
            
            if not user:
                return jsonify({
                    'status': 'not_found',
                    'message': 'No account found with this phone number. Please use password login.'
                })
            
            # Check account status for token tab
            if not user.is_active:
                return jsonify({
                    'status': 'inactive',
                    'message': 'Account is disabled. Please contact administrator.'
                })
            
            is_locked = user.is_locked_out()
            
            if is_locked:
                remaining_seconds = user.get_lockout_remaining_seconds()
                locked_until = user.get_lockout_end_time_ms()
                total_duration = user.lockout_duration_minutes * 60
                
                return jsonify({
                    'status': 'locked',
                    'username': user.username,
                    'is_locked': True,
                    'locked_until_ms': locked_until,
                    'remaining_seconds': remaining_seconds,
                    'total_duration_seconds': total_duration,
                    'message': f'Account locked. Please wait {max(1, (remaining_seconds + 59) // 60)} minute(s).'
                })
            
            # Check OTP availability for token tab
            can_otp = False
            otp_blocked_reason = None
            otp_cooldown = 0
            
            if not user.phone_verified:
                can_otp = False
                otp_blocked_reason = 'Phone number not verified. Please use password login.'
            elif not user.otp_enabled:
                can_otp = False
                otp_blocked_reason = 'SMS login is not enabled for this account. Use password login.'
            else:
                can_otp, otp_blocked_reason = user.can_request_otp()
                if not can_otp and user.last_otp_request:
                    secs_since = (now_utc() - ensure_aware(user.last_otp_request)).total_seconds()
                    if secs_since < 30:
                        otp_cooldown = int(30 - secs_since)
                        otp_blocked_reason = f'Please wait {otp_cooldown} seconds before requesting another code.'
                    elif user.otp_request_count >= user.max_otp_requests_per_hour:
                        if secs_since < 3600:
                            otp_cooldown = int(3600 - secs_since)
                            otp_blocked_reason = f'Rate limit reached. Try again in {otp_cooldown // 60} minutes.'
            
            if not can_otp:
                return jsonify({
                    'status': 'otp_unavailable',
                    'username': user.username,
                    'reason': otp_blocked_reason,
                    'otp_cooldown': otp_cooldown,
                    'has_password': user.has_password(),
                    'message': otp_blocked_reason
                })
            
            # Success - valid phone with OTP enabled
            return jsonify({
                'status': 'found',
                'username': user.username,
                'has_password': user.has_password(),
                'otp_enabled': user.otp_enabled,
                'phone_verified': user.phone_verified,
                'can_otp': True,
                'message': f'Account found: {user.username} — ready to receive SMS'
            })
        
        # =============================================================
        # PASSWORD TAB: ACCEPTS USERNAME, EMAIL, OR PHONE
        # =============================================================
        user = None
        
        # METHOD 1: Try phone number (remove spaces, dashes, parentheses)
        phone_clean = re.sub(r'[\s\-\(\)]', '', raw)
        
        if phone_clean.isdigit() or (phone_clean.startswith('+') and phone_clean[1:].isdigit()):
            search_variants = [
                phone_clean,
                phone_clean.lstrip('+'),
                '+' + phone_clean.lstrip('+'),
                '0' + phone_clean.lstrip('+').lstrip('0'),
            ]
            
            for variant in set(search_variants):
                user = User.query.filter(
                    User.phone == variant,
                    User.is_deleted == False
                ).first()
                if user:
                    current_app.logger.info(f"[PASSWORD TAB] Found user by phone: '{variant}' -> {user.username}")
                    if user.phone != phone_clean:
                        user.phone = phone_clean
                        db.session.commit()
                    break
        
        # METHOD 2: Try email (case-insensitive)
        if not user and '@' in raw:
            user = User.query.filter(
                User.email.ilike(raw),
                User.is_deleted == False
            ).first()
            if user:
                current_app.logger.info(f"[PASSWORD TAB] Found user by email: '{raw}' -> {user.username}")
        
        # METHOD 3: Try username (case-insensitive)
        if not user:
            user = User.query.filter(
                User.username.ilike(raw),
                User.is_deleted == False
            ).first()
            if user:
                current_app.logger.info(f"[PASSWORD TAB] Found user by username: '{raw}' -> {user.username}")
        
        # No user found
        if not user:
            current_app.logger.info(f"[PASSWORD TAB] No user found for: '{raw}'")
            return jsonify({'status': 'not_found'})

        # Check account status
        if not user.is_active:
            return jsonify({'status': 'inactive'})

        is_locked = user.is_locked_out()
        locked_until_ms = None
        remaining_seconds = 0
        total_duration_seconds = 0

        if is_locked and user.locked_until:
            lu = ensure_aware(user.locked_until)
            remaining_seconds = max(0, int((lu - now_utc()).total_seconds()))
            total_duration_seconds = user.lockout_duration_minutes * 60
            locked_until_ms = int(lu.timestamp() * 1000)

        # Return response for password tab
        response_data = {
            'status': 'locked' if is_locked else 'found',
            'username': user.username,
            'has_password': user.has_password(),
            'otp_enabled': user.otp_enabled,
            'phone_verified': user.phone_verified,
            'is_locked': is_locked,
            'locked_until_ms': locked_until_ms,
            'remaining_seconds': remaining_seconds,
            'total_duration_seconds': total_duration_seconds,
            'failed_attempts': user.failed_login_attempts,
            'max_attempts': user.max_failed_attempts,
            'remaining_attempts': max(0, user.max_failed_attempts - user.failed_login_attempts),
        }
        
        current_app.logger.info(f"[PASSWORD TAB] Returning response for {user.username}: has_password={user.has_password()}")
        return jsonify(response_data)

    except Exception as error:
        current_app.logger.error(f"check-identifier error: {error}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(error)}), 500
        
# =============================================================================
# API Routes for User Status Checks
# =============================================================================

@auth_bp.route('/auth/api/check-user/<identifier>', methods=['GET'])
@auth_bp.route('/auth/api/check-user', methods=['POST'])
def check_user_status(identifier=None):
    """Universal API endpoint to check user existence by username, email, or phone."""
    try:
        if request.method == 'POST':
            data = request.get_json()
            if not data:
                return jsonify({'exists': False, 'error': 'Invalid request data'}), 400
            identifier = data.get('identifier', '').strip()
        elif not identifier:
            identifier = request.args.get('identifier', '').strip()
        
        if not identifier:
            return jsonify({'exists': False, 'error': 'No identifier provided'}), 400
        
        if request.method == 'POST':
            csrf_valid, csrf_error = validate_csrf_token()
            if not csrf_valid:
                return jsonify({'exists': False, 'error': csrf_error}), 400
        
        # Find user
        user = None
        identifier_clean = identifier.strip()
        
        # Try phone search with normalization
        phone_clean = normalize_phone(identifier_clean)
        user = find_user_by_phone(phone_clean)
        
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
            return jsonify({
                'exists': False,
                'identifier': identifier,
                'message': 'No account found'
            })
        
        # Get lockout status
        is_locked = user.is_locked_out()
        locked_until = None
        remaining_seconds = 0
        total_duration_seconds = 0
        locked_until_ms = None
        
        if is_locked and user.locked_until:
            locked_until = ensure_aware(user.locked_until)
            now = now_utc()
            remaining_seconds = max(0, int((locked_until - now).total_seconds()))
            total_duration_seconds = user.lockout_duration_minutes * 60
            locked_until_ms = int(locked_until.timestamp() * 1000)
        
        # Check OTP availability
        can_request_otp = False
        otp_blocked_reason = None
        remaining_cooldown = 0
        
        if not is_locked and user.is_active:
            if not user.phone_verified:
                can_request_otp = False
                otp_blocked_reason = 'Phone number not verified'
            elif not user.otp_enabled:
                can_request_otp = False
                otp_blocked_reason = 'OTP not enabled for this account'
            else:
                can_request, reason = user.can_request_otp()
                can_request_otp = can_request
                otp_blocked_reason = reason
                
                if not can_request and user.last_otp_request:
                    last_request = ensure_aware(user.last_otp_request)
                    time_since_last = (now_utc() - last_request).total_seconds()
                    if time_since_last < 30:
                        remaining_cooldown = int(30 - time_since_last)
                    elif user.otp_request_count >= user.max_otp_requests_per_hour:
                        remaining_cooldown = 3600 - int(time_since_last)
        
        response_data = {
            'exists': True,
            'user_id': user.id,
            'username': user.username,
            'phone': user.get_formatted_phone(),
            'email': user.email,
            'is_active': user.is_active,
            'is_deleted': user.is_deleted,
            'is_locked': is_locked,
            'has_password': user.has_password(),
            'otp_enabled': user.otp_enabled,
            'phone_verified': user.phone_verified,
            'locked_until': locked_until.isoformat() if locked_until else None,
            'locked_until_ms': locked_until_ms,
            'remaining_seconds': remaining_seconds,
            'total_duration_seconds': total_duration_seconds,
            'failed_attempts': user.failed_login_attempts,
            'max_attempts': user.max_failed_attempts,
            'remaining_attempts': max(0, user.max_failed_attempts - user.failed_login_attempts),
            'can_request_otp': can_request_otp,
            'otp_blocked_reason': otp_blocked_reason,
            'remaining_cooldown': remaining_cooldown
        }
        
        return jsonify(response_data)
        
    except Exception as error:
        current_app.logger.error(f"User status check error: {error}", exc_info=True)
        return jsonify({'exists': False, 'error': 'An internal error occurred'}), 500


@auth_bp.route('/auth/api/lockout-status', methods=['GET'])
def get_lockout_status():
    """Get lockout status for the currently locked account from session."""
    try:
        locked_identifier = flask_session.get('locked_identifier')
        locked_until_ms = flask_session.get('locked_until_ms')
        
        if not locked_identifier or not locked_until_ms:
            return jsonify({'locked': False})
        
        current_time_ms = int(time.time() * 1000)
        if locked_until_ms <= current_time_ms:
            flask_session.pop('locked_identifier', None)
            flask_session.pop('locked_until_ms', None)
            flask_session.pop('lockout_duration_seconds', None)
            return jsonify({'locked': False, 'expired': True})
        
        remaining_seconds = max(0, (locked_until_ms - current_time_ms) // 1000)
        
        return jsonify({
            'locked': True,
            'identifier': locked_identifier,
            'locked_until_ms': locked_until_ms,
            'remaining_seconds': remaining_seconds,
            'total_duration_seconds': flask_session.get('lockout_duration_seconds', 1800)
        })
        
    except Exception as error:
        current_app.logger.error(f"Lockout status error: {error}", exc_info=True)
        return jsonify({'locked': False, 'error': str(error)}), 500


@auth_bp.route('/auth/api/set-lockout', methods=['POST'])
def set_lockout_session():
    """Store lockout information in session for persistence across page refreshes."""
    try:
        csrf_valid, csrf_error = validate_csrf_token()
        if not csrf_valid:
            return jsonify({'error': csrf_error}), 400
        
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Invalid request data'}), 400
            
        identifier = data.get('identifier', '').strip()
        locked_until_ms = data.get('locked_until_ms')
        duration_seconds = data.get('duration_seconds', 1800)
        
        if identifier and locked_until_ms:
            flask_session['locked_identifier'] = identifier
            flask_session['locked_until_ms'] = locked_until_ms
            flask_session['lockout_duration_seconds'] = duration_seconds
            return jsonify({'success': True})
        
        return jsonify({'error': 'Missing required fields'}), 400
        
    except Exception as error:
        current_app.logger.error(f"Set lockout session error: {error}", exc_info=True)
        return jsonify({'error': str(error)}), 500