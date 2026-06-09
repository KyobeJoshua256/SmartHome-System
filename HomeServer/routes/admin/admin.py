"""
HomeServer/admin.py
===================
Flask-Admin setup for the EBS HUB smart home system.

Structure
---------
1.  Imports
2.  Helpers
3.  Access-control mixin   (AdminAccessMixin)
4.  Audit mixin            (AuditMixin)
5.  Secure base view       (SecureModelView)
6.  User admin view        (UserAdmin)
7.  OTP session admin view (OTPSessionAdmin)
8.  Dashboard              (SecureAdminIndexView)
9.  Factory                (init_admin)
"""

# =============================================================================
# 1. Imports
# =============================================================================

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import logging
import re

from flask import flash, redirect, request, url_for
from flask_admin import Admin, AdminIndexView, expose
from flask_admin.actions import action
from flask_admin.contrib.sqla import ModelView
from flask_admin.theme import Bootstrap4Theme
from flask_login import current_user
from markupsafe import Markup, escape
from sqlalchemy import func, inspect as sa_inspect, or_, case
from wtforms import PasswordField, ValidationError
from wtforms.validators import EqualTo, Length, Optional as OptionalValidator

from HomeServer.models.users import OTPSession, User, UserRole, ensure_aware, now_utc

logger = logging.getLogger(__name__)


# =============================================================================
# 2. Helpers
# =============================================================================

def _get_pk(model) -> str:
    """Return the primary-key value(s) of a SQLAlchemy model instance."""
    try:
        mapper = sa_inspect(model.__class__)
        pks = [getattr(model, col.key) for col in mapper.mapper.primary_key]
        return str(pks[0]) if len(pks) == 1 else str(tuple(pks))
    except Exception:
        return "unknown"


