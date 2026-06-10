# ============================================================================
# MESSAGING & CONVERSATION API ROUTES
# ElectroNora Smart Home
# ============================================================================

import time
from datetime import timezone
from typing import Any
from flask import (
Blueprint, request, jsonify, current_app,
)
from flask_login import login_required, current_user
from flask_wtf.csrf import generate_csrf
from sqlalchemy import or_, exc
from HomeServer.models import (
User, Conversation, ConversationParticipant, Message, MessageType,
ConversationType,
)
from HomeServer.models.utils import now_utc, to_uganda_time
from HomeServer.models.chats import _hmac_uid
from HomeServer import database as db

# ============================================================================
# BLUEPRINT DEFINITION
# ============================================================================

message_api_bp = Blueprint('message_api', __name__)

# ============================================================================
# SECTION 1: CSRF TOKEN REFRESH
# ============================================================================

@message_api_bp.route('/csrf-refresh', methods=['GET'])
@login_required
def csrf_refresh() -> Any:
    """Return a fresh CSRF token without requiring an existing valid token."""
    try:
        new_token = generate_csrf()
        return jsonify({
            'csrf_token': new_token,
            'timestamp':  int(time.time()),
        }), 200
    except Exception as error:
        current_app.logger.error(f"CSRF refresh error: {error}")
        return jsonify({'error': 'Failed to generate CSRF token'}), 500

# ============================================================================
# SECTION 2: USER SEARCH (for starting conversations)
# ============================================================================

