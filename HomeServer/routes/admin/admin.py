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
from flask_admin import Admin, AdminIndexView, BaseView, expose
from flask_admin.actions import action
from flask_admin.contrib.sqla import ModelView
from flask_admin.theme import Bootstrap4Theme
from flask_login import current_user
from markupsafe import Markup, escape
from sqlalchemy import func, inspect as sa_inspect, or_, case
from wtforms import PasswordField, ValidationError
from wtforms.validators import EqualTo, Length, Optional as OptionalValidator

from HomeServer.models.users import OTPSession, User, UserRole, ensure_aware, now_utc
from HomeServer.models.rooms import (
    Room, RoomMember, GuestRoom,
    RoomStatus, RoomType, RoomService,
)
from HomeServer.models.utils import KAMPALA_TZ
from HomeServer.forms.rooms import (
    RoomForm, RoomMemberForm, GuestRoomForm,
    AllocateMemberForm, AllocateGuestForm, BulkRoomOperationForm,
)

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
    export_types = [
    "csv",
    "xlsx",
    "pdf",
    "txt"
        ]
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
# 9. Room Admin Views
# =============================================================================

# ---------------------------------------------------------------------------
# 9a. RoomAdminView — CRUD for physical/logical rooms
# ---------------------------------------------------------------------------

class RoomAdminView(AuditMixin, AdminAccessMixin, ModelView):
    """
    CRUD view for Rooms.

    When a room is created a corresponding VACANT RoomMember row is inserted
    automatically so the room immediately appears in the vacant pool without
    any extra admin action.
    """

    name = "Rooms"
    model_icon = "fas fa-door-open"

    can_view_details = True
    can_export = True
    can_create = True
    can_edit = True
    can_delete = True
    details_modal = True
    page_size = 50

    column_list = ["id", "name", "room_type", "status", "created_at", "updated_at"]
    column_labels = {
        "id": "ID", "name": "Name", "room_type": "Type",
        "status": "Status", "created_at": "Created", "updated_at": "Updated",
    }
    column_details_list = ["id", "name", "room_type", "status", "created_at", "updated_at"]
    column_searchable_list = ["name"]
    column_filters = ["room_type", "status"]
    column_sortable_list = ["id", "name", "room_type", "status", "created_at"]

    form = RoomForm
    form_columns = ["name", "room_type", "status"]

    def _fmt_dt(self, ctx, model, name):
        return _fmt_dt(getattr(model, name, None))

    def _fmt_room_type(self, ctx, model, name) -> Markup:
        icons = {
            "personal": ("fa-user", "primary"),
            "shared": ("fa-users", "info"),
            "guest": ("fa-user-clock", "warning"),
        }
        val = model.room_type.value
        icon, color = icons.get(val, ("fa-door-open", "secondary"))
        return Markup(
            f'<span class="badge badge-{color}">'
            f'<i class="fas {icon}"></i> {val.capitalize()}</span>'
        )

    def _fmt_room_status(self, ctx, model, name) -> Markup:
        colors = {
            "vacant": "secondary", "active": "success",
            "inactive": "warning", "expired": "danger",
        }
        val = model.status.value
        color = colors.get(val, "secondary")
        return Markup(f'<span class="badge badge-{color}">{val.capitalize()}</span>')

    column_formatters = {
        "room_type": _fmt_room_type,
        "status": _fmt_room_status,
        "created_at": _fmt_dt,
        "updated_at": _fmt_dt,
    }
    column_formatters_detail = column_formatters

    def on_model_change(self, form, model, is_created: bool) -> None:
        """
        After creating a room, insert a matching VACANT RoomMember entry so
        the room surfaces in the vacant pool immediately.
        """
        super().on_model_change(form, model, is_created)
        if is_created:
            # Flush to obtain model.id before creating the FK reference
            self.session.flush()
            vacant = RoomMember(
                room_id=model.id,
                user_id=None,
                room_type=model.room_type,
                status=RoomStatus.VACANT,
                can_view=True,
                can_control=True,
                can_manage=False,
            )
            self.session.add(vacant)
            logger.info(
                "[AUDIT] Auto-created VACANT RoomMember for room id=%s name=%r by %s",
                model.id, model.name, getattr(current_user, "username", "unknown"),
            )

    def render(self, template, **kwargs):
        kwargs.setdefault("model_name", self.name)
        kwargs.setdefault("model_icon", self.model_icon)
        return super().render(template, **kwargs)


