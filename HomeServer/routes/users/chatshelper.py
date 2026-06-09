# ============================================================================
# MESSAGING & CONVERSATION HELPERS
# ElectroNora Smart Home
# Purpose: Pure business logic for conversations and messages.
# No Flask route handlers live here.
# ============================================================================

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, func, or_
from flask_login import current_user

from HomeServer.models import (
    User, Conversation, ConversationParticipant, Message,
    MessageType, ConversationType,                      
)
from HomeServer.models.utils import now_utc, to_uganda_time           
from HomeServer import database as db



# ============================================================================
# SECTION 1: Get User's Conversations
# ============================================================================

def get_conversations_data(user: User) -> list:
    """Return conversations the *user* participates in, most recent first.

    Returns a list of dicts, each containing:
        - conversation:  The Conversation object
        - last_read:     When the user last read the conversation
        - unread_count:  Number of unread messages
        - is_muted:      Whether the user has muted this conversation
        - last_message:  The most recent Message (or None)
    """
    rows = (
        db.session.query(
            Conversation,
            ConversationParticipant.last_read_at,
            ConversationParticipant.unread_count,
            ConversationParticipant.is_muted,          
        )
        .join(ConversationParticipant, Conversation.id == ConversationParticipant.conversation_id)
        .filter(
            ConversationParticipant.user_id    == user.id,
            ConversationParticipant.is_deleted == False,
            Conversation.is_deleted            == False,
        )
        .order_by(Conversation.last_message_at.desc())
        .all()
    )

    result = []
    for conv, last_read, unread_count, is_muted in rows:
        last_msg = (
            db.session.query(Message)
            .filter(Message.conversation_id == conv.id, Message.is_deleted == False)
            .order_by(Message.created_at.desc())
            .first()
        )
        result.append({
            'conversation': conv,
            'last_read':    to_uganda_time(last_read) if last_read else None,
            'unread_count': unread_count or 0,
            'is_muted':     is_muted,                   
            'last_message': last_msg,
            
        })
    return result


# ============================================================================
# SECTION 2: Build Conversations JSON for Frontend
# ============================================================================

def build_conversations_json(user: User, conversations_data: List[dict]) -> List[dict]:
    """Convert conversations data into JSON-friendly format for the chat module.

    Args:
        user:               The current user
        conversations_data: Output from get_conversations_data()

    Returns:
        List of conversation dicts ready for JSON serialization.
    """
    conversations_json = []

    for c in conversations_data:
        conv     = c['conversation']
        last_msg = c['last_message']

        participants       = [p for p in conv.participants if not p.is_deleted]
        other_participants = [p for p in participants if p.user_id != user.id]

        # FIX: removed room_id branch — no such column.
        # Determine display name from conversation type and participants.
        if conv.conversation_type.value in ('group', 'broadcast'):
            # Named conversations always have an explicit name
            display_name   = conv.name or 'Unnamed Group'
            avatar_initial = (display_name[0] or 'G').upper()
            is_self        = False
        elif not other_participants:
            # Self-chat: user is the only participant
            display_name   = user.username
            avatar_initial = (display_name[0] or '?').upper()
            is_self        = True
        else:
            # Direct message with another user
            other_user     = User.query.get(other_participants[0].user_id)
            display_name   = other_user.username if other_user else 'Unknown'
            avatar_initial = (display_name[0] or '?').upper()
            is_self        = False

        last_message_text = ''
        last_message_time = ''
        if last_msg:
            last_message_text = last_msg.content[:60]
            ts = to_uganda_time(last_msg.created_at)
            last_message_time = ts.strftime('%H:%M') if ts else ''

        conversations_json.append({
            'conversation_id':   conv.id,
            'conversation_type': conv.conversation_type.value,
            'display_name':      display_name,
            'avatar_initial':    avatar_initial,
            'unread_count':      c['unread_count'],
            'is_muted':          c['is_muted'],         
            'last_message':      last_message_text,
            'last_message_time': last_message_time,
            'is_self':           is_self,
        })

    return conversations_json