@message_api_bp.route('/users/search', methods=['GET'])
@login_required
def search_users() -> Any:
    """Full-text search across username, phone, and e-mail."""
    try:
        q = request.args.get('q', '').strip()
        if not q:
            return jsonify({'users': []}), 200
        
        like = f'%{q}%'
        users = (
            User.query
            .filter(
                User.is_active   == True,
                User.is_deleted  == False,
                or_(
                    User.username.ilike(like),
                    User.phone.ilike(like),
                    User.email.ilike(like),
                ),
            )
            .order_by(User.username.asc())
            .limit(20)
            .all()
        )

        return jsonify({
            'users': [
                {
                    'id':         u.id,
                    'username':   u.username,
                    'name':       u.username,
                    'phone':      u.phone or '',
                    'role':       u.role if u.role else None,
                    'avatar_url': u.avatar_url or None,
                }
                for u in users
            ]
        }), 200

    except exc.SQLAlchemyError as db_error:
        current_app.logger.error(f"DB error in search_users: {db_error}", exc_info=True)
        db.session.rollback()
        return jsonify({'error': 'Database error'}), 500

    except Exception as error:
        current_app.logger.error(f"Error in search_users: {error}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500

# ============================================================================
# SECTION 3: FIND OR CREATE CONVERSATION
# ============================================================================

from .chatshelper import find_or_create_direct_conversation, get_conversation_summary

@message_api_bp.route('/conversations/find-or-create', methods=['POST'])
@login_required
def find_or_create_conversation() -> Any:
    data         = request.get_json(silent=True) or {}
    recipient_id = data.get('recipient_id')
    
    if not recipient_id:
        return jsonify({'error': 'recipient_id is required'}), 400
    
    recipient = User.query.filter_by(id=recipient_id, is_active=True, is_deleted=False).first()
    if not recipient:
        return jsonify({'error': 'User not found'}), 404

    conversation = find_or_create_direct_conversation(current_user.id, recipient.id)
    if not conversation:
        return jsonify({'error': 'Could not create conversation'}), 500

    summary = get_conversation_summary(conversation, current_user.id)
    return jsonify({'conversation': summary}), 200

# ============================================================================
# SECTION 4: GET MESSAGES (with read receipts)
# ============================================================================

@message_api_bp.route('/conversations/<int:conversation_id>/messages', methods=['GET'])
@login_required
def get_messages(conversation_id: int) -> Any:
    """Return all messages in a conversation (oldest first) and mark it as read."""
    try:
        participant = ConversationParticipant.query.filter_by(
            conversation_id=conversation_id,
            user_id=current_user.id,
            is_deleted=False,
        ).first()
        
        if not participant:
            return jsonify({'error': 'Conversation not found or access denied'}), 404
        
        messages = (
            Message.query
            .filter_by(conversation_id=conversation_id, is_deleted=False)
            .order_by(Message.created_at.asc())
            .all()
        )

        # Mark conversation as read when user views it
        if participant.unread_count and participant.unread_count > 0:
            participant.mark_as_read()          
            db.session.commit()

        def _fmt(msg: Message) -> dict:
            """Serialize a Message row to the JSON shape expected by messaging.js."""
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
                'is_read_by_me': msg.is_read_by(current_user.id),
                'read_status':   msg.get_read_status(current_user.id),
            }

        return jsonify({'messages': [_fmt(m) for m in messages]}), 200

    except exc.SQLAlchemyError as db_error:
        current_app.logger.error(f"DB error in get_messages: {db_error}", exc_info=True)
        db.session.rollback()
        return jsonify({'error': 'Database error'}), 500

    except Exception as error:
        current_app.logger.error(f"Error in get_messages: {error}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500

# ============================================================================
# SECTION 5: SEND MESSAGE
# ============================================================================

@message_api_bp.route('/conversations/<int:conversation_id>/messages', methods=['POST'])
@login_required
def send_message(conversation_id: int) -> Any:
    """Persist a new message and emit a Socket.IO event to all participants."""
    try:
        participant = ConversationParticipant.query.filter_by(
            conversation_id=conversation_id,
            user_id=current_user.id,
            is_deleted=False,
        ).first()
        
        if not participant:
            return jsonify({'error': 'Conversation not found or access denied'}), 404
        
        body    = request.get_json(silent=True) or {}
        content = (body.get('content') or '').strip()

        if not content:
            return jsonify({'error': 'Message content cannot be empty'}), 400
        if len(content) > 10_000:
            return jsonify({'error': 'Message too long (max 10000 characters)'}), 400

        message_type_str = body.get('message_type', MessageType.TEXT.value)
        try:
            message_type_enum = MessageType(message_type_str)
        except ValueError:
            message_type_enum = MessageType.TEXT

        msg = Message(
            conversation_id=conversation_id,
            sender_id=current_user.id,
            content=content,
            message_type=message_type_enum,
            reply_to_id=body.get('reply_to_id'),    # threading support
            is_edited=False,
            is_deleted=False,
        )
        db.session.add(msg)

        other_participants = ConversationParticipant.query.filter(
            ConversationParticipant.conversation_id == conversation_id,
            ConversationParticipant.user_id         != current_user.id,
            ConversationParticipant.is_deleted      == False,
        ).all()

        for p in other_participants:
            p.increment_unread()                   

        # Mark sender's own participant as read so their badge stays zero
        participant.mark_as_read()
        db.session.commit()

        msg_created_at = msg.created_at
        if msg_created_at and msg_created_at.tzinfo is None:
            msg_created_at = msg_created_at.replace(tzinfo=timezone.utc)
        if msg_created_at is None:
            msg_created_at = now_utc()

        try:
            socketio = current_app.extensions.get('socketio')
            if socketio:
                payload = {
                    'conversation_id': conversation_id,
                    'message_id':      msg.id,
                    'sender_id':       current_user.id,
                    'sender_name':     current_user.username,
                    'content':         content,
                    'created_at':      msg_created_at.isoformat(),
                    'message_type':    message_type_enum.value,
                    'reply_to_id':     msg.reply_to_id,
                }
                
                # 1. Send to everyone currently viewing this specific chat
                socketio.emit('new_message', payload, room=f'conv_{conversation_id}')
                
                # 2. Send to the sender's other tabs/devices
                socketio.emit('new_message', payload, room=f'user_{current_user.id}')
                
                # 3. FIX: Send to all other participants' private rooms so their 
                #    dashboard unread badges update even if the chat isn't open!
                for p in other_participants:
                    socketio.emit('new_message', payload, room=f'user_{p.user_id}')
                    
        except Exception as sock_err:
            current_app.logger.warning(f"Socket emit failed for message {msg.id}: {sock_err}")

        return jsonify({
            'message': {
                'id':            msg.id,
                'sender_id':     msg.sender_id,
                'sender_name':   current_user.username,
                'content':       msg.content,
                'created_at':    msg_created_at.isoformat(),
                'message_type':  message_type_enum.value,
                'reply_to_id':   msg.reply_to_id,
                'is_edited':     False,
                'read_count':    0,
                'is_read_by_me': False,
                'read_status':   msg.get_read_status(current_user.id),
            }
        }), 201

    except exc.SQLAlchemyError as db_error:
        current_app.logger.error(f"DB error in send_message: {db_error}", exc_info=True)
        db.session.rollback()
        return jsonify({'error': 'Database error'}), 500

    except Exception as error:
        current_app.logger.error(f"Error in send_message: {error}", exc_info=True)
        db.session.rollback()
        return jsonify({'error': 'Internal server error'}), 500

# ============================================================================
# SECTION 6: MARK SINGLE MESSAGE AS READ
# ============================================================================

@message_api_bp.route('/messages/<int:message_id>/read', methods=['POST'])
@login_required
def mark_message_read(message_id: int) -> Any:
    """Mark a specific message as read by the current user."""
    try:
        message = Message.query.get_or_404(message_id)
        
        participant = ConversationParticipant.query.filter_by(
            conversation_id=message.conversation_id,
            user_id=current_user.id,
            is_deleted=False,
        ).first() 

        if not participant:
            return jsonify({'error': 'Access denied'}), 403

        # In a normal conversation a user cannot self-read their own messages.
        # In self-chat the sender IS the only participant, so we allow it.
        if message.sender_id == current_user.id:
            participant_ids = [
                p.user_id for p in
                ConversationParticipant.query.filter_by(
                    conversation_id=message.conversation_id, is_deleted=False
                ).all()
            ]
            if participant_ids != [current_user.id]:
                return jsonify({'success': True, 'message': 'Cannot mark own messages as read'}), 200

        was_newly_marked = message.mark_read(current_user.id)

        if was_newly_marked:
            db.session.commit()

            try:
                socketio = current_app.extensions.get('socketio')
                if socketio:
                    read_status = message.get_read_status(message.sender_id)
                    socketio.emit('message_read_receipt', {
                        'message_id':      message_id,
                        'conversation_id': message.conversation_id,
                        'reader_id':       current_user.id,
                        'reader_name':     current_user.username,
                        'read_at':         now_utc().isoformat(),
                        'read_count':      message.read_count,
                        'status':          read_status.get('status', 'read'),
                    }, room=f'conv_{message.conversation_id}')
            except Exception as sock_err:
                current_app.logger.warning(f"Socket emit failed for read receipt: {sock_err}")

        return jsonify({
            'success':    True,
            'message_id': message_id,
            'read_count': message.read_count,
            'read_at':    message.get_read_at(current_user.id),
        }), 200

    except exc.SQLAlchemyError as db_error:
        current_app.logger.error(f"DB error in mark_message_read: {db_error}", exc_info=True)
        db.session.rollback()
        return jsonify({'error': 'Database error'}), 500
    except Exception as error:
        current_app.logger.error(f"Error in mark_message_read: {error}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500

# ============================================================================
# SECTION 7: MARK ENTIRE CONVERSATION AS READ
# ============================================================================

@message_api_bp.route('/conversations/<int:conversation_id>/read', methods=['POST'])
@login_required
def mark_conversation_read(conversation_id: int) -> Any:
    """Mark all messages in a conversation as read for the current user."""
    try:
        participant = ConversationParticipant.query.filter_by(
            conversation_id=conversation_id,
            user_id=current_user.id,
            is_deleted=False,
        ).first()
        
        if not participant:
            return jsonify({'error': 'Conversation not found or access denied'}), 404

        all_participant_ids = [
            p.user_id for p in
            ConversationParticipant.query.filter_by(
                conversation_id=conversation_id, is_deleted=False
            ).all()
        ]
        is_self_chat = all_participant_ids == [current_user.id]

        msg_query = Message.query.filter(
            Message.conversation_id == conversation_id,
            Message.is_deleted      == False,
        )
        if not is_self_chat:
            msg_query = msg_query.filter(Message.sender_id != current_user.id)
        messages = msg_query.all()

        newly_read_count = 0
        for message in messages:
            if message.mark_read(current_user.id):
                newly_read_count += 1

        if newly_read_count > 0:
            participant.mark_as_read()  # zeros unread_count + sets last_read_at
            db.session.commit()
            try:
                socketio = current_app.extensions.get('socketio')
                if socketio:
                    socketio.emit('conversation_read', {
                        'conversation_id': conversation_id,
                        'reader_id':       current_user.id,
                        'reader_name':     current_user.username,
                        'read_at':         now_utc().isoformat(),
                        'message_count':   newly_read_count,
                    }, room=f'conv_{conversation_id}')
            except Exception as sock_err:
                current_app.logger.warning(f"Socket emit failed for conversation read: {sock_err}")

        return jsonify({
            'success':           True,
            'conversation_id':   conversation_id,
            'marked_read_count': newly_read_count,
        }), 200

    except exc.SQLAlchemyError as db_error:
        current_app.logger.error(f"DB error in mark_conversation_read: {db_error}", exc_info=True)
        db.session.rollback()
        return jsonify({'error': 'Database error'}), 500
    except Exception as error:
        current_app.logger.error(f"Error in mark_conversation_read: {error}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500

# ============================================================================
# SECTION 8: GET READ STATUS FOR A MESSAGE
# ============================================================================

@message_api_bp.route('/messages/<int:message_id>/read-status', methods=['GET'])
@login_required
def get_message_read_status(message_id: int) -> Any:
    """Get read status information for a specific message."""
    try:
        message = Message.query.get_or_404(message_id)
        
        participant = ConversationParticipant.query.filter_by(
            conversation_id=message.conversation_id,
            user_id=current_user.id,
            is_deleted=False,
        ).first()

        if not participant:
            return jsonify({'error': 'Access denied'}), 403

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

        return jsonify({
            'message_id':         message_id,
            'total_participants': len(participants),
            'read_count':         message.read_count,
            'read_by':            read_by_list,
            'is_fully_read':      message.read_count >= len(participants),
            'read_status':        message.get_read_status(current_user.id),
        }), 200

    except Exception as error:
        current_app.logger.error(f"Error in get_message_read_status: {error}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500