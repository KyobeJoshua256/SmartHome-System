from __future__ import annotations

from datetime import datetime

from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    SelectField,
    SelectMultipleField,
    StringField,
    TimeField,
)
from wtforms.fields import DateTimeField
from wtforms import validators
from wtforms.validators import DataRequired, Length, Optional, ValidationError

from HomeServer import database
from HomeServer.models.rooms import Room, RoomMember, RoomStatus, RoomType
from HomeServer.models.users import User, UserRole
from HomeServer.models.utils import KAMPALA_TZ, VALID_DAY_NAMES, now_kampala

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
    invalid = set(days) - _VALID_DAY_NAMES
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

    ``user_id = 0`` is the sentinel for "no user assigned" (VACANT).
    The admin view's ``on_model_change`` converts 0 → None before saving.
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

        # 0 is the "vacant" sentinel; admin view coerces it to None on save
        members = (
            database.session.query(User)
            .filter(User.role != UserRole.GUEST.value, User.is_active.is_(True))
            .order_by(User.username)
            .all()
        )
        self.user_id.choices = [
            (0, "--- Vacant (no member) ---"),
        ] + [(u.id, u.username) for u in members]


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
        default=_now_kampala,
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
            .filter(User.role == UserRole.GUEST.value, User.is_active.is_(True))
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

    Only VACANT / INACTIVE RoomMember rows are offered — this prevents
    double-allocation and matches ``RoomService.allocate_to_member`` semantics.
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

        members = (
            database.session.query(User)
            .filter(User.role != UserRole.GUEST.value, User.is_active.is_(True))
            .order_by(User.username)
            .all()
        )
        self.user_id.choices = [(u.id, u.username) for u in members]


# ---------------------------------------------------------------------------
# AllocateGuestForm — workflow: pick a vacant room → assign a guest
# ---------------------------------------------------------------------------

class AllocateGuestForm(FlaskForm):
    """
    Allocate a VACANT room to a guest with time-bounded access.
    Admin-only; ``invited_by`` is always derived from ``current_user`` in
    the view — it is not exposed as a form field to prevent spoofing.
    """

    room_id = SelectField(
        "Vacant Room",
        coerce=int,
        validators=[DataRequired()],
        description="Only rooms with VACANT status are offered.",
    )
    guest_id = SelectField("Guest User", coerce=int, validators=[DataRequired()])
    expires_at = DateTimeField(
        "Expires At",
        format="%Y-%m-%d %H:%M:%S",
        validators=[DataRequired()],
        default=_now_kampala,
        description="Hard expiry enforced by the APScheduler job.",
    )
    valid_from = TimeField(
        "Daily Window — From",
        format="%H:%M:%S",
        validators=[Optional()],
    )
    valid_until = TimeField(
        "Daily Window — Until",
        format="%H:%M:%S",
        validators=[Optional()],
    )
    valid_days = StringField(
        "Valid Days",
        validators=[Optional(), Length(max=50)],
        description="e.g. 'mon,wed,fri' — leave blank for all days.",
    )
    can_view = BooleanField("Can View", default=True)
    can_control = BooleanField("Can Control", default=True)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        vacant_rooms = (
            database.session.query(Room)
            .filter(
                Room.status == RoomStatus.VACANT,
                Room.room_type == RoomType.GUEST,
            )
            .order_by(Room.name)
            .all()
        )
        self.room_id.choices = [
            (r.id, f"{r.name}  [{r.room_type.value}]")
            for r in vacant_rooms
        ]

        guests = (
            database.session.query(User)
            .filter(User.role == UserRole.GUEST.value, User.is_active.is_(True))
            .order_by(User.username)
            .all()
        )
        self.guest_id.choices = [(u.id, u.username) for u in guests]

    def validate_valid_days(self, field: StringField) -> None:
        """Normalise and validate the comma-separated day string."""
        if not field.data or not field.data.strip():
            field.data = None
            return
        field.data = ",".join(_parse_valid_days(field.data))

    def validate_valid_until(self, field: TimeField) -> None:
        """Ensure valid_until is after valid_from when both are provided."""
        if field.data and self.valid_from.data:
            if field.data <= self.valid_from.data:
                raise ValidationError("'Valid Until' must be later than 'Valid From'.")

    def validate_expires_at(self, field: DateTimeField) -> None:
        """Ensure expiry is in the future."""
        if field.data:
            now = datetime.now(tz=KAMPALA_TZ)
            # Make expires_at tz-aware for comparison if it came in naive
            expires = field.data
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=KAMPALA_TZ)
            if expires <= now:
                raise ValidationError("Expiry must be a future date and time.")


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