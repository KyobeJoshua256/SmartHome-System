import enum
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime,
    ForeignKey, JSON, Time, Enum as SqlaEnum,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship, validates
from sqlalchemy import event

from HomeServer import database
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
    Full lifecycle state of a room or allocation.

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

    Devices are mapped directly to Room so they survive member/guest
    allocation changes without requiring re-mapping.  A room begins life as
    VACANT and becomes ACTIVE once allocated to a member or guest.
    """
    __tablename__ = "rooms"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
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

    def __repr__(self) -> str:
        return f"<Room id={self.id} name={self.name!r} type={self.room_type.value} status={self.status.value}>"


# ---------------------------------------------------------------------------
# RoomMember  (homeowner / household user allocation)
# ---------------------------------------------------------------------------

class RoomMember(TimestampMixin, database.Model):
    """
    Allocation of a room to a home member (homeowner or household user).

    Homeowners receive default access to their personal rooms and all shared
    rooms (kitchen, dining room, garage, etc.) automatically on account
    creation.

    When a member is deleted the allocation is NOT removed — it transitions
    to INACTIVE, preserving device mappings and room data.  The room then
    enters the vacant pool so it can be re-allocated to a new member without
    re-mapping devices.

    On re-allocation the existing VACANT/INACTIVE row is reused rather than
    creating a new one (see RoomService.allocate_to_member).
    """
    __tablename__ = "room_members"

    id = Column(Integer, primary_key=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # nullable for vacant rows

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
    can_view = Column(Boolean, default=True, nullable=False)
    can_control = Column(Boolean, default=True, nullable=False)
    can_manage = Column(Boolean, default=False, nullable=False)  # Admin-only: change room settings

    # Populated when the room is released back to the vacant pool
    vacated_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    room = relationship("Room", back_populates="members", foreign_keys=[room_id])
    user = relationship("User", back_populates="room_memberships", foreign_keys=[user_id])

    __table_args__ = (
        # Prevent duplicate active allocations of the same room to the same user
        UniqueConstraint("room_id", "user_id", name="uq_room_member"),
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

    Access is time-bounded via expires_at and optionally restricted to
    specific days of the week (valid_days) and a daily time window
    (valid_from / valid_until).

    When access expires the scheduler calls expire(), which sets status to
    EXPIRED, drops the guest reference, and the room re-enters the vacant
    pool for a new allocation.
    """
    __tablename__ = "guest_rooms"

    id = Column(Integer, primary_key=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False)
    guest_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # nullable after expiry
    invited_by_id = Column(String, ForeignKey("users.username"), nullable=False)

    status = Column(
        SqlaEnum(RoomStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=RoomStatus.ACTIVE,
    )

    # Granular permissions (guests typically view/control only)
    can_view = Column(Boolean, default=True, nullable=False)
    can_control = Column(Boolean, default=True, nullable=False)
    can_manage = Column(Boolean, default=False, nullable=False)  # Admin-only

    # Time-bounded access
    expires_at = Column(DateTime(timezone=True), nullable=False)  # Hard expiry (date + time)
    valid_from = Column(Time, nullable=True)                       # Daily window start
    valid_until = Column(Time, nullable=True)                      # Daily window end
    valid_days = Column(JSON, nullable=True)                       # e.g. ["mon", "wed", "fri"]

    # Populated when access expires and room is released
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

    @validates("valid_days")
    def validate_valid_days(self, key, value):
        """Ensure valid_days contains only recognised short day names."""
        if value is None:
            return value
        invalid = {d.lower() for d in value} - VALID_DAY_NAMES
        if invalid:
            raise ValueError(f"Unrecognised day names in valid_days: {invalid}")
        return [d.lower() for d in value]

    def is_currently_accessible(self) -> bool:
        """
        Returns True if this guest allocation is valid right now.

        Checks: expiry not reached, today is an allowed weekday, and
        current time falls within the daily window if one is configured.
        """
        now = datetime.now(tz=KAMPALA_TZ)

        if now >= self.expires_at:
            return False

        if self.valid_days:
            today = now.strftime("%a").lower()
            if today not in self.valid_days:
                return False

        if self.valid_from and self.valid_until:
            if not (self.valid_from <= now.time() <= self.valid_until):
                return False

        return True

    def expire(self) -> None:
        """
        Mark this allocation as EXPIRED and release the room to the vacant
        pool.  Called by the APScheduler guest-expiry job.
        """
        self.status = RoomStatus.EXPIRED
        self.guest_id = None
        self.vacated_at = datetime.now(tz=KAMPALA_TZ)

    def __repr__(self) -> str:
        return (
            f"<GuestRoom id={self.id} room_id={self.room_id} "
            f"guest_id={self.guest_id} expires_at={self.expires_at}>"
        )


# ---------------------------------------------------------------------------
# SQLAlchemy event — auto-assign shared rooms to new homeowners
# ---------------------------------------------------------------------------

@event.listens_for(database.session, "after_flush")
def assign_default_shared_rooms(session, flush_context):
    """
    After a new User is flushed, allocate all existing SHARED rooms to them
    automatically so homeowners always have default access without manual steps.
    """
    from .users import User  # local import to avoid circular dependency

    for obj in session.new:
        if not isinstance(obj, User):
            continue

        shared_rooms = session.query(Room).filter_by(room_type=RoomType.SHARED).all()
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
    Handles the full room allocation lifecycle: creation, re-allocation from
    the vacant pool, member release, and guest expiry.

    All route handlers and CLI commands should go through this service rather
    than manipulating RoomMember / GuestRoom directly.
    """

    # -- Member allocations --------------------------------------------------

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
        Allocate a room to a member.

        Reuses an existing INACTIVE or VACANT row for this room before
        creating a new record — per the design note: 'start with allocation,
        if none then new is done'.
        """
        existing = RoomMember.query.filter(
            RoomMember.room_id == room_id,
            RoomMember.status.in_([RoomStatus.VACANT, RoomStatus.INACTIVE]),
        ).first()

        if existing:
            existing.user_id = user_id
            existing.room_type = room_type
            existing.status = RoomStatus.ACTIVE
            existing.can_view = can_view
            existing.can_control = can_control
            existing.can_manage = can_manage
            existing.vacated_at = None
            return existing

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
        return allocation

    @staticmethod
    def release_member_room(member: RoomMember) -> None:
        """
        Release a member's room allocation back to the vacant pool.

        The row is kept (INACTIVE) so device mappings are preserved.
        Called when a user account is deleted or a room is reassigned.
        """
        member.status = RoomStatus.INACTIVE
        member.user_id = None
        member.vacated_at = datetime.now(tz=KAMPALA_TZ)

    # -- Guest allocations ---------------------------------------------------

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
        valid_days=None,
    ) -> "GuestRoom":
        """
        Allocate a room to a guest.  Admin use only — enforce this in the
        calling route with a role check before invoking this method.
        """
        allocation = GuestRoom(
            room_id=room_id,
            guest_id=guest_id,
            invited_by_id=invited_by_username,
            expires_at=expires_at,
            status=RoomStatus.ACTIVE,
            can_view=can_view,
            can_control=can_control,
            can_manage=False,
            valid_from=valid_from,
            valid_until=valid_until,
            valid_days=valid_days,
        )
        database.session.add(allocation)
        return allocation

    @staticmethod
    def expire_guest_allocations() -> int:
        """
        Expire all GuestRoom allocations whose expires_at has passed.

        Called by the APScheduler job every 5 minutes.  Returns the count
        of allocations expired in this run.
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

    # -- Vacant pool query ---------------------------------------------------

    @staticmethod
    def get_vacant_rooms() -> list:
        """
        Return all rooms currently in the vacant pool (INACTIVE or VACANT
        member allocations with no active owner).  Used by the admin panel
        to show rooms available for re-allocation.
        """
        return RoomMember.query.filter(
            RoomMember.status.in_([RoomStatus.VACANT, RoomStatus.INACTIVE]),
        ).all()