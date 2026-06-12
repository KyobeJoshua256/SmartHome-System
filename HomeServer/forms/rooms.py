from __future__ import annotations

from datetime import datetime

from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    SelectField,
    SelectMultipleField,
    StringField,
    TimeField,
    TextAreaField,  # For QuickGuestForm
)
from wtforms.fields import DateTimeField
from wtforms.validators import DataRequired, Length, Optional, ValidationError

from HomeServer import database
from HomeServer.models.rooms import Room, RoomMember, RoomStatus, RoomType
from HomeServer.models.users import User, UserRole
from HomeServer.models.utils import KAMPALA_TZ, VALID_DAY_NAMES, to_uganda
from HomeServer.models.guests import Guest, GuestStatus  

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------



_ROOM_TYPE_CHOICES = [(rt.value, rt.value.capitalize()) for rt in RoomType]
_ROOM_STATUS_CHOICES = [(rs.value, rs.value.capitalize()) for rs in RoomStatus]



def _parse_valid_days(raw: str) -> list[str]:
    """
    Split a comma-separated day string into a normalised list.
    Raises ``ValidationError`` if any entry is not a recognised short day name.
    """
    days = [d.strip().lower() for d in raw.split(",") if d.strip()]
    invalid = set(days) - VALID_DAY_NAMES
    if invalid:
        raise ValidationError(
            f"Unrecognised day name(s): {', '.join(sorted(invalid))}. "
            f"Use: mon tue wed thu fri sat sun"
        )
    return days


# ---------------------------------------------------------------------------
# RoomForm — create / edit a physical or logical room
# ---------------------------------------------------------------------------

class RoomForm(FlaskForm):
    """Create or edit a Room record."""

    name = StringField(
        "Room Name",
        validators=[DataRequired(), Length(min=1, max=100)],
        description="Unique human-readable label (e.g. 'Master Bedroom', 'Kitchen').",
    )
    room_type = SelectField(
        "Room Type",
        choices=_ROOM_TYPE_CHOICES,
        validators=[DataRequired()],
        description="PERSONAL — belongs to one member; SHARED — all members; GUEST — guest pool.",
    )
    status = SelectField(
        "Status",
        choices=_ROOM_STATUS_CHOICES,
        validators=[DataRequired()],
        description="VACANT on creation; managed automatically by RoomService thereafter.",
    )


# ---------------------------------------------------------------------------
# RoomMemberForm — direct CRUD on a RoomMember allocation row
# ---------------------------------------------------------------------------