# ---------------------------------------------------------------------------
# 9b. RoomMemberAdminView — CRUD for member allocations
# ---------------------------------------------------------------------------

class RoomMemberAdminView(AuditMixin, AdminAccessMixin, ModelView):
    """
    CRUD view for RoomMember allocations.

    Keeps status consistent with user assignment: a row with no user_id is
    forced to VACANT; a row gaining a user_id is forced to ACTIVE.
    Vacated_at is maintained automatically.
    """

    name = "Room Allocations"
    model_icon = "fas fa-bed"

    can_view_details = True
    can_export = True
    can_create = True
    can_edit = True
    can_delete = True   
    details_modal = True
    page_size = 50

    column_list = [
        "id", "room", "user", "room_type", "status",
        "can_view", "can_control", "can_manage", "vacated_at", "created_at",
    ]
    column_labels = {
        "id": "ID", "room": "Room", "user": "Member", "room_type": "Type",
        "status": "Status", "can_view": "View", "can_control": "Control",
        "can_manage": "Manage", "vacated_at": "Vacated", "created_at": "Allocated",
    }
    column_details_list = column_list + ["updated_at"]
    column_searchable_list = ["room.name", "user.username"]
    column_filters = ["room_type", "status", "can_view", "can_control", "can_manage"]
    column_sortable_list = ["id", "room_type", "status", "created_at", "vacated_at"]

    form = RoomMemberForm
    form_columns = ["room_id", "user_id", "room_type", "status", "can_view", "can_control", "can_manage"]


    def _fmt_room(self, ctx, model, name) -> Markup:
        if model.room:
            return Markup(
                f'<a href="/admin/room/details/?id={model.room_id}" '
                f'title="View room">'
                f'<i class="fas fa-door-open"></i> {escape(model.room.name)}</a>'
            )
        return Markup('<span class="text-muted">—</span>')

    def _fmt_user(self, ctx, model, name) -> Markup:
        if model.user:
            return Markup(
                f'<a href="/admin/user/details/?id={model.user_id}" '
                f'title="View member">'
                f'<i class="fas fa-user"></i> {escape(model.user.username)}</a>'
            )
        return Markup('<span class="text-muted text-italic">Vacant</span>')

    def _fmt_bool_col(self, ctx, model, name) -> Markup:
        return _fmt_bool(getattr(model, name, None))

    def _fmt_dt_col(self, ctx, model, name):
        return _fmt_dt(getattr(model, name, None))

    def _fmt_status(self, ctx, model, name) -> Markup:
        colors = {
            "vacant": "secondary", "active": "success",
            "inactive": "warning", "expired": "danger",
        }
        val = model.status.value
        return Markup(
            f'<span class="badge badge-{colors.get(val, "secondary")}">{val.capitalize()}</span>'
        )

    column_formatters = {
    "room":        _fmt_room,
    "user":        _fmt_user,
    "can_view":    _fmt_bool_col,
    "can_control": _fmt_bool_col,
    "can_manage":  _fmt_bool_col,
    "status":      _fmt_status,
    "vacated_at":  _fmt_dt_col,
    "created_at":  _fmt_dt_col,
    }

    column_formatters_detail = column_formatters

    def on_model_change(self, form, model, is_created: bool) -> None:
        """
        Coerce user_id = 0 (the '--- Vacant ---' sentinel) to None, then
        keep status consistent with whether a user is assigned.
        """
        super().on_model_change(form, model, is_created)

        # Sentinel from RoomMemberForm: 0 means "no user"
        if model.user_id == 0:
            model.user_id = None

        now = datetime.now(tz=KAMPALA_TZ)

        if model.user_id is None:
            # Room is being released back to the vacant pool
            if model.status == RoomStatus.ACTIVE:
                model.status = RoomStatus.VACANT
            if model.vacated_at is None:
                model.vacated_at = now
        else:
            # Room is being assigned to a member
            if model.status in (RoomStatus.VACANT, RoomStatus.INACTIVE):
                model.status = RoomStatus.ACTIVE
                model.vacated_at = None

    def render(self, template, **kwargs):
        kwargs.setdefault("model_name", self.name)
        kwargs.setdefault("model_icon", self.model_icon)
        return super().render(template, **kwargs)


# ---------------------------------------------------------------------------
# 9c. GuestRoomAdminView — CRUD for time-bounded guest allocations
# ---------------------------------------------------------------------------

