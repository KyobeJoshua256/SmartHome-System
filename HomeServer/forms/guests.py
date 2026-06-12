from __future__ import annotations

from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SelectField
from wtforms.validators import DataRequired, Length, Optional, Regexp

from HomeServer import database
from HomeServer.models.rooms import Room, RoomStatus, RoomType
from HomeServer.models.guests import Guest, GuestStatus

_GUEST_STATUS_CHOICES = [(s.value, s.value.replace("_", " ").title()) for s in GuestStatus]


class GuestForm(FlaskForm):
    """Create or edit a Guest record."""

    full_name = StringField(
        "Full Name",
        validators=[DataRequired(), Length(min=2, max=120)],
        description="Visitor's full name (min 2 characters).",
    )
    phone = StringField(
        "Phone",
        validators=[
            Optional(),
            Regexp(r"^\+?[0-9\s\-\(\)]{9,18}$", message="Use format: +256700000000"),
        ],
        description="Optional. International format preferred.",
    )
    status = SelectField(
        "Status",
        choices=_GUEST_STATUS_CHOICES,
        validators=[DataRequired()],
        default=GuestStatus.EXPECTED.value,
        description="Lifecycle state — usually managed via check-in/check-out actions.",
    )
    room_id = SelectField(
        "Room",
        coerce=int,
        validators=[Optional()],
        description="Only GUEST-type rooms (vacant or already hosting guests) are shown.",
    )
    notes = TextAreaField(
        "Notes",
        validators=[Optional(), Length(max=2000)],
        description="Free-form notes about this visit.",
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        rooms = (
            database.session.query(Room)
            .filter(
                Room.room_type == RoomType.GUEST,
                Room.status.in_([RoomStatus.VACANT, RoomStatus.ACTIVE]),
            )
            .order_by(Room.name)
            .all()
        )

        choices = [(0, "--- None ---")]
        for r in rooms:
            label = f"{r.name}  [{r.room_type.value}]"
            if r.status == RoomStatus.ACTIVE:
                occupants = (
                    database.session.query(Guest)
                    .filter(
                        Guest.room_id == r.id,
                        Guest.status == GuestStatus.CHECKED_IN,
                    )
                    .count()
                )
                if occupants:
                    label += f"  — occupied ({occupants})"
            choices.append((r.id, label))

        # When editing a guest who already has a room assigned, keep that
        # room selectable even if it no longer matches the filters above
        # (e.g. room_type or status changed since assignment).
        obj = kwargs.get("obj")
        if obj is not None and obj.room_id and obj.room_id not in [c[0] for c in choices]:
            existing_room = database.session.get(Room, obj.room_id)
            if existing_room:
                choices.append(
                    (existing_room.id, f"{existing_room.name}  [{existing_room.room_type.value}]")
                )

        self.room_id.choices = choices

    def validate_room_id(self, field):
        if field.data == 0:
            field.data = None