# ============================================================================
# SECTION 3: Message Serialization
# ============================================================================

def serialize_message(msg: Message, current_user_id: int) -> dict:
    """Convert a Message object to a JSON-serializable dictionary.

    Args:
        msg:             The Message object
        current_user_id: ID of the requesting user (for read status)

    Returns:
        Dictionary with message data.
    """
    sender     = msg.sender
    created_at = msg.created_at

    if created_at and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    return {
        'id':            msg.id,
        'sender_id':     msg.sender_id,
        'sender_name':   sender.username if sender else 'Unknown',
        'content':       msg.content,
        'created_at':    created_at.isoformat() if created_at else '',
        'message_type':  msg.message_type.value if msg.message_type else 'text',
        'is_edited':     msg.is_edited,
        'reply_to_id':   msg.reply_to_id,
        'read_count':    msg.read_count or 0,
        'is_read_by_me': msg.is_read_by(current_user_id),
        'read_status':   msg.get_read_status(current_user_id),
    }


# ============================================================================
# SECTION 4: Get Conversation Messages
# ============================================================================

def get_conversation_messages(conversation_id: int, user_id: int) -> List[dict]:
    """Get all messages in a conversation and mark it as read.

    Args:
        conversation_id: ID of the conversation
        user_id:         ID of the requesting user

    Returns:
        List of serialized messages (oldest first), or empty list if no access.
    """
    participant = ConversationParticipant.query.filter_by(
        conversation_id=conversation_id,
        user_id=user_id,
        is_deleted=False,
    ).first()

    if not participant:
        return []

    messages = (
        Message.query
        .filter_by(conversation_id=conversation_id, is_deleted=False)
        .order_by(Message.created_at.asc())
        .all()
    )

    
    if participant.unread_count and participant.unread_count > 0:
        participant.mark_as_read()
        db.session.commit()

    return [serialize_message(msg, user_id) for msg in messages]


# ============================================================================
# SECTION 5: Create a New Message
# ============================================================================

def create_new_message(
    conversation_id: int,
    sender_id: int,
    content: str,
    message_type: str = 'text',
    reply_to_id: Optional[int] = None,
    ) -> Optional[Message]:
    """Create and persist a new message.

    Args:
        conversation_id: ID of the conversation
        sender_id:       ID of the user sending the message
        content:         Message content
        message_type:    Type string matching MessageType enum values
        reply_to_id:     Optional ID of the message being replied to

    Returns:
        The created Message object, or None on error.
    """
    try:
        participant = ConversationParticipant.query.filter_by(
            conversation_id=conversation_id,
            user_id=sender_id,
            is_deleted=False,
        ).first()

        if not participant:
            return None

        try:
            msg_type_enum = MessageType(message_type)
        except ValueError:
            msg_type_enum = MessageType.TEXT

        msg = Message(
            conversation_id=conversation_id,
            sender_id=sender_id,
            content=content,
            message_type=msg_type_enum,
            reply_to_id=reply_to_id,                   
            is_edited=False,
            is_deleted=False,
        )
        db.session.add(msg)

        other_participants = ConversationParticipant.query.filter(
            ConversationParticipant.conversation_id == conversation_id,
            ConversationParticipant.user_id         != sender_id,
            ConversationParticipant.is_deleted      == False,
        ).all()

        for p in other_participants:
            p.increment_unread()

        participant.mark_as_read()

        db.session.commit()
        return msg

    except Exception as error:
        db.session.rollback()
        from flask import current_app
        current_app.logger.error(f"Failed to create message: {error}")
        return None


# ============================================================================
# SECTION 6: Mark Message as Read
# ============================================================================