class GuestRoomAdminView(AuditMixin, AdminAccessMixin, ModelView):
    """
    CRUD view for GuestRoom allocations.

    Converts valid_days from comma-string (form input) to a JSON list
    (model column) on save, and back on load.  The model's @validates
    decorator enforces the day-name whitelist so we don't duplicate that
    logic here.
    """

    name = "Guest Allocations"
    model_icon = "fas fa-user-clock"

    can_view_details = True
    can_export = True
    can_create = True
    can_edit = True
    can_delete = True  
    details_modal = True
    page_size = 50

    column_list = [
        "id", "room", "guest", "invited_by_id", "status",
        "expires_at", "can_view", "can_control", "created_at",
    ]
    column_labels = {
        "id": "ID", "room": "Room", "guest": "Guest", "invited_by_id": "Invited By",
        "status": "Status", "expires_at": "Expires", "can_view": "View",
        "can_control": "Control", "created_at": "Allocated",
    }
    column_details_list = column_list + [
        "valid_from", "valid_until", "valid_days", "vacated_at", "updated_at",
    ]
    column_searchable_list = ["room.name", "guest.username", "invited_by_id"]
    column_filters = ["status", "expires_at", "can_view", "can_control"]
    column_sortable_list = ["id", "expires_at", "status", "created_at"]

    form = GuestRoomForm
    form_columns = [
        "room_id", "guest_id", "invited_by_id", "expires_at",
        "valid_from", "valid_until", "valid_days",
        "can_view", "can_control", "status",
    ]

    def _fmt_room(self, ctx, model, name) -> Markup:
        if model.room:
            return Markup(
                f'<a href="/admin/room/details/?id={model.room_id}" '
                f'title="View room">'
                f'<i class="fas fa-door-open"></i> {escape(model.room.name)}</a>'
            )
        return Markup('<span class="text-muted">—</span>')

    def _fmt_guest(self, ctx, model, name) -> Markup:
        if model.guest:
            return Markup(
                f'<a href="/admin/user/details/?id={model.guest_id}" '
                f'title="View guest">'
                f'<i class="fas fa-user-clock"></i> {escape(model.guest.username)}</a>'
            )
        return Markup('<span class="text-muted">—</span>')

    def _fmt_bool_col(self, ctx, model, name) -> Markup:
        return _fmt_bool(getattr(model, name, None))

    def _fmt_dt_col(self, ctx, model, name):
        return _fmt_dt(getattr(model, name, None))

    def _fmt_status(self, ctx, model, name) -> Markup:
        colors = {
            "active": "success", "expired": "danger",
            "vacant": "secondary", "inactive": "warning",
        }
        val = model.status.value
        accessible = model.is_currently_accessible() if val == "active" else False
        badge = colors.get(val, "secondary")
        extra = (
            ' <i class="fas fa-clock text-success" title="Currently accessible"></i>'
            if accessible else ""
        )
        return Markup(
            f'<span class="badge badge-{badge}">{val.capitalize()}</span>{extra}'
        )

    def _fmt_expires(self, ctx, model, name) -> Markup:
        dt = model.expires_at
        if dt is None:
            return Markup('<span class="text-muted">—</span>')
        now = datetime.now(tz=KAMPALA_TZ)
        formatted = _fmt_dt(dt)
        if dt <= now:
            return Markup(f'<span class="text-danger">{formatted}</span>')
        if dt <= now + timedelta(days=3):
            return Markup(f'<span class="text-warning">{formatted}</span>')
        return formatted

    column_formatters = {
    "room":       _fmt_room,
    "guest":      _fmt_guest,
    "can_view":   _fmt_bool_col,
    "can_control": _fmt_bool_col,
    "status":     _fmt_status,
    "expires_at": _fmt_expires,
    "created_at": _fmt_dt_col,
    }

    column_formatters_detail = column_formatters
    
    def on_model_change(self, form, model, is_created: bool) -> None:
        """
        Convert valid_days from comma-separated string to list when saved
        via the admin form.  The model's @validates('valid_days') will then
        normalise and validate the values.
        """
        super().on_model_change(form, model, is_created)

        if isinstance(model.valid_days, str):
            raw = model.valid_days.strip()
            model.valid_days = (
                [d.strip().lower() for d in raw.split(",") if d.strip()]
                if raw else None
            )

    def render(self, template, **kwargs):
        kwargs.setdefault("model_name", self.name)
        kwargs.setdefault("model_icon", self.model_icon)
        return super().render(template, **kwargs)