class RoomMemberForm(FlaskForm):
    """
    Edit a RoomMember allocation row directly.
    
    Shows ALL users for allocation editing because room access shouldn't
    be tied to user's is_active status.
    """

    room_id = SelectField("Room", coerce=int, validators=[DataRequired()])
    user_id = SelectField(
        "Member",
        coerce=int,
        validators=[Optional()],
        description="Leave as '--- Vacant ---' to release the room to the pool.",
    )
    room_type = SelectField(
        "Room Type",
        choices=_ROOM_TYPE_CHOICES,
        default=RoomType.PERSONAL.value,
    )
    status = SelectField(
        "Status",
        choices=_ROOM_STATUS_CHOICES,
        default=RoomStatus.ACTIVE.value,
        description="Status is managed automatically when user assignment changes.",
    )
    can_view = BooleanField("Can View", default=True)
    can_control = BooleanField("Can Control", default=True)
    can_manage = BooleanField(
        "Can Manage",
        default=False,
        description="Allows changing room settings — admin-granted only.",
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.room_id.choices = [
            (r.id, f"{r.name}  [{r.room_type.value}]")
            for r in database.session.query(Room).order_by(Room.name).all()
        ]

        # ✅ FIX: Show ALL users (not just active ones)
        # 0 is the "vacant" sentinel; admin view coerces it to None on save
        all_users = (
            database.session.query(User)
            .filter(User.is_deleted == False)  # Only exclude deleted users
            .order_by(User.is_active.desc(), User.username)
            .all()
        )

        self.user_id.choices = [(0, "--- Vacant (no member) ---")]
        
        for user in all_users:
            status_indicator = ""
            if not user.is_active:
                status_indicator = " [OFFLINE]"
            elif user.is_locked:
                status_indicator = " [LOCKED]"
            
            display_name = f"{user.username} ({user.role}){status_indicator}"
            self.user_id.choices.append((user.id, display_name))

# ---------------------------------------------------------------------------
# GuestRoomForm — direct CRUD on a GuestRoom allocation row
# ---------------------------------------------------------------------------

class GuestRoomForm(FlaskForm):
    """
    Edit a GuestRoom allocation row directly.

    ``status`` is intentionally excluded from this form — guest status is
    managed entirely by the lifecycle (``GuestRoom.expire()`` / scheduler).
    Admins should use the Dashboard's "Expire" action instead.
    """

    room_id = SelectField("Room", coerce=int, validators=[DataRequired()])
    guest_id = SelectField("Guest User", coerce=int, validators=[DataRequired()])
    invited_by_id = SelectField(
        "Invited By (Admin)",
        coerce=str,
        validators=[DataRequired()],
        description="The admin who is granting this access.",
    )
    expires_at = DateTimeField(
        "Expires At",
        format="%Y-%m-%d %H:%M:%S",
        validators=[DataRequired()],
        default=to_uganda,
        description="Hard expiry — access is revoked by the scheduler at this moment.",
    )
    valid_from = TimeField(
        "Daily Window — From",
        format="%H:%M:%S",
        validators=[Optional()],
        description="Leave blank for no daily time restriction.",
    )
    valid_until = TimeField(
        "Daily Window — Until",
        format="%H:%M:%S",
        validators=[Optional()],
        description="Must be after 'Valid From'. Leave both blank for all-day access.",
    )
    valid_days = StringField(
        "Valid Days",
        validators=[Optional(), Length(max=50)],
        description="Comma-separated short names, e.g. 'mon,wed,fri'. Leave blank for all days.",
    )
    can_view = BooleanField("Can View", default=True)
    can_control = BooleanField("Can Control", default=True)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.room_id.choices = [
            (r.id, f"{r.name}  [{r.room_type.value}]")
            for r in database.session.query(Room).order_by(Room.name).all()
        ]

        guests = (
            database.session.query(User)
            .filter(User.is_active.is_(True))
            .order_by(User.username)
            .all()
        )

        self.guest_id.choices = [(u.id, u.username) for u in guests]

        admins = (
            database.session.query(User)
            .filter(User.role == UserRole.ADMIN.value)
            .order_by(User.username)
            .all()
        )
        self.invited_by_id.choices = [(u.username, u.username) for u in admins]

    def validate_valid_days(self, field: StringField) -> None:
        """Normalise and validate the comma-separated day string."""
        if not field.data or not field.data.strip():
            field.data = None
            return
        field.data = ",".join(_parse_valid_days(field.data))  # store as normalised string

    def validate_valid_until(self, field: TimeField) -> None:
        """Ensure valid_until is after valid_from when both are provided."""
        if field.data and self.valid_from.data:
            if field.data <= self.valid_from.data:
                raise ValidationError("'Valid Until' must be later than 'Valid From'.")


# ---------------------------------------------------------------------------
# AllocateMemberForm — workflow: pick a vacant slot → assign a member
# ---------------------------------------------------------------------------

class AllocateMemberForm(FlaskForm):
    """
    Allocate a room from the vacant pool to a household member.
    
    Shows ALL users regardless of is_active status because:
    - A user might be inactive but still need room access
    - is_active might be used for login suspension, not room access
    - Room allocation should be based on user existence, not active status
    """

    room_member_id = SelectField(
        "Vacant Room Slot",
        coerce=int,
        validators=[DataRequired()],
        description="Only slots currently in the vacant pool are shown.",
    )
    user_id = SelectField("Member", coerce=int, validators=[DataRequired()])
    room_type = SelectField(
        "Room Type",
        choices=_ROOM_TYPE_CHOICES,
        default=RoomType.PERSONAL.value,
    )
    can_view = BooleanField("Can View", default=True)
    can_control = BooleanField("Can Control", default=True)
    can_manage = BooleanField("Can Manage", default=False)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        # Vacant room slots (unchanged)
        vacant_slots = (
            database.session.query(RoomMember)
            .filter(
                RoomMember.status.in_([RoomStatus.VACANT, RoomStatus.INACTIVE]),
                RoomMember.room_type != RoomType.GUEST,
            )
            .join(RoomMember.room)
            .order_by(Room.name)
            .all()
        )
        self.room_member_id.choices = [
            (
                rm.id,
                f"{rm.room.name}  [{rm.room.room_type.value}]  —  {rm.status.value.upper()}",
            )
            for rm in vacant_slots
        ]

        # ✅ FIX: Show ALL users - remove the is_active filter
        # Show all users regardless of active status
        all_users = (
            database.session.query(User)
            .filter(User.is_deleted == False)  # Only exclude deleted users
            .order_by(
                User.is_active.desc(),  # Show active users first for better UX
                User.username
            )
            .all()
        )
        
        # Create choices with status indicator
        self.user_id.choices = []
        
        # Add a placeholder option
        self.user_id.choices.append((0, "--- Select User ---"))
        
        for user in all_users:
            # Add status indicator to help admin make informed decision
            status_label = ""
            if not user.is_active:
                status_label = " [INACTIVE]"
            elif user.is_locked:
                status_label = " [LOCKED]"
            
            # Format the display name with role and status
            display_name = f"{user.username} ({user.role}){status_label}"
            self.user_id.choices.append((user.id, display_name))
        
        # Optional: Add debug logging to see what users are being loaded
        import logging
        logger = logging.getLogger(__name__)
        logger.debug(f"Loaded {len(all_users)} users for room allocation dropdown")

# ---------------------------------------------------------------------------
# AllocateGuestForm — workflow: pick a vacant room → assign a guest
# ---------------------------------------------------------------------------
class AllocateGuestForm(FlaskForm):
    """
    Allocate a VACANT room to a PHYSICAL WALK-IN GUEST.
    """
    
    room_id = SelectField(
        "Vacant Room", 
        coerce=int, 
        validators=[DataRequired()],
        choices=[(0, "--- Select Vacant Room ---")],  # Use 0 as sentinel
        description="Only GUEST-type rooms with VACANT status are offered."
    )
    
    guest_id = SelectField(
        "Select Walk-in Guest", 
        coerce=int, 
        validators=[DataRequired()],
        choices=[(0, "--- Select Walk-in Guest ---")],
        description="Physical visitor who will occupy this room."
    )
    
    expected_checkout = DateTimeField(
        "Expected Checkout",
        format="%Y-%m-%d %H:%M:%S",
        validators=[Optional()],
        description="When the guest is expected to leave (optional)."
    )
    
    can_view = BooleanField("Can View", default=True)
    can_control = BooleanField("Can Control", default=False)
    notes = TextAreaField("Additional Notes", validators=[Optional(), Length(max=500)])

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        
        from HomeServer.models.guests import GuestStatus
        
        # Room choices
        vacant_rooms = (
            database.session.query(Room)
            .filter(
                Room.status == RoomStatus.VACANT,
                Room.room_type == RoomType.GUEST,
            )
            .order_by(Room.name)
            .all()
        )
        
        self.room_id.choices = [(0, "--- Select Vacant Room ---")]
        for r in vacant_rooms:
            self.room_id.choices.append((r.id, f"{r.name}"))
        
        # Guest choices
        physical_guests = (
            database.session.query(Guest)
            .filter(
                Guest.status != GuestStatus.CHECKED_IN,
                Guest.status != GuestStatus.CHECKED_OUT,
            )
            .order_by(Guest.full_name)
            .all()
        )
        
        self.guest_id.choices = [(0, "--- Select Walk-in Guest ---")]
        for guest in physical_guests:
            status_icon = "🔄" if guest.status.value == "expected" else "✓"
            phone_info = f" ({guest.phone})" if guest.phone else ""
            display_name = f"{status_icon} {guest.full_name}{phone_info} - {guest.status.value.upper()}"
            self.guest_id.choices.append((guest.id, display_name))
        
        # Debug logging
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"Loaded {len(physical_guests)} physical guests")
        logger.info(f"Loaded {len(vacant_rooms)} vacant rooms")

    def validate_room_id(self, field):
        """Validate room selection"""
        if field.data == 0 or field.data is None:
            raise ValidationError('Please select a room')
        
        # Verify room is still vacant
        room = database.session.get(Room, field.data)
        if room and room.status != RoomStatus.VACANT:
            raise ValidationError(f"Room '{room.name}' is no longer vacant. Please select another room.")
        return field.data
    
    def validate_guest_id(self, field):
        """Validate guest selection"""
        if field.data == 0 or field.data is None:
            raise ValidationError('Please select a guest')
        
        # Verify guest is not already checked in
        from HomeServer.models.guests import GuestStatus
        guest = database.session.get(Guest, field.data)
        if guest and guest.status == GuestStatus.CHECKED_IN:
            raise ValidationError(f"Guest '{guest.full_name}' is already checked into another room.")
        return field.data
    
    def validate_expected_checkout(self, field):
        """Ensure expected checkout is in the future if provided"""
        if field.data:
            now = datetime.now(tz=KAMPALA_TZ)
            if field.data.tzinfo is None:
                field.data = field.data.replace(tzinfo=KAMPALA_TZ)
            if field.data <= now:
                raise ValidationError("Expected checkout must be in the future.")

