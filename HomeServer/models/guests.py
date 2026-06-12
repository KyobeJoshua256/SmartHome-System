from __future__ import annotations

import enum
import re
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Enum as SqlaEnum,
    ForeignKey, Index, Integer, String, Text,
)
from sqlalchemy.orm import relationship, validates

from HomeServer import database
from .utils import TimestampMixin, now_kampala, KAMPALA_TZ
from .rooms import Room, RoomStatus


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class GuestStatus(enum.Enum):
    EXPECTED    = "expected"
    CHECKED_IN  = "checked_in"
    CHECKED_OUT = "checked_out"


# ---------------------------------------------------------------------------
# Guest
# ---------------------------------------------------------------------------

class Guest(TimestampMixin, database.Model):
    """A physical visitor to the home."""
    __tablename__ = "guests"

    id        = Column(Integer, primary_key=True)
    full_name = Column(String(120), nullable=False)
    phone     = Column(String(20), nullable=True)
    notes     = Column(Text, nullable=True)

    status = Column(
        SqlaEnum(GuestStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=GuestStatus.EXPECTED,
    )

    room_id     = Column(Integer, ForeignKey("rooms.id"), nullable=True)
    added_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    checked_in_at     = Column(DateTime(timezone=True), nullable=True)
    checked_out_at    = Column(DateTime(timezone=True), nullable=True)
    expected_checkout = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    room     = relationship("Room", lazy="select")
    added_by = relationship("User", foreign_keys=[added_by_id], lazy="select")

    __table_args__ = (
        Index("ix_guests_status", "status"),
    )

    # ── Validators ────────────────────────────────────────────────────────

    @validates("phone")
    def validate_phone(self, key, phone):
        if not phone:
            return phone
        clean = re.sub(r"[\s\-\(\)]", "", phone)
        if not re.match(r"^\+?[0-9]{9,15}$", clean):
            raise ValueError("Invalid phone number. Use format: +256700000000")
        return clean if clean.startswith("+") else "+" + clean

    @validates("full_name")
    def validate_full_name(self, key, name):
        name = name.strip()
        if len(name) < 2:
            raise ValueError("Name must be at least 2 characters.")
        return name

    # ── Business logic ────────────────────────────────────────────────────

    def check_in(self, room_id: int) -> None:
        """
        Mark the guest as checked in and assign their room.

        Room status handling
        ---------------------
        Multiple guests can share the same GUEST-type room. The room's
        ``status`` is set to ACTIVE on first check-in and stays ACTIVE
        while at least one guest is currently checked into it.
        """
        self.status = GuestStatus.CHECKED_IN
        self.room_id = room_id
        self.checked_in_at = now_kampala()

        room = database.session.get(Room, room_id)
        if room and room.status != RoomStatus.ACTIVE:
            room.status = RoomStatus.ACTIVE

    def check_out(self) -> None:
        """
        Mark the guest as checked out and release their hold on the room.

        Room status handling
        ---------------------
        The room is only released back to VACANT when this is the *last*
        currently-checked-in guest in that room — co-occupants are not
        affected by one guest's checkout.
        """
        previous_room_id = self.room_id

        self.status = GuestStatus.CHECKED_OUT
        self.checked_out_at = now_kampala()
        self.room_id = None

        if previous_room_id is not None:
            other_checked_in = (
                Guest.query
                .filter(
                    Guest.room_id == previous_room_id,
                    Guest.status == GuestStatus.CHECKED_IN,
                    Guest.id != self.id,
                )
                .first()
            )
            if not other_checked_in:
                room = database.session.get(Room, previous_room_id)
                if room and room.status == RoomStatus.ACTIVE:
                    room.status = RoomStatus.VACANT

    def to_dict(self) -> dict:
        return {
            "id":                self.id,
            "full_name":         self.full_name,
            "phone":             self.phone,
            "notes":             self.notes,
            "status":            self.status.value,
            "room_id":           self.room_id,
            "checked_in_at":     self.checked_in_at.isoformat()  if self.checked_in_at  else None,
            "checked_out_at":    self.checked_out_at.isoformat() if self.checked_out_at else None,
            "expected_checkout": self.expected_checkout.isoformat() if self.expected_checkout else None,
            "added_by_id":       self.added_by_id,
            "created_at":        self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:
        return f"<Guest id={self.id} name={self.full_name!r} status={self.status.value}>"