# ---------------------------------------------------------------------------
# 9d. RoomAllocationView — allocate a vacant/inactive room to a member
# ---------------------------------------------------------------------------

class RoomAllocationView(AdminAccessMixin, BaseView):
    """
    Custom operation view: pick a vacant room from the pool and assign it to
    a household member.  Uses RoomService.allocate_to_member so the existing
    VACANT/INACTIVE row is reused, preserving device mappings.
    """

    @expose("/", methods=["GET", "POST"])
    def index(self):
        from HomeServer import database

        form = AllocateMemberForm()

        if form.validate_on_submit():
            try:
                room_member = database.session.get(RoomMember, form.room_member_id.data)
                if room_member is None:
                    flash("Room allocation record not found.", "error")
                    return redirect(url_for("roomallocationview.index"))

                RoomService.allocate_to_member(
                    room_id=room_member.room_id,
                    user_id=form.user_id.data,
                    room_type=RoomType(form.room_type.data),
                    can_view=form.can_view.data,
                    can_control=form.can_control.data,
                    can_manage=form.can_manage.data,
                )
                database.session.commit()
                logger.info(
                    "[ADMIN ACTION] Allocated room_id=%s to user_id=%s by %s",
                    room_member.room_id, form.user_id.data,
                    getattr(current_user, "username", "unknown"),
                )
                flash(f"Room '{room_member.room.name}' successfully allocated.", "success")
                return redirect(url_for("roomallocationview.index"))
            except Exception as exc:
                database.session.rollback()
                logger.exception("Failed to allocate room to member")
                flash(f"Error allocating room: {exc}", "error")

        return self.render("admin/rooms/room_allocation.html", form=form)


# ---------------------------------------------------------------------------
# 9e. GuestAllocationView — allocate a vacant room to a guest (admin only)
# ---------------------------------------------------------------------------

class GuestAllocationView(AdminAccessMixin, BaseView):
    """
    Custom operation view: allocate a VACANT room to a guest user with
    time-bounded access.  Uses RoomService.allocate_to_guest.
    invited_by is always the currently logged-in admin.
    """

    @expose("/", methods=["GET", "POST"])
    def index(self):
        from HomeServer import database

        form = AllocateGuestForm()

        if form.validate_on_submit():
            try:
                # Parse valid_days from the comma string if provided
                raw_days = form.valid_days.data
                valid_days = (
                    [d.strip().lower() for d in raw_days.split(",") if d.strip()]
                    if raw_days else None
                )

                RoomService.allocate_to_guest(
                    room_id=form.room_id.data,
                    guest_id=form.guest_id.data,
                    invited_by_username=current_user.username,
                    expires_at=form.expires_at.data,
                    can_view=form.can_view.data,
                    can_control=form.can_control.data,
                    valid_from=form.valid_from.data or None,
                    valid_until=form.valid_until.data or None,
                    valid_days=valid_days,
                )
                database.session.commit()
                logger.info(
                    "[ADMIN ACTION] Guest room allocated room_id=%s guest_id=%s by %s",
                    form.room_id.data, form.guest_id.data,
                    current_user.username,
                )
                flash("Room successfully allocated to guest.", "success")
                return redirect(url_for("guestallocationview.index"))
            except Exception as exc:
                database.session.rollback()
                logger.exception("Failed to allocate room to guest")
                flash(f"Error allocating room to guest: {exc}", "error")

        return self.render("admin/rooms/guest_allocation.html", form=form)


# ---------------------------------------------------------------------------
# 9f. RoomManagementDashboard — overview + quick-action endpoints
# ---------------------------------------------------------------------------

