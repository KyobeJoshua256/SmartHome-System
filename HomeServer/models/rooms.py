"""
HomeServer/models/rooms.py
==========================
SQLAlchemy models and service layer for the room allocation subsystem.

Architecture overview
---------------------
Room            Physical or logical space.  Devices attach here permanently.
RoomMember      Allocation of a room to a household member (homeowner / user).
GuestRoom       Time-bounded allocation of a room to a guest, admin-created.
RoomService     Stateless allocation helpers — callers own the transaction.

Lifecycle
---------
                  ┌──────────┐
            ┌────▶│  VACANT  │◀────────────────────────────┐
            │     └────┬─────┘                             │
  release / │          │ allocate_to_member /              │ expire()
  delete    │          │ allocate_to_guest                 │ release_member_room()
            │     ┌────▼─────┐                             │
            └─────│  ACTIVE  │─────────────────────────────┘
                  └──────────┘

  INACTIVE  — set on a RoomMember when its user account is deleted.
              Preserves device mappings; room re-enters the vacant pool.
  EXPIRED   — set on a GuestRoom when its access period elapses.

Notes
-----
- ``GuestRoom.invited_by_id`` is a FK to ``users.username``.  This is
  intentional so that the inviter's display name is preserved in the audit
  trail even if the user is later deleted.  Username changes are **not**
  supported on the User model, so referential integrity holds.
- The ``assign_default_shared_rooms`` event uses ``after_bulk_update_postexec``
  (via ``after_flush_postexec``) so ``User.id`` is guaranteed to be populated
  after the INSERT is reflected back from the DB.
- Midnight-crossing time windows (e.g. 22:00–02:00) are explicitly handled in
  ``GuestRoom.is_currently_accessible``.
"""

from __future__ import annotations

import enum
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum as SqlaEnum,
    ForeignKey,
    Integer,
    JSON,
    String,
    Time,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import relationship, validates
from sqlalchemy import event

from HomeServer import database
# from .users import User
from .utils import TimestampMixin, KAMPALA_TZ, VALID_DAY_NAMES


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class RoomType(enum.Enum):
    """Ownership / allocation category of a room."""
    PERSONAL = "personal"
    SHARED = "shared"
    GUEST = "guest"


class RoomStatus(enum.Enum):
    """
    Full lifecycle state of a room or allocation row.

    VACANT   — room exists but has no current owner; available for allocation.
    ACTIVE   — room is allocated and in use.
    INACTIVE — member was deleted; room retains devices/data, awaits re-allocation.
    EXPIRED  — guest access period elapsed; room released back to vacant pool.
    """
    VACANT = "vacant"
    ACTIVE = "active"
    INACTIVE = "inactive"
    EXPIRED = "expired"


# ---------------------------------------------------------------------------
# Room  (physical / logical room — device mappings live here)
# ---------------------------------------------------------------------------