def mark_message_read(message_id: int, user_id: int) -> tuple[bool, Optional[str]]:
    """Mark a specific message as read by the user.

    Args:
        message_id: ID of the message
        user_id:    ID of the user marking it read

    Returns:
        Tuple of (was_newly_marked, error_message).
    """
    try:
        message = Message.query.get(message_id)
        if not message:
            return False, "Message not found"

        participant = ConversationParticipant.query.filter_by(
            conversation_id=message.conversation_id,
            user_id=user_id,
            is_deleted=False,
        ).first()

        if not participant:
            return False, "Access denied"

        all_participants = ConversationParticipant.query.filter_by(
            conversation_id=message.conversation_id,
            is_deleted=False,
        ).all()
        is_self_chat = (
            len(all_participants) == 1 and all_participants[0].user_id == user_id
        )

        if message.sender_id == user_id and not is_self_chat:
            return False, "Cannot mark own messages as read"

        was_newly_marked = message.mark_read(user_id)

        if was_newly_marked:
            db.session.commit()

        return was_newly_marked, None

    except Exception as error:
        db.session.rollback()
        return False, str(error)


# ============================================================================
# SECTION 7: Mark Entire Conversation as Read
# ============================================================================

def mark_conversation_read(conversation_id: int, user_id: int) -> tuple[int, Optional[str]]:
    """Mark all messages in a conversation as read for the user.

    Args:
        conversation_id: ID of the conversation
        user_id:         ID of the user

    Returns:
        Tuple of (count_of_newly_marked_messages, error_message).
    """
    try:
        participant = ConversationParticipant.query.filter_by(
            conversation_id=conversation_id,
            user_id=user_id,
            is_deleted=False,
        ).first()

        if not participant:
            return 0, "Conversation not found or access denied"

        all_participants = ConversationParticipant.query.filter_by(
            conversation_id=conversation_id,
            is_deleted=False,
        ).all()
        is_self_chat = (
            len(all_participants) == 1 and all_participants[0].user_id == user_id
        )

        msg_query = Message.query.filter(
            Message.conversation_id == conversation_id,
            Message.is_deleted      == False,
        )
        if not is_self_chat:
            msg_query = msg_query.filter(Message.sender_id != user_id)
        messages = msg_query.all()

        newly_read_count = 0
        for message in messages:
            if message.mark_read(user_id):
                newly_read_count += 1

        if newly_read_count > 0:
            participant.mark_as_read()
            db.session.commit()

        return newly_read_count, None

    except Exception as error:
        db.session.rollback()
        return 0, str(error)


# ============================================================================
# SECTION 8: Get Message Read Status
# ============================================================================

def get_message_read_status(message_id: int, user_id: int) -> Optional[dict]:
    """Get read status information for a specific message.

    Args:
        message_id: ID of the message
        user_id:    ID of the requesting user

    Returns:
        Dictionary with read status data, or None if not found / access denied.
    """
    try:
        message = Message.query.get(message_id)
        if not message:
            return None

        participant = ConversationParticipant.query.filter_by(
            conversation_id=message.conversation_id,
            user_id=user_id,
            is_deleted=False,
        ).first()

        if not participant:
            return None

        participants = ConversationParticipant.query.filter(
            ConversationParticipant.conversation_id == message.conversation_id,
            ConversationParticipant.user_id         != message.sender_id,
            ConversationParticipant.is_deleted      == False,
        ).all()

        read_by_list = []
        for p in participants:
            if message.is_read_by(p.user_id):
                read_by_list.append({
                    'user_id':  p.user_id,
                    'username': p.user.username,
                    'read_at':  message.get_read_at(p.user_id),  
                })

        return {
            'message_id':         message_id,
            'total_participants': len(participants),
            'read_count':         message.read_count,
            'read_by':            read_by_list,
            'is_fully_read':      message.read_count >= len(participants),
            'read_status':        message.get_read_status(user_id),
        }

    except Exception as error:
        from flask import current_app
        current_app.logger.error(f"Error getting read status: {error}")
        return None


# ============================================================================
# SECTION 9: Find or Create Direct Conversation
# ============================================================================