class RoomManagementDashboard(AdminAccessMixin, BaseView):
    """
    Dashboard showing room allocation statistics, recent activity, and
    guest allocations expiring within 3 days.

    Also exposes quick-action endpoints for expiring a guest allocation or
    releasing a member room without going through the full CRUD form.
    """

    @expose("/")
    def index(self):
        from HomeServer import database

        now = datetime.now(tz=KAMPALA_TZ)
        soon = now + timedelta(days=3)

        total_rooms = database.session.query(Room).count()
        active_member_allocations = (
            database.session.query(RoomMember)
            .filter_by(status=RoomStatus.ACTIVE).count()
        )
        active_guest_allocations = (
            database.session.query(GuestRoom)
            .filter_by(status=RoomStatus.ACTIVE).count()
        )
        vacant_rooms = (
            database.session.query(RoomMember)
            .filter(RoomMember.status.in_([RoomStatus.VACANT, RoomStatus.INACTIVE]))
            .count()
        )
        expired_guests = (
            database.session.query(GuestRoom)
            .filter_by(status=RoomStatus.EXPIRED).count()
        )

        recent_member_allocations = (
            database.session.query(RoomMember)
            .order_by(RoomMember.created_at.desc())
            .limit(10).all()
        )
        recent_guest_allocations = (
            database.session.query(GuestRoom)
            .order_by(GuestRoom.created_at.desc())
            .limit(10).all()
        )
        soon_expiring = (
            database.session.query(GuestRoom)
            .filter(
                GuestRoom.status == RoomStatus.ACTIVE,
                GuestRoom.expires_at <= soon,
            ).all()
        )

        return self.render(
            "admin/rooms/dashboard.html",
            total_rooms=total_rooms,
            active_member_allocations=active_member_allocations,
            active_guest_allocations=active_guest_allocations,
            vacant_rooms=vacant_rooms,
            expired_guests=expired_guests,
            recent_member_allocations=recent_member_allocations,
            recent_guest_allocations=recent_guest_allocations,
            soon_expiring=soon_expiring,
        )

    @expose("/expire-guest/<int:guest_id>")
    def expire_guest(self, guest_id):
        from HomeServer import database

        guest = database.session.get(GuestRoom, guest_id)
        if guest is None:
            flash("Guest allocation not found.", "error")
            return redirect(url_for("roommember.index_view"))

        guest.expire()
        database.session.commit()
        logger.info(
            "[ADMIN ACTION] Manually expired GuestRoom id=%s (room=%s) by %s",
            guest_id, getattr(guest.room, "name", "?"),
            getattr(current_user, "username", "unknown"),
        )
        flash(f"Guest allocation for room '{guest.room.name}' has been expired.", "success")
        return redirect(url_for("roommember.index_view"))

    @expose("/release-room/<int:member_id>")
    def release_room(self, member_id):
        from HomeServer import database

        member = database.session.get(RoomMember, member_id)
        if member is None:
            flash("Room allocation not found.", "error")
            return redirect(url_for("roommember.index_view"))

        if not member.user_id:
            flash("This room is already vacant.", "warning")
            return redirect(url_for("roommember.index_view"))

        RoomService.release_member_room(member)
        database.session.commit()
        logger.info(
            "[ADMIN ACTION] Released RoomMember id=%s (room=%s) by %s",
            member_id, getattr(member.room, "name", "?"),
            getattr(current_user, "username", "unknown"),
        )
        flash(f"Room '{member.room.name}' has been released back to the vacant pool.", "success")
        return redirect(url_for("roommember.index_view"))


# ---------------------------------------------------------------------------
# 9g. BulkRoomOperationView — batch operations across multiple rooms
# ---------------------------------------------------------------------------

