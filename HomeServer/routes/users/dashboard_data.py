# Final imports for dashboard_data.py
from HomeServer.models.utils import now_kampala, to_uganda_time
from typing import Dict, Any, List
from HomeServer.models.users import User, UserRole
from HomeServer.routes.users.chatshelper import get_conversations_data, build_conversations_json
from datetime import datetime

from flask_login import current_user
from sqlalchemy import or_

from HomeServer import database
from HomeServer.models.rooms import (
    GuestRoom,
    Room,
    RoomMember,
    RoomStatus,
    RoomType,
)
from HomeServer.models.utils import KAMPALA_TZ


# =============================================================================
# HELPERS
# =============================================================================

def _now() -> datetime:
    return datetime.now(tz=KAMPALA_TZ)


# =============================================================================
# PROFILE DATA
# =============================================================================

def get_user_profile_data(user: User) -> Dict[str, Any]:
    """Return user profile information safe for templates/API responses."""
    return {
        "username":             user.username,
        "role":                 user.role,
        "avatar_url":           user.avatar_url,
        "is_active":            user.is_active,
        "force_password_reset": user.force_password_reset,
        "receive_push":         user.receive_push,
        "mute_notifications":   user.mute_notifications,
    }


# =============================================================================
# NOTIFICATION DATA
# =============================================================================

def get_notification_data(user: User) -> Dict[str, Any]:
    """Return notification preference information."""
    return {
        "receive_push":       user.receive_push,
        "mute_notifications": user.mute_notifications,
    }


# =============================================================================
# CHAT DATA
# =============================================================================

def get_chat_summary(user: User) -> Dict[str, Any]:
    conversations_data = get_conversations_data(user)
    return {
        "conversations": build_conversations_json(user, conversations_data),
        "total_unread":  sum(c["unread_count"] for c in conversations_data),
        "active_convs":  len(conversations_data),
    }


# =============================================================================
# ROOM DATA
# =============================================================================

def _get_member_rooms(user_id: int) -> Dict[str, List[RoomMember]]:
    """
    Fetch personal and shared rooms for a member/admin in one query.

    Personal — RoomMember where user_id = this user, type = PERSONAL, status = ACTIVE
    Shared   — RoomMember where room_type = SHARED, status = ACTIVE (visible to all)

    Shared rooms are deduplicated by room_id — a room with multiple ACTIVE
    RoomMember rows only appears once.
    """
    rows = (
        database.session.query(RoomMember)
        .join(RoomMember.room)
        .filter(
            RoomMember.status == RoomStatus.ACTIVE,
            or_(
                RoomMember.user_id == user_id,
                RoomMember.room_type == RoomType.SHARED,
            ),
        )
        .order_by(Room.name)
        .all()
    )

    personal_rooms = []
    shared_rooms   = []
    shared_seen    = set()

    for rm in rows:
        if rm.room_type == RoomType.PERSONAL and rm.user_id == user_id:
            personal_rooms.append(rm)
        elif rm.room_type == RoomType.SHARED and rm.room_id not in shared_seen:
            shared_rooms.append(rm)
            shared_seen.add(rm.room_id)

    return {"personal": personal_rooms, "shared": shared_rooms}


def _get_admin_room_overview() -> Dict[str, int]:
    """Scalar room stats for the admin overview panel — 4 fast index queries."""
    now = _now()
    return {
        "total_rooms":    database.session.query(Room).count(),
        "active_members": (
            database.session.query(RoomMember)
            .filter(RoomMember.status == RoomStatus.ACTIVE)
            .count()
        ),
        "active_guests": (
            database.session.query(GuestRoom)
            .filter(
                GuestRoom.status == RoomStatus.ACTIVE,
                GuestRoom.expires_at > now,
            )
            .count()
        ),
        "vacant_slots": (
            database.session.query(RoomMember)
            .filter(RoomMember.status.in_([RoomStatus.VACANT, RoomStatus.INACTIVE]))
            .count()
        ),
    }


def get_room_data(user: User) -> Dict[str, Any]:
    """
    Build the complete room context for the dashboard.

    Returns
    -------
    {
        personal_rooms  : list[RoomMember]   — this user's personal rooms
        shared_rooms    : list[RoomMember]   — shared rooms (all members see these)
        admin_overview  : dict | None        — room stats for admins, else None
        total_rooms     : int                — personal + shared count (quick badge)
    }
    """
    role = user.role

    # Members and admins see personal + shared rooms
    member_rooms   = _get_member_rooms(user.id)
    personal_rooms = member_rooms["personal"]
    shared_rooms   = member_rooms["shared"]

    admin_overview = (
        _get_admin_room_overview() if role == UserRole.ADMIN.value else None
    )

    return {
        "personal_rooms": personal_rooms,
        "shared_rooms":   shared_rooms,
        "admin_overview": admin_overview,
        # Convenience count for nav badges / empty-state checks
        "total_rooms":    len(personal_rooms) + len(shared_rooms),
    }


# =============================================================================
# DASHBOARD CONTEXT — single entry point for the route
# =============================================================================

def get_user_dashboard_data(user: User) -> Dict[str, Any]:
    """
    Generate the complete dashboard context dict.

    Usage in your route
    -------------------
        from HomeServer.routes.main.dashboard_data import get_user_dashboard_data

        @main.route("/dashboard")
        @login_required
        def dashboard():
            ctx = get_user_dashboard_data(current_user)
            return render_template("main/dashboard.html", **ctx)

    Template variables
    ------------------
    user              User            — the logged-in user object
    UserRole          enum            — for role comparisons in the template
    profile           dict            — username, role, avatar_url, flags
    notifications     dict            — receive_push, mute_notifications
    is_admin          bool
    is_user           bool
    is_active         bool
    now               datetime        — current Uganda time
    chat              dict            — conversations, total_unread, active_convs

    # Room variables (from get_room_data)
    personal_rooms    list[RoomMember]
    shared_rooms      list[RoomMember]
    admin_overview    dict | None     — room stats, admins only
    total_rooms       int             — combined room count
    """
    rooms = get_room_data(user)

    return {
        # Core
        "user":     user,
        "UserRole": UserRole,

        # Convenience flags
        "is_admin":  user.role == UserRole.ADMIN.value,
        "is_user":   user.role == UserRole.USER.value,
        "is_active": user.is_active,

        # Sub-contexts
        "profile":       get_user_profile_data(user),
        "notifications": get_notification_data(user),
        "chat":          get_chat_summary(user),

        # Room data (unpacked so templates access them as top-level vars)
        "personal_rooms": rooms["personal_rooms"],
        "shared_rooms":   rooms["shared_rooms"],
        "admin_overview": rooms["admin_overview"],
        "total_rooms":    rooms["total_rooms"],

        # Time
        "now": now_kampala(),
    }