def find_or_create_direct_conversation(user_id: int, other_user_id: int) -> Optional[Conversation]:
    """Find an existing direct conversation or create a new one.

    Args:
        user_id:       ID of the first user
        other_user_id: ID of the second user (can be same for self-chat)

    Returns:
        The Conversation object, or None on error.
    """
    try:
        is_self = user_id == other_user_id
        existing = (
            db.session.query(Conversation)
            .join(
                ConversationParticipant,
                Conversation.id == ConversationParticipant.conversation_id,
            )
            .filter(
                ConversationParticipant.user_id    == user_id,
                ConversationParticipant.is_deleted == False,
                Conversation.conversation_type     == ConversationType.DIRECT,
                Conversation.is_deleted            == False,
            )
            .all()
        )

        conversation = None
        for conv in existing:
            participant_ids = {p.user_id for p in conv.participants if not p.is_deleted}
            if is_self:
                if participant_ids == {user_id}:
                    conversation = conv
                    break
            else:
                if participant_ids == {user_id, other_user_id}:
                    conversation = conv
                    break

        if not conversation:
            conversation = Conversation(
                conversation_type=ConversationType.DIRECT,
                name=None,
                is_deleted=False,
            )
            db.session.add(conversation)
            db.session.flush()

            db.session.add(ConversationParticipant(
                conversation_id=conversation.id,
                user_id=user_id,
                unread_count=0,
                is_deleted=False,
            ))

            if not is_self:
                db.session.add(ConversationParticipant(
                    conversation_id=conversation.id,
                    user_id=other_user_id,
                    unread_count=0,
                    is_deleted=False,
                ))

            db.session.commit()

        return conversation

    except Exception as error:
        db.session.rollback()
        from flask import current_app
        current_app.logger.error(f"Error finding/creating conversation: {error}")
        return None


# ============================================================================
# SECTION 10: Get Conversation Summary for UI
# ============================================================================

def get_conversation_summary(conversation: Conversation, current_user_id: int) -> dict:
    """Get a summary of a conversation for display in the chat list.

    Args:
        conversation:     The Conversation object
        current_user_id:  ID of the requesting user

    Returns:
        Dictionary with conversation summary data.
    """
    participants       = [p for p in conversation.participants if not p.is_deleted]
    other_participants = [p for p in participants if p.user_id != current_user_id]

    my_participant = next(
        (p for p in participants if p.user_id == current_user_id), None
    )

    if conversation.conversation_type.value in ('group', 'broadcast'):
        display_name   = conversation.name or 'Unnamed Group'
        avatar_initial = (display_name[0] or 'G').upper()
        is_self        = False
    elif not other_participants:
        current_user_obj = User.query.get(current_user_id)
        display_name     = current_user_obj.username if current_user_obj else 'Me'
        avatar_initial   = (display_name[0] or '?').upper()
        is_self          = True
    else:
        other_user     = User.query.get(other_participants[0].user_id)
        display_name   = other_user.username if other_user else 'Unknown'
        avatar_initial = (display_name[0] or '?').upper()
        is_self        = False

    last_msg = (
        Message.query
        .filter_by(conversation_id=conversation.id, is_deleted=False)
        .order_by(Message.created_at.desc())
        .first()
    )

    last_message_text = ''
    last_message_time = ''
    if last_msg:
        last_message_text = last_msg.content[:60]
        ts = to_uganda_time(last_msg.created_at)
        last_message_time = ts.strftime('%H:%M') if ts else ''

    return {
        'conversation_id':   conversation.id,
        'conversation_type': conversation.conversation_type.value,
        'display_name':      display_name,
        'avatar_initial':    avatar_initial,
        'unread_count':      my_participant.unread_count if my_participant else 0,
        'is_muted':          my_participant.is_muted if my_participant else False,
        'last_message':      last_message_text,
        'last_message_time': last_message_time,
        'is_self':           is_self,
    }