class Room(TimestampMixin, database.Model):
    """
    A physical or logical room in the home (bedroom, kitchen, garage, etc.).

    Devices are mapped directly to ``Room`` so they survive member/guest
    allocation changes without requiring re-mapping.  A room begins life as
    VACANT and becomes ACTIVE once allocated to a member or guest.

    The ``name`` column is unique per installation — two rooms cannot share
    the same label, which prevents accidental duplicate allocations in the UI.
    """
    __tablename__ = "rooms"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    room_type = Column(
        SqlaEnum(RoomType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    status = Column(
        SqlaEnum(RoomStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=RoomStatus.VACANT,
    )

    # Relationships
    # devices = relationship("Device", back_populates="room", lazy="dynamic")
    members = relationship("RoomMember", back_populates="room", lazy="dynamic")
    guest_allocations = relationship("GuestRoom", back_populates="room", lazy="dynamic")

    __table_args__ = (
        Index("ix_rooms_status", "status"),
        Index("ix_rooms_room_type", "room_type"),
    )

    def __repr__(self) -> str:
        return (
            f"<Room id={self.id} name={self.name!r} "
            f"type={self.room_type.value} status={self.status.value}>"
        )


# ---------------------------------------------------------------------------
# RoomMember  (homeowner / household user allocation)
# ---------------------------------------------------------------------------

class RoomMember(TimestampMixin, database.Model):
    """
    Allocation of a room to a home member (homeowner or household user).

    Homeowners receive default access to their personal rooms and all shared
    rooms (kitchen, dining room, garage, etc.) automatically on account
    creation via the ``assign_default_shared_rooms`` event listener.

    When a member is deleted the allocation is **not** removed — it transitions
    to INACTIVE, preserving device mappings and room data.  The room then
    enters the vacant pool so it can be re-allocated to a new member without
    re-mapping devices.

    On re-allocation the existing VACANT/INACTIVE row is reused rather than
    creating a new one (see ``RoomService.allocate_to_member``).
    """
    __tablename__ = "room_members"

    id = Column(Integer, primary_key=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False)

    # ``user_id`` is nullable so the row can live in the VACANT/INACTIVE pool
    # without a member assigned.
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    room_type = Column(
        SqlaEnum(RoomType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=RoomType.PERSONAL,
    )
    status = Column(
        SqlaEnum(RoomStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=RoomStatus.ACTIVE,
    )

    # Granular permissions
    can_view = Column(Boolean, nullable=False, default=True)
    can_control = Column(Boolean, nullable=False, default=True)
    can_manage = Column(Boolean, nullable=False, default=False)  # admin-granted only

    # Populated when the room is released back to the vacant pool
    vacated_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    room = relationship("Room", back_populates="members", foreign_keys=[room_id])
    user = relationship("User", back_populates="room_memberships", foreign_keys=[user_id])

    __table_args__ = (
        # Prevent the *same user* from appearing twice in the *same room*.
        # Multiple *different* users CAN share a room (e.g. two children in a
        # shared bedroom) — each gets their own ACTIVE row with independent
        # permissions.  The constraint is on (room_id, user_id) — NOT on
        # room_id alone — so co-occupants are fully supported without any
        # schema change.
        UniqueConstraint("room_id", "user_id", name="uq_room_member"),
        Index("ix_room_members_room_id", "room_id"),
        Index("ix_room_members_status", "status"),
        Index("ix_room_members_user_id", "user_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<RoomMember id={self.id} room_id={self.room_id} "
            f"user_id={self.user_id} status={self.status.value}>"
        )


# ---------------------------------------------------------------------------
# GuestRoom  (time-bounded guest allocation, admin-created only)
# ---------------------------------------------------------------------------

class GuestRoom(TimestampMixin, database.Model):
    """
    Allocation of a room to a guest user, created exclusively by an admin.

    Access is time-bounded via ``expires_at`` and optionally restricted to
    specific days of the week (``valid_days``) and a daily time window
    (``valid_from`` / ``valid_until``).

    When access expires the scheduler calls ``expire()``, which sets status to
    EXPIRED, drops the guest reference, and the room re-enters the vacant
    pool for a new allocation.

    Midnight-crossing windows
    -------------------------
    ``is_currently_accessible`` correctly handles daily windows that cross
    midnight (e.g. 22:00–02:00) by comparing the elapsed-second offset from
    midnight rather than using a simple ``≤`` chain.
    """
    __tablename__ = "guest_rooms"

    id = Column(Integer, primary_key=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False)

    # Nullable after expiry so the row is kept for audit without a dangling FK
    guest_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    # FK to username — intentionally denormalised so the inviter's identity is
    # preserved in the audit trail even if their account is later deleted.
    # Username changes are not supported, so referential integrity holds.
    invited_by_id = Column(String, ForeignKey("users.username"), nullable=False)

    status = Column(
        SqlaEnum(RoomStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=RoomStatus.ACTIVE,
    )

    # Granular permissions (guests: view/control only; manage is never granted)
    can_view = Column(Boolean, nullable=False, default=True)
    can_control = Column(Boolean, nullable=False, default=True)
    can_manage = Column(Boolean, nullable=False, default=False)

    # Time-bounded access
    expires_at = Column(DateTime(timezone=True), nullable=False)
    valid_from = Column(Time, nullable=True)     # Daily window start (inclusive)
    valid_until = Column(Time, nullable=True)    # Daily window end   (inclusive)
    valid_days = Column(JSON, nullable=True)     # e.g. ["mon", "wed", "fri"]

    # Populated when access expires and the room is released
    vacated_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    room = relationship("Room", back_populates="guest_allocations", foreign_keys=[room_id])
    guest = relationship(
        "User",
        foreign_keys=[guest_id],
        back_populates="guest_rooms",
        primaryjoin="GuestRoom.guest_id == User.id",
    )
    invited_by = relationship(
        "User",
        foreign_keys=[invited_by_id],
        primaryjoin="GuestRoom.invited_by_id == User.username",
    )

    __table_args__ = (
        Index("ix_guest_rooms_status", "status"),
        Index("ix_guest_rooms_expires_at", "expires_at"),
        Index("ix_guest_rooms_guest_id", "guest_id"),
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @validates("valid_days")
    def validate_valid_days(self, key: str, value) -> list[str] | None:
        """Ensure ``valid_days`` contains only recognised short day names."""
        if value is None:
            return value
        invalid = {d.lower() for d in value} - VALID_DAY_NAMES
        if invalid:
            raise ValueError(
                f"Unrecognised day name(s) in valid_days: {sorted(invalid)}. "
                f"Allowed: {sorted(VALID_DAY_NAMES)}"
            )
        return [d.lower() for d in value]

    # ------------------------------------------------------------------
    # Business logic
    # ------------------------------------------------------------------

    def is_currently_accessible(self) -> bool:
        """
        Return ``True`` if this guest allocation is valid right now.

        Checks (in order):
        1. Allocation is ACTIVE.
        2. Hard expiry has not been reached.
        3. Today is an allowed weekday (if ``valid_days`` is configured).
        4. Current time falls within the daily window (if configured).
           Midnight-crossing windows (e.g. 22:00–02:00) are supported.

        Returns
        -------
        bool
        """
        if self.status != RoomStatus.ACTIVE:
            return False

        now = datetime.now(tz=KAMPALA_TZ)

        if now >= self.expires_at:
            return False

        if self.valid_days:
            today = now.strftime("%a").lower()
            if today not in self.valid_days:
                return False

        if self.valid_from is not None and self.valid_until is not None:
            current_time = now.time()
            if self.valid_from <= self.valid_until:
                # Normal window: e.g. 08:00–20:00
                if not (self.valid_from <= current_time <= self.valid_until):
                    return False
            else:
                # Midnight-crossing window: e.g. 22:00–02:00
                if not (current_time >= self.valid_from or current_time <= self.valid_until):
                    return False

        return True

    def expire(self) -> None:
        """
        Mark this allocation as EXPIRED and release *this guest's* hold on the room.

        Multiple concurrent guests
        --------------------------
        A room can host several guests simultaneously (e.g. two visiting
        relatives, each with their own ``GuestRoom`` row and their own
        expiry time).  ``expire()`` therefore only flips ``Room.status`` to
        VACANT when **all** of the following are true after this row is
        marked EXPIRED:

        - No other ``GuestRoom`` rows for the same room remain ACTIVE.
        - No ``RoomMember`` rows for the same room remain ACTIVE.

        This prevents one guest's expiry from evicting a co-guest or a
        permanent member who is sharing the same room.

        Called by ``RoomService.expire_guest_allocations`` (APScheduler job)
        or manually from the admin Dashboard.  The caller is responsible for
        committing the session.
        """
        self.status = RoomStatus.EXPIRED
        self.guest_id = None
        self.vacated_at = datetime.now(tz=KAMPALA_TZ)

        # Only vacate the room if no other active occupants remain
        other_active_guest = GuestRoom.query.filter(
            GuestRoom.room_id == self.room_id,
            GuestRoom.status == RoomStatus.ACTIVE,
            GuestRoom.id != self.id,
        ).first()

        if not other_active_guest:
            active_member = RoomMember.query.filter_by(
                room_id=self.room_id,
                status=RoomStatus.ACTIVE,
            ).first()

            if not active_member:
                room = database.session.get(Room, self.room_id)
                if room:
                    room.status = RoomStatus.VACANT

    def __repr__(self) -> str:
        return (
            f"<GuestRoom id={self.id} room_id={self.room_id} "
            f"guest_id={self.guest_id} expires_at={self.expires_at}>"
        )


# ---------------------------------------------------------------------------
# SQLAlchemy event — auto-assign shared rooms to new homeowners
# ---------------------------------------------------------------------------

@event.listens_for(database.session, "after_flush_postexec")
def assign_default_shared_rooms(session, flush_context) -> None:
    """
    After a new ``User`` is fully flushed (and their PK is available),
    allocate all existing SHARED rooms to them automatically so homeowners
    always have default access without manual steps.

    Uses ``after_flush_postexec`` (not ``after_flush``) to guarantee that
    ``User.id`` has been populated from the database before the FK reference
    is written into ``RoomMember.user_id``.
    """
    from .users import User  

    for obj in session.new:
        if not isinstance(obj, User):
            continue

        shared_rooms = (
            session.query(Room).filter_by(room_type=RoomType.SHARED).all()
        )
        for room in shared_rooms:
            allocation = RoomMember(
                room_id=room.id,
                user_id=obj.id,
                room_type=RoomType.SHARED,
                status=RoomStatus.ACTIVE,
                can_view=True,
                can_control=True,
                can_manage=False,
            )
            session.add(allocation)


# ---------------------------------------------------------------------------
# RoomService  (allocation lifecycle — use this in routes, never raw ORM)
# ---------------------------------------------------------------------------

class RoomService:
    """
    Stateless helpers that manage the full room allocation lifecycle:
    creation, re-allocation from the vacant pool, member release, and
    guest expiry.

    Transaction ownership
    ---------------------
    **All methods leave the session dirty but uncommitted.**  The caller
    (route handler, admin view, CLI command) is responsible for calling
    ``db.session.commit()`` — or ``db.session.rollback()`` on error.
    This keeps unit-of-work boundaries clean and prevents partial commits.

    Usage
    -----
    All route handlers and CLI commands should go through this service rather
    than manipulating ``RoomMember`` / ``GuestRoom`` directly.
    """

    # ------------------------------------------------------------------
    # Member allocations
    # ------------------------------------------------------------------

    @staticmethod
    def allocate_to_member(
        room_id: int,
        user_id: int,
        room_type: RoomType,
        can_view: bool = True,
        can_control: bool = True,
        can_manage: bool = False,
    ) -> RoomMember:
        """
        Allocate a room to a household member.

        Multiple members sharing a room
        ---------------------------------
        A room can be occupied by more than one member simultaneously (e.g.
        two children in a bunk-bed room, or a couple in a master bedroom).
        Each occupant gets their own ``RoomMember`` row with independent
        permissions.  The ``UniqueConstraint`` on ``(room_id, user_id)``
        prevents the *same* user from being allocated the same room twice, but
        places no limit on *how many different* users can share a room.

        Vacant-slot reuse logic
        -----------------------
        If a VACANT or INACTIVE row exists for this *specific user* in this
        room, it is reused (preserving device mappings).  Otherwise a fresh
        row is created.  We deliberately do **not** reuse a slot that belongs
        to a different user — that would overwrite their allocation.

        The ``Room.status`` is set to ACTIVE whenever at least one member is
        allocated; it is only set back to VACANT when the last active member
        is released via ``release_member_room``.

        Parameters
        ----------
        room_id:     PK of the Room to allocate.
        user_id:     PK of the User to assign.
        room_type:   Allocation category (PERSONAL / SHARED).
        can_view:    Grant view permission (default True).
        can_control: Grant control permission (default True).
        can_manage:  Grant manage permission (default False; admin-granted only).

        Returns
        -------
        RoomMember — the upserted (or newly created) allocation row.

        Raises
        ------
        ValueError — if the user already has an ACTIVE allocation for this room.
        """
        # Guard: same user cannot be in the same room twice
        already_active = RoomMember.query.filter_by(
            room_id=room_id,
            user_id=user_id,
            status=RoomStatus.ACTIVE,
        ).first()
        if already_active:
            raise ValueError(
                f"User {user_id} already has an active allocation for room {room_id}."
            )

        # Reuse this specific user's own INACTIVE slot if one exists
        # (e.g. they were removed and are being re-added to the same room).
        existing = RoomMember.query.filter_by(
            room_id=room_id,
            user_id=user_id,
        ).filter(
            RoomMember.status.in_([RoomStatus.VACANT, RoomStatus.INACTIVE])
        ).first()

        if existing:
            existing.room_type = room_type
            existing.status = RoomStatus.ACTIVE
            existing.can_view = can_view
            existing.can_control = can_control
            existing.can_manage = can_manage
            existing.vacated_at = None
            allocation = existing
        else:
            allocation = RoomMember(
                room_id=room_id,
                user_id=user_id,
                room_type=room_type,
                status=RoomStatus.ACTIVE,
                can_view=can_view,
                can_control=can_control,
                can_manage=can_manage,
            )
            database.session.add(allocation)

        # Mark the room itself as ACTIVE now that it has at least one occupant
        room = database.session.get(Room, room_id)
        if room and room.status != RoomStatus.ACTIVE:
            room.status = RoomStatus.ACTIVE

        return allocation

    @staticmethod
    def release_member_room(member: RoomMember) -> None:
        """
        Release a member's room allocation back to the vacant pool.

        The ``RoomMember`` row is kept (INACTIVE) so device mappings are
        preserved and the slot can be reused for that same user later.
        ``vacated_at`` is stamped with the current Kampala time.

        Shared-room awareness
        ---------------------
        If other members are still actively allocated to the same room, the
        ``Room.status`` remains ACTIVE.  Only when *this is the last active
        occupant* does the room flip to VACANT.

        Called when a user account is deleted or an admin explicitly releases
        a room via the Dashboard.  **Caller must commit.**
        """
        member.status = RoomStatus.INACTIVE
        member.user_id = None
        member.vacated_at = datetime.now(tz=KAMPALA_TZ)

        # Check whether any other members are still actively using this room
        other_active = RoomMember.query.filter(
            RoomMember.room_id == member.room_id,
            RoomMember.status == RoomStatus.ACTIVE,
            RoomMember.id != member.id,
        ).first()

        if not other_active:
            # Also check for active guest allocations on this room before
            # declaring it fully vacant
            active_guest = GuestRoom.query.filter_by(
                room_id=member.room_id,
                status=RoomStatus.ACTIVE,
            ).first()

            if not active_guest:
                room = database.session.get(Room, member.room_id)
                if room:
                    room.status = RoomStatus.VACANT

    # ------------------------------------------------------------------
    # Guest allocations
    # ------------------------------------------------------------------

    @staticmethod
    def allocate_to_guest(
        room_id: int,
        guest_id: int,
        invited_by_username: str,
        expires_at: datetime,
        can_view: bool = True,
        can_control: bool = True,
        valid_from=None,
        valid_until=None,
        valid_days: list[str] | None = None,
    ) -> GuestRoom:
        """
        Allocate a room to a guest user.

        **Admin use only** — enforce this with a role check in the calling
        route *before* invoking this method.

        Multiple concurrent guests
        --------------------------
        Several guests can share the same room simultaneously.  Each gets
        their own ``GuestRoom`` row with independent expiry times, daily
        windows, and permissions — so one guest's expiry does not affect
        another's access.  There is no cap on the number of concurrent
        ``GuestRoom`` rows for a single room.

        Parameters
        ----------
        room_id:              PK of the Room to allocate.
        guest_id:             PK of the guest User.
        invited_by_username:  Username of the admin creating the allocation.
        expires_at:           Hard expiry datetime (must be timezone-aware).
        can_view:             Grant view permission (default True).
        can_control:          Grant control permission (default True).
        valid_from:           ``datetime.time`` — daily window start, or None.
        valid_until:          ``datetime.time`` — daily window end, or None.
        valid_days:           List of short day names, or None for all days.

        Returns
        -------
        GuestRoom — the newly created allocation row.

        Raises
        ------
        ValueError — if ``expires_at`` is naive (no tzinfo).
        """
        if expires_at.tzinfo is None:
            raise ValueError(
                "expires_at must be timezone-aware. "
                "Use datetime.now(tz=KAMPALA_TZ) or similar."
            )

        allocation = GuestRoom(
            room_id=room_id,
            guest_id=guest_id,
            invited_by_id=invited_by_username,
            expires_at=expires_at,
            status=RoomStatus.ACTIVE,
            can_view=can_view,
            can_control=can_control,
            can_manage=False,  # guests never receive manage permission
            valid_from=valid_from,
            valid_until=valid_until,
            valid_days=valid_days,
        )
        database.session.add(allocation)

        # Mark the room itself ACTIVE now that it has at least one guest
        room = database.session.get(Room, room_id)
        if room and room.status != RoomStatus.ACTIVE:
            room.status = RoomStatus.ACTIVE

        return allocation

    @staticmethod
    def expire_guest_allocations() -> int:
        """
        Expire all ``GuestRoom`` allocations whose ``expires_at`` has passed.

        Called by the APScheduler job every 5 minutes.

        Returns
        -------
        int — count of allocations expired in this run.

        Note
        ----
        This method commits the session itself because it is called from a
        background scheduler context where no Flask request (and therefore no
        outer transaction) exists.  All other service methods leave the commit
        to the caller.
        """
        now = datetime.now(tz=KAMPALA_TZ)
        expired_list = GuestRoom.query.filter(
            GuestRoom.expires_at <= now,
            GuestRoom.status == RoomStatus.ACTIVE,
        ).all()

        for allocation in expired_list:
            allocation.expire()

        if expired_list:
            database.session.commit()

        return len(expired_list)

    # ------------------------------------------------------------------
    # Vacant pool queries
    # ------------------------------------------------------------------

    @staticmethod
    def get_vacant_room_slots() -> list[RoomMember]:
        """
        Return all ``RoomMember`` rows currently in the vacant pool
        (status INACTIVE or VACANT, no active owner).

        These are the slots available for re-allocation via
        ``allocate_to_member``.  Used by the admin panel and
        ``AllocateMemberForm`` to populate the slot picker.

        Returns
        -------
        list[RoomMember] — ordered by room name.
        """
        return (
            RoomMember.query
            .filter(RoomMember.status.in_([RoomStatus.VACANT, RoomStatus.INACTIVE]))
            .join(RoomMember.room)
            .order_by(Room.name)
            .all()
        )

    @staticmethod
    def get_vacant_rooms() -> list[Room]:
        """
        Return ``Room`` objects whose status is VACANT.

        Used by ``AllocateGuestForm`` to show which rooms can receive a new
        guest allocation.

        Returns
        -------
        list[Room] — ordered by name.
        """
        return (
            Room.query
            .filter_by(status=RoomStatus.VACANT)
            .order_by(Room.name)
            .all()
        )