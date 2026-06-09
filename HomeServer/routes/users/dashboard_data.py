# Final imports for dashboard_data.py
from HomeServer.models.utils import now_utc, to_uganda_time
from typing import Dict, Any
from HomeServer.models import User, UserRole
from HomeServer.routes.users.chatshelper import get_conversations_data, build_conversations_json

# =============================================================================
# PROFILE DATA
# =============================================================================

def get_user_profile_data(user: User) -> Dict[str, Any]:
    """
    Return user profile information safe for templates/API responses.
    """

    return {
        "username": user.username,
        "role": user.role,
        "avatar_url": user.avatar_url,

        # Status
        "is_active": user.is_active,
        "force_password_reset": user.force_password_reset,

        # Preferences
        "receive_push": user.receive_push,
        "mute_notifications": user.mute_notifications,
    }

# =============================================================================
# NOTIFICATION DATA
# =============================================================================

def get_notification_data(user: User) -> Dict[str, Any]:
    """
    Return notification preference information.
    """

    return {
        "receive_push": user.receive_push,
        "mute_notifications": user.mute_notifications,
    }

# Message and chat data context
def get_chat_summary(user: User) -> Dict[str, Any]:
    conversations_data = get_conversations_data(user)
    return {
        "conversations":   build_conversations_json(user, conversations_data),
        "total_unread":    sum(c["unread_count"] for c in conversations_data),
        "active_convs":    len(conversations_data),
    }

# =============================================================================
# DASHBOARD CONTEXT
# =============================================================================

def get_user_dashboard_data(user: User) -> Dict[str, Any]:
    """
    Generate complete dashboard context.

    Includes:
    - User object
    - Profile information
    - Notification settings
    - Role information
    - Current Uganda time
    """


    return {
        "user": user,
        "UserRole": UserRole,
        "profile": get_user_profile_data(user),
        "notifications": get_notification_data(user),
        "is_admin": user.role == UserRole.ADMIN.value,
        "is_user": user.role == UserRole.USER.value,
        "is_active": user.is_active,
        "now": to_uganda_time(now_utc()),
        "chat": get_chat_summary(user),
    }
