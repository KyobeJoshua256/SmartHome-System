# =============================================================================
# USER MODEL — Smart Home System with OTP Support (Optimized for Pi Zero 2W)
# =============================================================================

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
import re
import secrets
import hashlib

from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, CheckConstraint, ForeignKey, Index
)
from sqlalchemy.orm import validates, relationship
from flask_login import UserMixin
from enum import Enum as PyEnum
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, HashingError

from HomeServer import database

# =============================================================================
# SECTION 1: Constants & Hashing Configuration
# =============================================================================

DEFAULT_TIMEZONE = "Africa/Nairobi"
from .utils import EAT, now_kampala

def ensure_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure a datetime is timezone-aware (assume EAT/UTC+3 if naive)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

ph = PasswordHasher(time_cost=3, memory_cost=32768, parallelism=2)


# =============================================================================
# SECTION 2: Enums
# =============================================================================

class UserRole(PyEnum):
    ADMIN = "admin"
    USER = "user"
   

# =============================================================================
# SECTION 3: USER MODEL
# =============================================================================

class User(UserMixin, database.Model):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    username = Column(String(50), nullable=False, unique=True, index=True)
    phone = Column(String(20), unique=True, nullable=False, index=True)
    email = Column(String(120), unique=True, nullable=True, index=True)
    role = Column(String(20), default=UserRole.USER.value, nullable=False, index=True)
    
    # Authentication
    password_hash = Column(String(256), nullable=True)
    
    # OTP Authentication
    otp_enabled = Column(Boolean, default=True, nullable=False)
    phone_verified = Column(Boolean, default=True, nullable=False)
    
    # Presence / online status — True while logged in, False after logout.
    # Default is False (offline). Flipped by mark_online() on successful login
    # and mark_offline() on logout. Mirror of how social media tracks presence.
    is_active = Column(Boolean, default=False, nullable=False, index=True)
    last_seen = Column(DateTime(timezone=True), nullable=True)   # set on every login & logout
    is_deleted = Column(Boolean, default=False, nullable=False, index=True)
    is_locked = Column(Boolean, default=False, nullable=False, index=True)
    failed_login_attempts = Column(Integer, default=0, nullable=False)
    locked_until = Column(DateTime(timezone=True), nullable=True)
    
    # Rate Limiting
    max_failed_attempts = Column(Integer, default=5, nullable=False)
    lockout_duration_minutes = Column(Integer, default=60, nullable=False)
    
    # OTP Rate Limiting
    otp_request_count = Column(Integer, default=0, nullable=False)
    last_otp_request = Column(DateTime(timezone=True), nullable=True)
    max_otp_requests_per_hour = Column(Integer, default=3, nullable=False)

    # Profile
    avatar_url = Column(String(500), nullable=True)
    timezone = Column(String(50), default=DEFAULT_TIMEZONE, nullable=False)
    
    # Notification Preferences
    receive_push = Column(Boolean, default=True)
    mute_notifications = Column(Boolean, default=False)

    # Security
    last_password_change = Column(DateTime(timezone=True), nullable=True)
    force_password_reset = Column(Boolean, default=False)

    # Timestamps (timezone=True so SQLAlchemy stores/retrieves tz-aware datetimes)
    created_at = Column(DateTime(timezone=True), default=now_kampala, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=now_kampala, onupdate=now_kampala, nullable=False)

    # Relationships
    room_memberships = relationship(
        "RoomMember",
        back_populates="user",
        foreign_keys="RoomMember.user_id",
        lazy="dynamic",
    )
    # guest_rooms = relationship(
    #     "GuestRoom",
    #     back_populates="guest",
    #     foreign_keys="GuestRoom.guest_id",
    #     lazy="dynamic",
    # )

    conversations = relationship(
        "ConversationParticipant",
         back_populates="user",
         cascade="all, delete-orphan",
    )
    messages_sent = relationship(
        "Message",
        back_populates="sender",
        foreign_keys="Message.sender_id",
        passive_deletes=True,   
        cascade="all, delete-orphan", 
     )

    __table_args__ = (
        CheckConstraint('failed_login_attempts >= 0', name='check_failed_attempts_positive'),
        CheckConstraint('max_failed_attempts > 0', name='check_max_attempts_positive'),
        CheckConstraint('lockout_duration_minutes > 0', name='check_lockout_duration_positive'),
        CheckConstraint('otp_request_count >= 0', name='check_otp_count_positive'),
    )

    # ========================================================================
    # Validators - FIXED: Normalize phone numbers to E.164 format
    # ========================================================================

    @validates('phone')
    def validate_phone(self, key, phone):
        if phone:
            # Remove spaces, dashes, parentheses
            clean_phone = re.sub(r'[\s\-\(\)]', '', phone)
            
            # Validate E.164 format: optional + followed by 10-15 digits
            if not re.match(r'^\+?[0-9]{10,15}$', clean_phone):
                raise ValueError("Invalid phone number format. Use international format (e.g., +256XXXXXXXXX)")
            
            # Ensure E.164 format with + prefix
            if not clean_phone.startswith('+'):
                clean_phone = '+' + clean_phone
                
            return clean_phone
        return phone

    @validates('username')
    def validate_username(self, key, username):
        if username:
            if len(username) < 3:
                raise ValueError("Username must be at least 3 characters")
            if not re.match(r'^[a-zA-Z0-9_]+$', username):
                raise ValueError("Username can only contain letters, numbers, and underscores")
        return username

    @validates('email')
    def validate_email(self, key, email):
        if email:
            email_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
            if not re.match(email_regex, email):
                raise ValueError("Invalid email format")
        return email

    # ========================================================================
    # Helper: Get formatted phone for display
    # ========================================================================

    def get_formatted_phone(self, format_type='international'):
        """Return formatted phone number for display."""
        if not self.phone:
            return None
        
        digits = self.phone.lstrip('+')
        
        if format_type == 'international' and len(digits) == 12:
            return f"+{digits[:3]} {digits[3:6]} {digits[6:9]} {digits[9:12]}"
        elif format_type == 'local' and len(digits) == 12:
            return f"0{digits[3:6]} {digits[6:9]} {digits[9:12]}"
        return self.phone

    # ========================================================================
    # Password Authentication (Argon2id)
    # ========================================================================

    def set_password(self, password: str) -> None:
        """Hash and store password using Argon2id."""
        if not password or len(password) < 6:
            raise ValueError("Password must be at least 6 characters")
        
        try:
            self.password_hash = ph.hash(password)
            self.last_password_change = now_kampala()
            self.force_password_reset = False
        except HashingError as e:
            raise ValueError(f"Password hashing failed: {e}")

    def check_password(self, password: str) -> bool:
        """Verify password against Argon2id hash."""
        if not self.password_hash or not password:
            return False
        try:
            return ph.verify(self.password_hash, password)
        except VerifyMismatchError:
            return False
        except (VerificationError, Exception):
            return False

    def has_password(self) -> bool:
        return self.password_hash is not None

    # ========================================================================
    # OTP Authentication Methods
    # ========================================================================

    def generate_otp(self) -> Tuple[str, str]:
        """Generate a 6-digit OTP and its SHA-256 hash."""
        otp_int = secrets.randbelow(1000000)
        otp = f"{otp_int:06d}"
        otp_hash = hashlib.sha256(otp.encode()).hexdigest()
        return otp, otp_hash

    def verify_otp(self, provided_otp: str, stored_hash: str) -> bool:
        """Verify a provided OTP against its hash."""
        if not provided_otp or not stored_hash:
            return False
        provided_hash = hashlib.sha256(provided_otp.encode()).hexdigest()
        return secrets.compare_digest(provided_hash, stored_hash)

   
    def can_request_otp(self) -> Tuple[bool, Optional[str]]:
        """Check if user can request a new OTP."""
        if self.is_locked_out():
            return False, "Account is locked. Please try again later."
        
        # AUTO-RESET: If last request was more than 1 hour ago, reset counter
        if self.last_otp_request:
            last_request = ensure_aware(self.last_otp_request)
            hours_since_last = (now_kampala() - last_request).total_seconds() / 3600
            
            if hours_since_last >= 1:
                # Reset the counter - hour window has passed
                self.otp_request_count = 0

        if self.otp_request_count >= self.max_otp_requests_per_hour:
            # Calculate when the oldest request expires
            if self.last_otp_request:
                last_request = ensure_aware(self.last_otp_request)
                reset_in_seconds = 3600 - (now_kampala() - last_request).total_seconds()
                if reset_in_seconds > 0:
                    minutes = int(reset_in_seconds // 60) + 1
                    return False, f"Too many OTP requests. Try again in {minutes} minute(s)."
            return False, "Too many OTP requests. Please wait an hour before requesting again."
        
        # 30-second cooldown between requests
        if self.last_otp_request:
            last_request = ensure_aware(self.last_otp_request)
            time_since_last = (now_kampala() - last_request).total_seconds()
            if time_since_last < 30:
                return False, f"Please wait {int(30 - time_since_last)} seconds before requesting another OTP."
        
        return True, None

    def record_otp_request(self):
        """Record that an OTP was requested (for rate limiting)."""
        now = now_kampala()
        if self.last_otp_request:
            last_request = ensure_aware(self.last_otp_request)
            if (now - last_request).total_seconds() > 3600:
                self.otp_request_count = 0
        
        self.otp_request_count += 1
        self.last_otp_request = now

    # ========================================================================
    # Account Lockout Methods
    # ========================================================================

    def record_failed_attempt(self):
        """Record a failed login attempt. Locks immediately when max attempts reached.
        
        IMPORTANT: Always call is_locked_out() first to auto-clear any expired
        lockout before incrementing. This prevents a stale lock (is_locked=True
        but locked_until in the past) from causing a fresh user on the same
        browser/session to be incorrectly blocked.
        """
        # Auto-clear any expired lockout before recording a new failure.
        # Without this, a different user logging in on the same browser after
        # the lockout window expires would still see is_locked=True and get
        # immediately re-locked on their very first wrong attempt.
        self.is_locked_out()  # clears stale lock as a side-effect if expired

        self.failed_login_attempts += 1
        
        if self.failed_login_attempts >= self.max_failed_attempts:
            self.is_locked = True
            self.locked_until = now_kampala() + timedelta(minutes=self.lockout_duration_minutes)
            import logging
            logging.getLogger(__name__).warning(
                f"User {self.id} ({self.username}) locked out after "
                f"{self.failed_login_attempts} failed attempts. Locked until {self.locked_until}"
            )

    def reset_failed_attempts(self):
        """Reset failed attempts and unlock the account."""
        self.failed_login_attempts = 0
        self.is_locked = False
        self.locked_until = None

    def is_locked_out(self) -> bool:
        """Check if the account is currently locked out."""
        if not self.is_locked:
            return False
        if self.locked_until is None:
            return False
        
        now = now_kampala()
        locked_until = ensure_aware(self.locked_until)
        
        if now > locked_until:
            self.reset_failed_attempts()
            return False
        
        return True

    def get_lockout_remaining_seconds(self) -> int:
        """Get remaining lockout time in seconds."""
        if not self.is_locked or self.locked_until is None:
            return 0
        
        now = now_kampala()
        locked_until = ensure_aware(self.locked_until)
        
        if now >= locked_until:
            return 0
        
        return max(0, int((locked_until - now).total_seconds()))

    def get_lockout_remaining_ms(self) -> Optional[int]:
        """Get remaining lockout time in milliseconds for frontend."""
        if not self.is_locked or self.locked_until is None:
            return None
        
        now = now_kampala()
        locked_until = ensure_aware(self.locked_until)
        
        if now >= locked_until:
            return None
        
        return int(locked_until.timestamp() * 1000)

    def get_lockout_end_time_ms(self) -> Optional[int]:
        """Get the timestamp (milliseconds) when lockout ends."""
        if not self.is_locked or self.locked_until is None:
            return None
        
        locked_until = ensure_aware(self.locked_until)
        return int(locked_until.timestamp() * 1000)

    # ========================================================================
    # Authentication Entry Points
    # ========================================================================

    def authenticate_with_password(self, password: str) -> Tuple[bool, Optional[str], Optional[dict]]:
        """Authenticate user with password."""
        # Use is_locked_out() — not self.is_locked — so expired lockouts are
        # auto-cleared. Checking self.is_locked directly would keep a user
        # (or a different user on the same browser) blocked even after the
        # lockout window has passed.
        if self.is_locked_out():
            remaining_seconds = self.get_lockout_remaining_seconds()
            lockout_info = {
                'remaining_seconds': remaining_seconds,
                'end_time_ms': self.get_lockout_end_time_ms(),
                'duration_minutes': self.lockout_duration_minutes,
                'failed_attempts': self.failed_login_attempts,
                'max_attempts': self.max_failed_attempts
            }
            minutes = max(1, (remaining_seconds + 59) // 60)
            return False, f"Account locked. Try again in {minutes} minute(s).", lockout_info
        
        if not self.check_password(password):
            self.record_failed_attempt()
            remaining_attempts = self.max_failed_attempts - self.failed_login_attempts
            
            if self.is_locked:
                remaining_seconds = self.get_lockout_remaining_seconds()
                lockout_info = {
                    'remaining_seconds': remaining_seconds,
                    'end_time_ms': self.get_lockout_end_time_ms(),
                    'duration_minutes': self.lockout_duration_minutes,
                    'failed_attempts': self.failed_login_attempts,
                    'max_attempts': self.max_failed_attempts
                }
                minutes = max(1, (remaining_seconds + 59) // 60)
                return False, f"Account locked due to too many failed attempts. Try again in {minutes} minute(s).", lockout_info
            
            return False, f"Invalid password. {remaining_attempts} attempt(s) remaining.", None
        
        self.reset_failed_attempts()
        self.mark_online()
        return True, "Login successful", None

    def authenticate_with_otp(self, otp: str, otp_hash: str) -> Tuple[bool, Optional[str], Optional[dict]]:
        """Authenticate user with OTP."""
        # Same fix as authenticate_with_password: use is_locked_out() so that
        # expired lockouts are cleared automatically before checking.
        if self.is_locked_out():
            remaining_seconds = self.get_lockout_remaining_seconds()
            lockout_info = {
                'remaining_seconds': remaining_seconds,
                'end_time_ms': self.get_lockout_end_time_ms(),
                'duration_minutes': self.lockout_duration_minutes,
                'failed_attempts': self.failed_login_attempts,
                'max_attempts': self.max_failed_attempts
            }
            minutes = max(1, (remaining_seconds + 59) // 60)
            return False, f"Account locked. Try again in {minutes} minute(s).", lockout_info
        
        if not self.otp_enabled:
            return False, "OTP authentication is not enabled for this account.", None
        
        if not self.verify_otp(otp, otp_hash):
            self.record_failed_attempt()
            remaining_attempts = self.max_failed_attempts - self.failed_login_attempts
            
            if self.is_locked:
                remaining_seconds = self.get_lockout_remaining_seconds()
                lockout_info = {
                    'remaining_seconds': remaining_seconds,
                    'end_time_ms': self.get_lockout_end_time_ms(),
                    'duration_minutes': self.lockout_duration_minutes,
                    'failed_attempts': self.failed_login_attempts,
                    'max_attempts': self.max_failed_attempts
                }
                minutes = max(1, (remaining_seconds + 59) // 60)
                return False, f"Account locked due to too many failed attempts. Try again in {minutes} minute(s).", lockout_info
            
            return False, f"Invalid OTP. {remaining_attempts} attempt(s) remaining.", None
        
        self.reset_failed_attempts()
        self.mark_online()
        return True, "Login successful", None

    # ========================================================================
    # Presence Methods
    # ========================================================================

    def mark_online(self) -> None:
        """Set the user as online. Call this immediately after a successful login.
        The caller must commit the session for the change to persist."""
        self.is_active = True
        self.last_seen = now_kampala()

    def mark_offline(self) -> None:
        """Set the user as offline. Call this on logout (or session expiry).
        Records last_seen so you can display 'last seen X ago' while offline.
        The caller must commit the session for the change to persist."""
        self.is_active = False
        self.last_seen = now_kampala()

    def logout(self) -> None:
        """Convenience wrapper: marks user offline. Call from your logout view,
        then db.session.commit().

        Example::

            @app.route('/logout')
            @login_required
            def logout_view():
                current_user.logout()
                db.session.commit()
                logout_user()          # flask-login
                return redirect(url_for('auth.login'))
        """
        self.mark_offline()

    # ========================================================================
    # Utility Methods
    # ========================================================================

    def to_dict(self, include_sensitive: bool = False) -> dict:
        """Convert user to dictionary for API responses."""
        data = {
            'id': self.id,
            'username': self.username,
            'phone': self.get_formatted_phone(),
            'email': self.email,
            'role': self.role,
            'avatar_url': self.avatar_url,
            'timezone': self.timezone,
            'is_active': self.is_active,
            'last_seen': self.last_seen.isoformat() if self.last_seen else None,
            'is_deleted': self.is_deleted,
            'is_locked': self.is_locked,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
        
        if include_sensitive:
            data.update({
                'otp_enabled': self.otp_enabled,
                'phone_verified': self.phone_verified,
                'receive_push': self.receive_push,
                'mute_notifications': self.mute_notifications,
                'failed_attempts': self.failed_login_attempts,
                'max_attempts': self.max_failed_attempts,
                'lockout_remaining_seconds': self.get_lockout_remaining_seconds(),
                'lockout_end_time_ms': self.get_lockout_end_time_ms(),
            })
        
        return data

    def __repr__(self):
        return f"<User {self.username} ({self.get_formatted_phone()})>"


# =============================================================================
# SECTION 4: OTP Session Model
# =============================================================================

class OTPSession(database.Model):
    """Temporary OTP storage. OTPs are stored hashed and expire after a short time."""
    __tablename__ = 'otp_sessions'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False) 
    otp_hash = Column(String(64), nullable=False)
    purpose = Column(String(50), nullable=False, default='login')
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=now_kampala, nullable=False)
    used = Column(Boolean, default=False, nullable=False)
     
    # Relationships 
    user = relationship("User", backref="otp_sessions", lazy="select")
    

    __table_args__ = (
        Index('idx_otp_user', 'user_id'),
        Index('idx_otp_expires', 'expires_at'),
        Index('idx_otp_used', 'used'),
    )

    def is_valid(self) -> bool:
        """Check if OTP session is still valid (not used and not expired)."""
        if self.used:
            return False
        
        now = now_kampala()
        expires_at = ensure_aware(self.expires_at)
        
        if now > expires_at:
            return False
        return True

    def mark_used(self):
        """Mark OTP session as used (prevents replay attacks)."""
        self.used = True

    def get_remaining_seconds(self) -> int:
        """Get remaining time in seconds before OTP expires."""
        now = now_kampala()
        expires_at = ensure_aware(self.expires_at)
        
        if now >= expires_at:
            return 0
        
        return int((expires_at - now).total_seconds())

    @classmethod
    def create(cls, user_id: int, otp_hash: str, purpose: str = 'login', expiry_minutes: int = 10) -> 'OTPSession':
        """Create a new OTP session."""
        return cls(
            user_id=user_id,
            otp_hash=otp_hash,
            purpose=purpose,
            expires_at=now_kampala() + timedelta(minutes=expiry_minutes)
        )

    def __repr__(self):
        return f"<OTPSession user={self.user_id} purpose={self.purpose} valid={self.is_valid()}>"