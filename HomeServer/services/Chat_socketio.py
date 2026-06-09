from flask_login import current_user
from flask_socketio import join_room, leave_room, emit

from HomeServer.models.chats import ConversationParticipant
from HomeServer.models.utils import now_utc
from HomeServer import database as db


def message_socket_events(socketio):
    """Attach all SocketIO event handlers to *socketio*."""

    # ── Connection lifecycle ───────────────────────────────────────────────────

    @socketio.on('connect')
    def on_connect():
        """Authenticate the connecting socket and join the user's private room.

        The private ``user_{id}`` room is used by send_message in MessageApi.py
        to deliver new_message events to the sender's own other tabs/devices.
        Returns False (rejected) for unauthenticated connections.
        """
        if not current_user.is_authenticated:
            return False

        join_room(f'user_{current_user.id}')

        try:
            current_user.is_online = True
            current_user.last_seen = now_utc()
            db.session.commit()
        except Exception:
            db.session.rollback()

        emit('connected', {
            'user_id':  current_user.id,
            'username': current_user.username,
            'status':   'online',
        })

    @socketio.on('disconnect')
    def on_disconnect():
        """Mark the user offline when their last socket closes."""
        if not current_user.is_authenticated:
            return

        try:
            user_room    = f'user_{current_user.id}'
            room_sockets = socketio.server.manager.get_participants('/', user_room)
            # The disconnecting socket is still counted at this point, so the
            # threshold is > 1 to detect the truly last socket for this user.
            still_connected = sum(1 for _ in room_sockets) > 1
        except Exception:
            still_connected = False

        try:
            if not still_connected:
                current_user.is_online = False
            current_user.last_seen = now_utc()
            db.session.commit()
        except Exception:
            db.session.rollback()

    # ── Conversation room management ───────────────────────────────────────────

    @socketio.on('join_conversation')
    def on_join_conversation(data):
        """Join the socket room for a conversation after verifying participation.

        Called by UserMessaging.js _openConv() and on socket reconnect.
        Emits ``joined_conversation`` back to the caller on success so the JS
        can clear the unread badge immediately without waiting for an HTTP round
        trip.
        """
        if not current_user.is_authenticated:
            return

        conv_id = data.get('conversation_id')
        if not conv_id:
            return

        participant = ConversationParticipant.query.filter_by(
            conversation_id=conv_id,
            user_id=current_user.id,
            is_deleted=False,
        ).first()

        if not participant:
            emit('error', {'message': 'Access denied to conversation'})
            return

        join_room(f'conv_{conv_id}')
        emit('joined_conversation', {
            'conversation_id': conv_id,
            'user_id':         current_user.id,
            'username':        current_user.username,
        })

    @socketio.on('leave_conversation')
    def on_leave_conversation(data):
        """Leave the socket room for a conversation.

        Called by UserMessaging.js when the user clicks the back button in the
        chat panel, closing the active conversation.
        """
        if not current_user.is_authenticated:
            return

        conv_id = data.get('conversation_id')
        if conv_id:
            leave_room(f'conv_{conv_id}')