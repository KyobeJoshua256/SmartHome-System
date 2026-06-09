# =============================================================================
# AUTH FORMS — ElectroNora Smart Home
# Purpose: WTForms login forms ONLY.
#          No business logic, no DB calls, no Flask routes.
# =============================================================================

import re
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField, HiddenField, SubmitField
from wtforms.validators import DataRequired, Length, ValidationError, Regexp


# -----------------------------------------------------------------------------
# Shared filter
# -----------------------------------------------------------------------------

def _strip(value):
    """Strip whitespace from string values."""
    return value.strip() if value and isinstance(value, str) else value


# -----------------------------------------------------------------------------
# Shared validator: phone-only format  (mirrors User.validate_phone)
# Used by TokenRequestForm where only a phone number is accepted.
# -----------------------------------------------------------------------------

def validate_phone_format(form, field):
    if not field.data:
        return
    clean = re.sub(r'[\s\-\(\)]', '', field.data)
    if not re.match(r'^\+?[0-9]{10,15}$', clean):
        raise ValidationError(
            "Enter a valid phone number (e.g. +256700000000)."
        )


# =============================================================================
# Form 1: LoginForm  — accepts username, email, or phone (password auth)
# =============================================================================

class LoginForm(FlaskForm):
    identifier = StringField(
        'Username, Email or Phone',
        filters=[_strip],
        validators=[
            DataRequired(message="Please enter your username, email, or phone number."),
            Length(max=120, message="Identifier is too long."),
        ],
        render_kw={
            "placeholder": "johndoe  /  user@example.com  /  +256700000000",
            "autocomplete": "username",
            "id": "login-identifier",
        }
    )

    password = PasswordField(
        'Password',
        validators=[
            DataRequired(message="Password is required."),
            Length(min=6, message="Password must be at least 6 characters."),
        ],
        render_kw={
            "placeholder": "Enter your password",
            "autocomplete": "current-password",
            "id": "login-password",
        }
    )

    remember_me = BooleanField('Keep me signed in', default=False)
    submit = SubmitField('Sign In')


# =============================================================================
# Form 2: TokenRequestForm — phone number only (used to send SMS OTP)
# Field name is 'phone' to match the HTML input name="phone".
# Only a phone number is accepted here; SMS delivery requires a number.
# =============================================================================

class TokenRequestForm(FlaskForm):
    phone = StringField(
        'Phone Number',
        filters=[_strip],
        validators=[
            DataRequired(message="Phone number is required."),
            Length(min=10, max=16, message="Please enter a valid phone number."),
            validate_phone_format,
        ],
        render_kw={
            "placeholder": "+256700000000",
            "autocomplete": "tel",
            "inputmode": "tel",
            "id": "token-phone",
            "pattern": r"^[\+][0-9]{10,15}$|^[0-9]{10,15}$",
        }
    )
    submit = SubmitField('Send Login Code')


# =============================================================================
# Form 3: TokenVerifyForm — verifies the 6-digit OTP sent via SMS
# =============================================================================

class TokenVerifyForm(FlaskForm):
    token = StringField(
        'Login Code',
        filters=[_strip],
        validators=[
            DataRequired(message="Please enter the 6-digit code sent to your phone."),
            Length(min=6, max=6, message="The code must be exactly 6 digits."),
            Regexp(r'^\d{6}$', message="The code must contain digits only."),
        ],
        render_kw={
            "placeholder": "000000",
            "inputmode": "numeric",
            "autocomplete": "one-time-code",
            "maxlength": "6",
            "id": "token-code",
        }
    )

    session_id = HiddenField(
        'Session ID',
        validators=[
            DataRequired(message="Session ID is missing. Please request a new code.")
        ]
    )

    remember_me = BooleanField('Keep me signed in', default=False)
    submit = SubmitField('Verify Code')