class BulkRoomOperationView(AdminAccessMixin, BaseView):
    """
    Batch operations for room management.

    'deactivate' sets the Room.status to VACANT (not INACTIVE — INACTIVE is
    reserved for member-deleted lifecycle rows on RoomMember, not rooms
    themselves).  To fully remove a room from rotation, release its member
    allocation first via the Dashboard.
    """

    @expose("/", methods=["GET", "POST"])
    def index(self):
        from HomeServer import database

        form = BulkRoomOperationForm()

        if form.validate_on_submit():
            room_ids = form.room_ids.data
            action_name = form.action.data

            if not room_ids:
                flash("No rooms selected.", "warning")
                return redirect(request.url)

            try:
                count = 0

                if action_name == "set_vacant":
                    for room_id in room_ids:
                        active_members = (
                            database.session.query(RoomMember)
                            .filter_by(room_id=room_id, status=RoomStatus.ACTIVE)
                            .all()
                        )
                        for member in active_members:
                            if member.user_id:
                                RoomService.release_member_room(member)
                                count += 1
                    flash(f"Released {count} member allocation(s) to the vacant pool.", "success")

                elif action_name in ("set_shared", "set_personal", "set_guest"):
                    type_map = {
                        "set_shared": RoomType.SHARED,
                        "set_personal": RoomType.PERSONAL,
                        "set_guest": RoomType.GUEST,
                    }
                    new_type = type_map[action_name]
                    rooms = database.session.query(Room).filter(Room.id.in_(room_ids)).all()
                    for room in rooms:
                        room.room_type = new_type
                        count += 1
                    flash(
                        f"Set {count} room(s) to {new_type.value.upper()} type.",
                        "success",
                    )

                elif action_name == "activate":
                    # Mark rooms as VACANT (ready for allocation)
                    rooms = database.session.query(Room).filter(Room.id.in_(room_ids)).all()
                    for room in rooms:
                        room.status = RoomStatus.VACANT
                        count += 1
                    flash(f"Activated {count} room(s).", "success")

                elif action_name == "deactivate":
                    # Set to VACANT with no member — this makes them visible in the
                    # pool but unallocated. Rooms have no INACTIVE status by design
                    # (INACTIVE belongs to RoomMember rows, not Room rows).
                    rooms = database.session.query(Room).filter(Room.id.in_(room_ids)).all()
                    for room in rooms:
                        active_members = (
                            database.session.query(RoomMember)
                            .filter_by(room_id=room.id, status=RoomStatus.ACTIVE)
                            .all()
                        )
                        for active_member in active_members:
                            if active_member.user_id:
                                RoomService.release_member_room(active_member)
                        room.status = RoomStatus.VACANT
                        count += 1
                    flash(
                        f"Deactivated {count} room(s) — all members released, rooms set to vacant.",
                        "success",
                    )

                database.session.commit()
                logger.info(
                    "[ADMIN ACTION] Bulk room op '%s' on %s rooms by %s",
                    action_name, len(room_ids),
                    getattr(current_user, "username", "unknown"),
                )

            except Exception as exc:
                database.session.rollback()
                logger.exception("Bulk room operation failed")
                flash(f"Error performing bulk operation: {exc}", "error")

            return redirect(url_for("bulkroomoperationview.index"))

        return self.render("admin/rooms/bulk_room_Operations.html", form=form)


# =============================================================================
# 10. Factory
# =============================================================================

def init_admin(app, db):
    """
    Create and configure the Flask-Admin instance.

    Registered views
    ----------------
    /admin/                          → Dashboard
    /admin/user/                     → UserAdmin          (Identity & Access)
    /admin/otpsession/               → OTPSessionAdmin    (Identity & Access)
    /admin/room/                     → RoomAdminView      (Room Management)
    /admin/roommember/               → RoomMemberAdmin    (Room Management)
    /admin/guestroom/                → GuestRoomAdmin     (Room Management)
    /admin/roommanagementdashboard/  → Dashboard         (Room Management)
    /admin/roomallocationview/       → Allocate to Member (Room Operations)
    /admin/guestallocationview/      → Allocate to Guest  (Room Operations)
    /admin/bulkroomoperationview/    → Bulk Operations    (Room Operations)
    """
    admin = Admin(
        app,
        name="EBS HUB",
        url="/admin",
        index_view=SecureAdminIndexView(name="Dashboard"),
        theme=Bootstrap4Theme(base_template="admin/base.html"),
    )

    # -- Identity & Access ---------------------------------------------------
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

    # -- Room Management (CRUD) ----------------------------------------------
    admin.add_view(
        RoomAdminView(
            Room,
            db.session,
            name="Rooms",
            category="Room Management",
        )
    )
    admin.add_view(
        RoomMemberAdminView(
            RoomMember,
            db.session,
            name="Room Allocations",
            category="Room Management",
        )
    )
    admin.add_view(
        GuestRoomAdminView(
            GuestRoom,
            db.session,
            name="Guest Allocations",
            category="Room Management",
        )
    )
    admin.add_view(
        RoomManagementDashboard(
            name="Dashboard",
            endpoint="roommanagementdashboard",
            category="Room Management",
        )
    )

    # -- Room Operations (custom workflows) ----------------------------------
    admin.add_view(
        RoomAllocationView(
            name="Allocate to Member",
            endpoint="roomallocationview",
            category="Room Operations",
        )
    )
    admin.add_view(
        GuestAllocationView(
            name="Allocate to Guest",
            endpoint="guestallocationview",
            category="Room Operations",
        )
    )
    admin.add_view(
        BulkRoomOperationView(
            name="Bulk Operations",
            endpoint="bulkroomoperationview",
            category="Room Operations",
        )
    )

    return admin