def _localize(dt: datetime) -> datetime:
    """Convert a naive-or-aware datetime to the current admin user's timezone."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    tz_name = getattr(current_user, "timezone", None)
    if tz_name:
        try:
            dt = dt.astimezone(ZoneInfo(tz_name))
        except Exception:
            pass
    return dt


def _fmt_dt(dt: datetime) -> Markup:
    """Render a datetime as a localised <time> element."""
    if dt is None:
        return Markup('<span class="text-muted">—</span>')
    local = _localize(dt)
    label = local.strftime("%Y-%m-%d %H:%M %Z")
    iso = local.isoformat()
    return Markup(f'<time datetime="{iso}" title="{iso}">{label}</time>')


def _fmt_bool(val) -> Markup:
    """Render a boolean as a coloured Font Awesome icon."""
    if val is None:
        return Markup('<span class="text-muted">—</span>')
    return (
        Markup('<i class="fas fa-check-circle text-success"></i>')
        if val else
        Markup('<i class="fas fa-times-circle text-danger"></i>')
    )


# =============================================================================
# 3. Access-Control Mixin
# =============================================================================

class AdminAccessMixin:
    """
    Plugs into any Flask-Admin view.
    Grants access only to authenticated, active, unlocked, non-deleted admins.
    """

    def is_accessible(self) -> bool:
        if not current_user.is_authenticated:
            return False
        return (
            current_user.role == UserRole.ADMIN.value
            and current_user.is_active
            and not current_user.is_locked
            and not current_user.is_deleted
        )

    def inaccessible_callback(self, name, **kwargs):
        ip = request.remote_addr

        if not current_user.is_authenticated:
            flash("Please log in first.", "warning")
            return redirect(url_for("auth.login"))

        if current_user.role != UserRole.ADMIN.value:
            logger.warning(
                "Non-admin user '%s' attempted admin access from %s",
                current_user.username,
                ip,
            )
            flash("Administrator privileges required.", "danger")
        elif not current_user.is_active:
            flash("Your account is inactive.", "danger")
        elif current_user.is_locked:
            flash("Your account has been locked.", "danger")
        elif current_user.is_deleted:
            flash("Account no longer exists.", "danger")

        return redirect(url_for("main.index"))


# =============================================================================
# 4. Audit Mixin
# =============================================================================

class AuditMixin:
    """
    Emits structured [AUDIT] log lines on every create, update, and delete.
    Compatible with log aggregators (Loki, CloudWatch, etc.).
    """

    def _audit(self, action_name: str, model) -> None:
        logger.info(
            "[AUDIT] action=%s table=%s pk=%s user=%s ip=%s",
            action_name,
            model.__class__.__tablename__,
            _get_pk(model),
            getattr(current_user, "username", "unknown"),
            request.remote_addr,
        )

    def after_model_change(self, form, model, is_created: bool) -> None:
        self._audit("CREATE" if is_created else "UPDATE", model)

    def after_model_delete(self, model) -> None:
        self._audit("DELETE", model)


# =============================================================================
# 5. Secure Base Model View
# =============================================================================

class SecureModelView(AuditMixin, AdminAccessMixin, ModelView):
    """
    Base view inherited by all model-specific admin views.

    Provides
    --------
    - Hard deletes with a PK-confirmation guard (opt-out via skip_delete_confirm).
    - Sensitive column exclusion across list, detail, and form.
    - Localised datetime formatters for created_at / updated_at.
    - Model icon / name injected into every template.
    """

    # Capabilities
    can_view_details = True
    can_export = True
    can_create = True
    can_edit = True
    can_delete = True
    details_modal = True
    # edit_modal = False
    # create_modal = False
    page_size = 50
    export_max_rows = 0
    export_types = ["csv", "xlsx"]

    # Sensitive columns — never exposed in any view or form
    _SENSITIVE = ["password_hash", "api_key", "secret_key", "token", "otp_secret"]
    column_exclude_list = _SENSITIVE
    column_details_exclude_list = _SENSITIVE
    form_excluded_columns = _SENSITIVE

    # Subclass overrides
    model_icon = "fas fa-microchip"
    column_searchable_list = []
    column_filters = []
    column_sortable_list = []
    action_disallowed_list = []
    skip_delete_confirm = False

    # Datetime formatters
    def _col_fmt_dt(self, ctx, model, name):
        return _fmt_dt(getattr(model, name, None))

    column_formatters = {
        "created_at": _col_fmt_dt,
        "updated_at": _col_fmt_dt,
    }
    column_formatters_detail = {
        "created_at": _col_fmt_dt,
        "updated_at": _col_fmt_dt,
    }

    def on_model_delete(self, model) -> None:
        if self.skip_delete_confirm:
            return
        confirm = request.form.get("delete_confirm", "").strip()
        expected = _get_pk(model)
        if confirm != expected:
            raise ValidationError(
                f"Hard-delete aborted. Type '{expected}' in the confirm field "
                "to permanently remove this record."
            )

    def render(self, template, **kwargs):
        kwargs.setdefault("model_name", self.name)
        kwargs.setdefault("model_icon", self.model_icon)
        return super().render(template, **kwargs)


# =============================================================================
# 6. User Admin View
# =============================================================================
class UserAdmin(AuditMixin, AdminAccessMixin, ModelView):
    """
    Full-featured admin view for the User model.
    Uses the User model's built-in validation methods.
    """

    name = "Users"
    model_icon = "fas fa-users"

    # ===== ADD YOUR CUSTOM TEMPLATES =====
    create_template = 'admin/user/create.html'
    edit_template = 'admin/user/edit.html'

    # Capabilities
    can_view_details = True
    can_export = True
    can_create = True
    can_edit = True
    can_delete = True
    details_modal = True
    edit_modal = True
    page_size = 50
    export_types = ["csv", "xlsx"]
    export_max_rows = 0

    # Sensitive / internal columns
    _SENSITIVE = ["password_hash"]
    column_exclude_list = _SENSITIVE
    column_details_exclude_list = _SENSITIVE
    form_excluded_columns = _SENSITIVE + [
        "created_at", "updated_at",
        "failed_login_attempts", "locked_until",
        "otp_request_count", "last_otp_request",
        "last_password_change","conversations",
        "messages_sent",
    ]

    # Column list
    column_list = [
        "id", "username", "phone", "email", "role",
        "_status", "_lockout",
        "otp_enabled", "phone_verified",
        "timezone", "created_at", "updated_at",
    ]

    column_labels = {
        "id": "ID",
        "username": "Username",
        "phone": "Phone (E.164)",
        "email": "Email",
        "role": "Role",
        "_status": "Status",
        "_lockout": "Lockout",
        "otp_enabled": "OTP",
        "phone_verified": "Phone",
        "timezone": "Timezone",
        "created_at": "Created",
        "updated_at": "Updated",
    }

    column_details_list = [
        "id", "username", "phone", "email", "role",
        "is_active", "is_locked", "is_deleted",
        "failed_login_attempts", "max_failed_attempts",
        "locked_until", "lockout_duration_minutes",
        "otp_enabled", "otp_request_count", "last_otp_request",
        "max_otp_requests_per_hour", "phone_verified",
        "avatar_url", "timezone",
        "receive_push", "mute_notifications",
        "force_password_reset", "last_password_change",
        "created_at", "updated_at",
    ]

    # Search, filter, sort
    column_searchable_list = ["username", "phone", "email", "role"]
    column_filters = [
        "role", "is_active", "is_locked", "is_deleted",
        "otp_enabled", "phone_verified",
        "force_password_reset", "timezone",
    ]
    column_sortable_list = [
        "id", "username", "phone", "email", "role",
        "is_active", "is_locked", "created_at", "updated_at",
    ]

    form_create_rules = (
        'username',
        'phone', 
        'email',
        'password',
        'password_confirm',
        'role',
        'otp_enabled',
        'phone_verified',
        'is_active',
        'timezone',
        'avatar_url',
        'receive_push',
        'mute_notifications',
        'force_password_reset',
        'max_failed_attempts',
        'lockout_duration_minutes',
        'max_otp_requests_per_hour'
    )
    
    # Also define edit form order if different
    form_edit_rules = (
        'username',
        'phone',
        'email', 
        'password',
        'password_confirm',
        'role',
        'otp_enabled',
        'phone_verified', 
        'is_active',
        'is_locked',
        'timezone',
        'avatar_url',
        'receive_push',
        'mute_notifications',
        'force_password_reset'
        # Note: Password fields should be editable but can be left blank
    )

    # ------------------------------------------------------------------
    # Formatters
    # ------------------------------------------------------------------

    def _fmt_status(self, ctx, model, name) -> Markup:
        badges = []
        if model.is_deleted:
            badges.append('<span class="badge badge-secondary">Deleted</span>')
        elif model.is_locked:
            remaining = model.get_lockout_remaining_seconds()
            if remaining > 0:
                mins = max(1, (remaining + 59) // 60)
                badges.append(
                    f'<span class="badge badge-danger" '
                    f'title="Unlocks in ~{mins} min">Locked</span>'
                )
            else:
                badges.append('<span class="badge badge-warning">Locked (expired)</span>')
        elif not model.is_active:
            badges.append('<span class="badge badge-warning">Inactive</span>')
        else:
            badges.append('<span class="badge badge-success">Active</span>')
        if model.force_password_reset:
            badges.append('<span class="badge badge-info">Force Reset</span>')
        return Markup(" ".join(badges))

    def _fmt_lockout(self, ctx, model, name) -> Markup:
        if model.is_locked:
            mins = max(0, (model.get_lockout_remaining_seconds() + 59) // 60)
            return Markup(
                f'<span class="text-danger">'
                f'<i class="fas fa-lock"></i> ~{mins} min remaining</span>'
            )
        if model.failed_login_attempts:
            pct = int(
                model.failed_login_attempts
                / max(model.max_failed_attempts, 1)
                * 100
            )
            color = "warning" if pct >= 60 else "info"
            return Markup(
                f'<div class="progress" style="min-width:80px;height:18px" '
                f'title="{model.failed_login_attempts}/{model.max_failed_attempts} attempts">'
                f'<div class="progress-bar bg-{color}" style="width:{pct}%">'
                f'{model.failed_login_attempts}/{model.max_failed_attempts}'
                f'</div></div>'
            )
        return Markup('<span class="text-muted">—</span>')

    def _fmt_role(self, ctx, model, name) -> Markup:
        is_admin = model.role == UserRole.ADMIN.value
        color = "danger" if is_admin else "primary"
        icon = "fa-user-shield" if is_admin else "fa-user-circle"
        return Markup(
            f'<span class="badge badge-{color}">'
            f'<i class="fas {icon}"></i> {model.role.title()}</span>'
        )

    def _fmt_bool_col(self, ctx, model, name) -> Markup:
        return _fmt_bool(getattr(model, name, None))

    def _fmt_created(self, ctx, model, name) -> Markup:
        return _fmt_dt(model.created_at)

    def _fmt_updated(self, ctx, model, name) -> Markup:
        return _fmt_dt(model.updated_at)

    column_formatters = {
        "_status":      _fmt_status,
        "_lockout":     _fmt_lockout,
        "role":         _fmt_role,
        "otp_enabled":  _fmt_bool_col,
        "phone_verified": _fmt_bool_col,
        "created_at":   _fmt_created,
        "updated_at":   _fmt_updated,
    }

    column_formatters_detail = {
        "role":                 _fmt_role,
        "otp_enabled":          _fmt_bool_col,
        "phone_verified":       _fmt_bool_col,
        "is_active":            _fmt_bool_col,
        "is_locked":            _fmt_bool_col,
        "is_deleted":           _fmt_bool_col,
        "force_password_reset": _fmt_bool_col,
        "receive_push":         _fmt_bool_col,
        "mute_notifications":   _fmt_bool_col,
        "created_at":           _fmt_created,
        "updated_at":           _fmt_updated,
        "last_password_change": lambda s, c, m, n: _fmt_dt(m.last_password_change),
        "last_otp_request":     lambda s, c, m, n: _fmt_dt(m.last_otp_request),
        "locked_until":         lambda s, c, m, n: _fmt_dt(m.locked_until),
    }

    # ------------------------------------------------------------------ #
    # Forms — Use model validators instead of repeating validation logic
    # ------------------------------------------------------------------ #
    form_extra_fields = {
        "password": PasswordField(
            "Password",
            validators=[
                OptionalValidator(),
                Length(min=6, message="Password must be at least 6 characters."),
            ],
            description=(
                "Required when creating a user. "
                "Leave blank on edit to keep the current password."
            ),
        ),
        "password_confirm": PasswordField(
            "Confirm Password",
            validators=[
                OptionalValidator(),
                EqualTo("password", message="Passwords must match."),
            ],
        ),
    }

    form_widget_args = {
        "username":                  {"placeholder": "e.g. electronora"},
        "phone":                     {"placeholder": "+256XXXXXXXXX"},
        "email":                     {"placeholder": "nora@electronora.com"},
        "timezone":                  {"placeholder": "Africa/Nairobi"},
        "avatar_url":                {"placeholder": "https://..."},
        "password":                  {
            "autocomplete": "new-password",
            "id": "field-password",
            "class": "form-control",
        },
        "password_confirm":          {
            "autocomplete": "new-password",
            "id": "field-password-confirm",
            "class": "form-control",
        },
        "lockout_duration_minutes":  {"min": 1},
        "max_failed_attempts":       {"min": 1},
        "max_otp_requests_per_hour": {"min": 1},
    }

    form_choices = {
        "role": [(r.value, r.value.title()) for r in UserRole],
    }

    # ------------------------------------------------------------------ #
    # SIMPLIFIED: Use model validators instead of duplicating logic
    # ------------------------------------------------------------------ #
    def validate_form(self, form):
        """
        Validate form data - let the model do the heavy lifting.
        Only validate what Flask-Admin can't get from the model.
        """
        is_valid = super().validate_form(form)
        
        # Create a temporary model instance to test validations
        # without committing to database
        temp_model = self.model()
        
        # Test username validation using the model's validator
        if hasattr(form, 'username') and form.username.data:
            try:
                temp_model.validate_username('username', form.username.data)
            except ValueError as e:
                form.username.errors.append(str(e))
                is_valid = False
        
        # Test phone validation using the model's validator
        if hasattr(form, 'phone') and form.phone.data:
            try:
                temp_model.validate_phone('phone', form.phone.data)
            except ValueError as e:
                form.phone.errors.append(str(e))
                is_valid = False
        
        # Test email validation using the model's validator
        if hasattr(form, 'email') and form.email.data:
            try:
                temp_model.validate_email('email', form.email.data)
            except ValueError as e:
                form.email.errors.append(str(e))
                is_valid = False
        
        return is_valid

    def on_model_change(self, form, model, is_created: bool) -> None:
        """
        Handle password - everything else is validated by the model.
        """
        raw = (form.password.data or "").strip()
        confirm = (form.password_confirm.data or "").strip()

        if is_created:
            if not raw:
                raise ValidationError("A password is required when creating a user.")
            if raw != confirm:
                raise ValidationError("Passwords do not match.")
            # Let the model's set_password handle validation and hashing
            model.set_password(raw)
        else:
            # Edit: only re-hash when the admin actually typed something
            if raw:
                if raw != confirm:
                    raise ValidationError("Passwords do not match.")
                # Let the model's set_password handle validation and hashing
                model.set_password(raw)
        
        # The model's validators will automatically validate username, phone, email
        # when the session is committed

    def on_model_delete(self, model) -> None:
        """Hard-delete guard — must type the exact username"""
        confirm = request.form.get("delete_confirm", "").strip() # srill bugging me
        if confirm != model.username:
            raise ValidationError(
                f"Permanent deletion aborted. "
                f"You must type the username '{model.username}' to confirm."
            )

    # ------------------------------------------------------------------ #
    # SIMPLIFIED: Better error handling that leverages model errors
    # ------------------------------------------------------------------ #
    def handle_view_exception(self, exc):
        """
        Handle exceptions - now including model validation errors.
        """
        error_message = str(exc)
        
        # Handle model validation errors (these come from the model's validators)
        if "Username" in error_message and ("only contain" in error_message or "characters" in error_message):
            flash(f"❌ {error_message}", "error")
            return True
        
        elif "phone" in error_message.lower() and "invalid" in error_message.lower():
            flash(
                "❌ Invalid phone number format.<br>"
                "💡 <strong>Tip:</strong> Use international format with country code.<br>"
                "Examples: <code>+256712345678</code> or <code>+447123456789</code>",
                "error"
            )
            return True
        
        elif "email" in error_message.lower() and "invalid" in error_message.lower():
            flash(
                "❌ Invalid email address format.<br>"
                "💡 <strong>Tip:</strong> Use a valid email like <code>user@example.com</code>",
                "error"
            )
            return True
        
        # Handle password validation errors (from set_password method)
        elif "password" in error_message.lower():
            if "match" in error_message.lower():
                flash("❌ Passwords do not match. Please re-enter both password fields.", "error")
            elif "6 characters" in error_message.lower() or "length" in error_message.lower():
                flash("❌ Password must be at least 6 characters long.", "error")
            elif "hashing" in error_message.lower():
                flash("❌ An error occurred while securing the password. Please try again.", "error")
            else:
                flash(f"❌ {error_message}", "error")
            return True
        
        # Handle delete confirmation errors
        elif "Permanent deletion aborted" in error_message or "must type the username" in error_message:
            flash(f"❌ {error_message}", "error")
            return True
        
        # Handle database integrity errors (e.g., duplicate username/phone/email)
        elif "IntegrityError" in str(type(exc)) or "Duplicate entry" in error_message:
            if "username" in error_message.lower():
                flash("❌ Username already exists. Please choose a different username.", "error")
            elif "phone" in error_message.lower():
                flash("❌ Phone number already registered. Please use a different number.", "error")
            elif "email" in error_message.lower():
                flash("❌ Email already registered. Please use a different email.", "error")
            else:
                flash("❌ Duplicate value detected. Please check unique fields (username, phone, email).", "error")
            return True
        
        # Handle all other validation errors
        elif "ValidationError" in str(type(exc)) or "ValueError" in str(type(exc)):
            flash(f"❌ {error_message}", "error")
            return True
        
        # Not handled - let Flask-Admin handle it normally
        return super().handle_view_exception(exc) if hasattr(super(), 'handle_view_exception') else False

    # ------------------------------------------------------------------ #
    # Improved search that leverages model methods
    # ------------------------------------------------------------------ #
    def apply_search(self, query, search_term):
        """
        Apply case-insensitive search across username, email, and phone.
        """
        if not search_term:
            return query
        
        from sqlalchemy import or_, func
        
        search_term = search_term.strip()
        search_conditions = []
        
        # Username search (case-insensitive)
        search_conditions.append(
            func.lower(self.model.username).contains(func.lower(search_term))
        )
        
        # Email search (case-insensitive)
        if self.model.email is not None:
            search_conditions.append(
                func.lower(self.model.email).contains(func.lower(search_term))
            )
        
        # Phone search - leverage the model's phone normalization
        # Create a temporary instance to use its validation method
        temp_model = self.model()
        try:
            # Try to normalize the search term using the same logic as the model
            normalized_phone = temp_model.validate_phone('phone', search_term)
            search_conditions.append(self.model.phone.contains(normalized_phone))
        except ValueError:
            # If it's not a valid phone format, search as-is
            search_conditions.append(self.model.phone.contains(search_term))
        
        return query.filter(or_(*search_conditions))

    # ------------------------------------------------------------------ #
    # Bulk actions (unchanged - they already use model methods)
    # ------------------------------------------------------------------ #

    @action("lock_accounts", "Lock Selected",
            "Lock selected accounts? They will be unable to log in.")
    def action_lock(self, ids) -> None:
        count = 0
        for user in self._fetch_users(ids):
            if not user.is_locked:
                user.is_locked = True
                user.locked_until = (
                    now_utc() + timedelta(minutes=user.lockout_duration_minutes)
                )
                self.session.add(user)
                logger.info("[ADMIN ACTION] Locked user %s (id=%s) by %s",
                            user.username, user.id, current_user.username)
                count += 1
        self.session.commit()
        flash(f"Locked {count} account(s).", "success")

    @action("unlock_accounts", "Unlock Selected",
            "Unlock selected accounts and reset failed-attempt counters?")
    def action_unlock(self, ids) -> None:
        count = 0
        for user in self._fetch_users(ids):
            if user.is_locked or user.failed_login_attempts:
                user.reset_failed_attempts()
                self.session.add(user)
                logger.info("[ADMIN ACTION] Unlocked user %s (id=%s) by %s",
                            user.username, user.id, current_user.username)
                count += 1
        self.session.commit()
        flash(f"Unlocked {count} account(s).", "success")

    @action("deactivate_accounts", "Deactivate Selected",
            "Deactivate selected accounts?")
    def action_deactivate(self, ids) -> None:
        count = 0
        for user in self._fetch_users(ids):
            if user.is_active:
                user.is_active = False
                self.session.add(user)
                logger.info("[ADMIN ACTION] Deactivated user %s (id=%s) by %s",
                            user.username, user.id, current_user.username)
                count += 1
        self.session.commit()
        flash(f"Deactivated {count} account(s).", "success")

    @action("activate_accounts", "Activate Selected",
            "Activate selected accounts?")
    def action_activate(self, ids) -> None:
        count = 0
        for user in self._fetch_users(ids):
            if not user.is_active:
                user.is_active = True
                self.session.add(user)
                logger.info("[ADMIN ACTION] Activated user %s (id=%s) by %s",
                            user.username, user.id, current_user.username)
                count += 1
        self.session.commit()
        flash(f"Activated {count} account(s).", "success")

    @action("force_password_reset", "Force Password Reset",
            "Flag selected accounts to require a password reset on next login?")
    def action_force_reset(self, ids) -> None:
        count = 0
        for user in self._fetch_users(ids):
            user.force_password_reset = True
            self.session.add(user)
            logger.info("[ADMIN ACTION] Forced password reset for user %s (id=%s) by %s",
                        user.username, user.id, current_user.username)
            count += 1
        self.session.commit()
        flash(f"Flagged {count} account(s) for password reset.", "success")

    @action("reset_otp_rate_limit", "Reset OTP Rate Limit",
            "Clear OTP request counters for selected users?")
    def action_reset_otp(self, ids) -> None:
        count = 0
        for user in self._fetch_users(ids):
            user.otp_request_count = 0
            user.last_otp_request = None
            self.session.add(user)
            logger.info("[ADMIN ACTION] Reset OTP rate limit for user %s (id=%s) by %s",
                        user.username, user.id, current_user.username)
            count += 1
        self.session.commit()
        flash(f"Reset OTP rate limit for {count} user(s).", "success")

    # Internal helpers
    def _fetch_users(self, ids):
        return (
            self.session.query(self.model)
            .filter(self.model.id.in_([int(i) for i in ids]))
            .all()
        )

    def render(self, template, **kwargs):
        kwargs.setdefault("model_name", self.name)
        kwargs.setdefault("model_icon", self.model_icon)
        return super().render(template, **kwargs)

# =============================================================================
# 7. OTP Session Admin View
# =============================================================================
class OTPSessionAdmin(AuditMixin, AdminAccessMixin, ModelView):
    """
    Read-heavy admin view for OTP sessions.
    """
    name = "OTP Sessions"
    model_icon = "fas fa-key"

    # Capabilities
    can_view_details = True
    can_create = False
    can_edit = False
    can_delete = True
    can_export = True
    details_modal = True
    page_size = 100
    export_types = ["csv"]
    export_max_rows = 5000

    # Columns
    column_exclude_list = ["otp_hash"]
    column_details_exclude_list = ["otp_hash"]

    column_list = [
        "id", "user_id", "_user_label",
        "purpose", "_validity",
        "created_at", "expires_at",
    ]

    column_labels = {
        "id": "ID", "user_id": "User ID", "_user_label": "User",
        "purpose": "Purpose", "_validity": "State",
        "created_at": "Created", "expires_at": "Expires",
    }

    column_details_list = [
        "id", "user_id", "purpose", "used", "created_at", "expires_at",
    ]

    # Search, filter, sort (Added user.username for better UX)
    column_searchable_list = ["user_id", "user.username", "purpose"]
    column_filters = ["purpose", "used", "expires_at", "created_at", "user.username"]
    column_sortable_list = [
        "id", "user_id", "purpose", "used", "created_at", "expires_at",
    ]

    # ------------------------------------------------------------------
    # Formatters
    # ------------------------------------------------------------------

    def _fmt_user_label(self, ctx, model, name) -> Markup:
        try:
            user = model.user
            if user:
                # SECURITY: escape() prevents XSS if username contains HTML/JS
                return Markup(
                    f'<a href="/admin/user/details/?id={user.id}" '
                    f'title="View user profile">'
                    f'<i class="fas fa-user"></i> {escape(user.username)}</a>'
                )
        except Exception:
            pass
        return Markup(f'<span class="text-muted">uid:{escape(str(model.user_id))}</span>')

    def _fmt_validity(self, ctx, model, name) -> Markup:
        if model.used:
            return Markup('<span class="badge badge-secondary">Used</span>')
        
        now = now_utc()
        expires = ensure_aware(model.expires_at)
        
        if now > expires:
            return Markup('<span class="badge badge-warning">Expired</span>')
        
        remaining = int((expires - now).total_seconds())
        mins, secs = divmod(remaining, 60)
        label = f"{mins}m {secs}s" if mins else f"{secs}s"
        
        # UX: Added 'otp-countdown' class and 'data-expires-at' for the JS hook
        return Markup(
            f'<span class="badge badge-success otp-countdown" '
            f'data-expires-at="{expires.isoformat()}" title="Expires in {label}">'
            f'Active &bull; {label}</span>'
        )

    def _fmt_created(self, ctx, model, name) -> Markup:
        return _fmt_dt(model.created_at)

    def _fmt_expires(self, ctx, model, name) -> Markup:
        return _fmt_dt(model.expires_at)

    def _fmt_used(self, ctx, model, name) -> Markup:
        return _fmt_bool(model.used)

    column_formatters = {
        "_user_label": _fmt_user_label,
        "_validity":   _fmt_validity,
        "created_at":  _fmt_created,
        "expires_at":  _fmt_expires,
    }

    column_formatters_detail = {
        "used":       _fmt_used,
        "created_at": _fmt_created,
        "expires_at": _fmt_expires,
    }

    # ------------------------------------------------------------------
    # Bulk Actions (Optimized for Memory & Speed)
    # ------------------------------------------------------------------

    @action("invalidate_sessions", "Invalidate Selected",
            "Mark selected sessions as used (blocks their use without deleting them)?")
    def action_invalidate(self, ids) -> None:
        valid_ids = [int(i) for i in ids if str(i).isdigit()]
        if not valid_ids:
            flash("No valid sessions selected.", "warning")
            return

        try:
            # PERFORMANCE: Native SQL update. 
            # NOTE: If your model's mark_used() sets other fields (like used_at), 
            # add them here: e.g., .update({"used": True, "used_at": now_utc()})
            count = self.session.query(self.model).filter(
                self.model.id.in_(valid_ids),
                self.model.used.is_(False)
            ).update({"used": True}, synchronize_session="fetch")
            
            self.session.commit()
            logger.info("[ADMIN ACTION] Invalidated %s OTP sessions by %s", count, current_user.username)
            flash(f"Invalidated {count} session(s).", "success")
        except Exception as e:
            self.session.rollback()
            logger.exception("Failed to invalidate OTP sessions")
            flash(f"Error invalidating sessions: {e}", "danger")

    @action("purge_expired", "Purge Expired",
            "Hard-delete expired and used sessions from the selection?")
    def action_purge_expired(self, ids) -> None:
        valid_ids = [int(i) for i in ids if str(i).isdigit()]
        if not valid_ids:
            flash("No valid sessions selected.", "warning")
            return

        now = now_utc()
        try:
            # PERFORMANCE: Native SQL delete
            count = self.session.query(self.model).filter(
                self.model.id.in_(valid_ids),
                or_(
                    self.model.used.is_(True),
                    self.model.expires_at <= now
                )
            ).delete(synchronize_session="fetch")
            
            self.session.commit()
            logger.info("[ADMIN ACTION] Purged %s expired/used OTP sessions by %s", count, current_user.username)
            flash(f"Purged {count} expired/used session(s).", "success")
        except Exception as e:
            self.session.rollback()
            logger.exception("Failed to purge OTP sessions")
            flash(f"Error purging sessions: {e}", "danger")

    def on_model_delete(self, model) -> None:
        logger.info(
            "[ADMIN ACTION] Hard-deleted OTP session id=%s (user_id=%s) by %s from %s",
            model.id, model.user_id,
            getattr(current_user, "username", "unknown"),
            request.remote_addr,
        )

    # ------------------------------------------------------------------
    # Live Stats & Rendering
    # ------------------------------------------------------------------

    @expose("/")
    def index_view(self):
        try:
            now = now_utc()
            # PERFORMANCE: Single query using conditional aggregation instead of 4 separate queries
            row = self.session.query(
                func.count(self.model.id),
                func.sum(case(
                    (self.model.used.is_(False) & (self.model.expires_at > now), 1), 
                    else_=0
                )),
                func.sum(case(
                    (self.model.used.is_(True), 1), 
                    else_=0
                )),
                func.sum(case(
                    (self.model.used.is_(False) & (self.model.expires_at <= now), 1), 
                    else_=0
                ))
            ).first()

            total, active, used, expired = [x or 0 for x in row]
            
            self._template_args["otp_stats"] = {
                "total": total, "active": active, "used": used, "expired": expired,
            }
        except Exception:
            logger.exception("Failed to compute OTP stats")
            self._template_args["otp_stats"] = {"total": 0, "active": 0, "used": 0, "expired": 0}

        return super().index_view()

    def render(self, template, **kwargs):
        kwargs.setdefault("model_name", self.name)
        kwargs.setdefault("model_icon", self.model_icon)
        return super().render(template, **kwargs)

# =============================================================================
# 8. Dashboard
# =============================================================================

class SecureAdminIndexView(AdminAccessMixin, AdminIndexView):
    """Admin home page — access-controlled, shows current local time."""

    @expose("/")
    def index(self):
        if not self.is_accessible():
            return self.inaccessible_callback("index")

        current_time = datetime.now(timezone.utc)
        if getattr(current_user, "timezone", None):
            try:
                current_time = current_time.astimezone(
                    ZoneInfo(current_user.timezone)
                )
            except Exception:
                logger.warning(
                    "Invalid timezone '%s' for user %s",
                    current_user.timezone,
                    current_user.username,
                )

        return self.render(
            "admin/dashboard.html",
            user=current_user,
            current_time=current_time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        )


# =============================================================================
# 9. Factory
# =============================================================================

def init_admin(app, db):
    """
    Create and configure the Flask-Admin instance.

    Registered views
    ----------------
    /admin/              → Dashboard
    /admin/user/         → UserAdmin        (category: Accounts)
    /admin/otpsession/   → OTPSessionAdmin  (category: Accounts)
    """
    admin = Admin(
        app,
        name="EBS HUB",
        url="/admin",
        index_view=SecureAdminIndexView(name="Dashboard"),
        theme=Bootstrap4Theme(base_template="admin/base.html"),
    )

    admin.add_view(
        UserAdmin(
            User,
            db.session,
            name="Users",
            category="Identity & Access",
        )
    )

    admin.add_view(
        OTPSessionAdmin(
            OTPSession,
            db.session,
            name="OTP Sessions",
            category="Identity & Access",
        )
    )

    return admin