class QuickGuestForm(FlaskForm):
    """Quick create a physical guest for room allocation."""
    
    full_name = StringField(
        "Guest Full Name",
        validators=[DataRequired(), Length(min=2, max=120)],
        render_kw={"placeholder": "e.g., John Doe"}
    )
    phone = StringField(
        "Phone Number",
        validators=[Optional(), Length(max=20)],
        render_kw={"placeholder": "+256XXXXXXXXX"}
    )
    notes = TextAreaField(
        "Notes",
        validators=[Optional()],
        render_kw={"placeholder": "Any additional information about this guest"}
    )



# ---------------------------------------------------------------------------
# BulkRoomOperationForm — batch operations across multiple rooms
# ---------------------------------------------------------------------------

class BulkRoomOperationForm(FlaskForm):
    """
    Select one or more rooms and apply a bulk action.

    Available actions mirror the operations in ``BulkRoomOperationView`` —
    keep choices in sync if new actions are added there.
    """

    action = SelectField(
        "Action",
        choices=[
            ("set_vacant", "Release to Vacant Pool (remove member allocation)"),
            ("set_shared", "Set Room Type → SHARED"),
            ("set_personal", "Set Room Type → PERSONAL"),
            ("set_guest", "Set Room Type → GUEST"),
            ("activate", "Activate Room (mark VACANT / available)"),
            ("deactivate", "Deactivate Room (release member + mark VACANT)"),
        ],
        validators=[DataRequired()],
    )
    room_ids = SelectMultipleField(
        "Rooms",
        coerce=int,
        validators=[DataRequired()],
        description="Hold Ctrl / Cmd to select multiple rooms.",
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        rooms = database.session.query(Room).order_by(Room.name).all()
        self.room_ids.choices = [
            (r.id, f"{r.name}  [{r.room_type.value}]  —  {r.status.value.upper()}")
            for r in rooms
        ]