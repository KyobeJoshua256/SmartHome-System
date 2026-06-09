from flask import request, session
from flask_socketio import Namespace, emit, join_room, leave_room
import time


class AuthNamespace(Namespace):
    """
    Socket.IO namespace: /auth

    Rooms
    -----
    Each authenticated session gets its own room keyed on Flask session ID.
    This lets the server push to a specific browser session if needed.

    Events (client → server)
    ------------------------
    lockout:check         — ask server for current lockout state
    lockout:set_server    — store a new lockout (fired when LockoutClock.show() runs)
    lockout:clear_server  — clear lockout (fired when timer expires client-side)

    Events (server → client)
    ------------------------
    lockout:status        — current lockout payload (response to lockout:check)
    lockout:set           — pushed to other sessions when a lockout is created
    lockout:cleared       — pushed when lockout is cleared
    """

    # In-memory store: { session_id: { identifier, locked_until_ms, total_duration_seconds } }
    # For production, swap this with Redis or your DB.
    _lockouts: dict = {}

    def on_connect(self):
        sid = request.sid
        flask_session_id = session.get('_id', sid)
        join_room(flask_session_id)

    def on_disconnect(self):
        sid = request.sid
        flask_session_id = session.get('_id', sid)
        leave_room(flask_session_id)

    def on_lockout_check(self, data=None):
        """Client is asking: am I locked out right now?"""
        flask_session_id = session.get('_id', request.sid)
        lockout = self._lockouts.get(flask_session_id)

        if not lockout:
            emit('lockout:status', {'locked': False})
            return

        now_ms = int(time.time() * 1000)
        if lockout['locked_until_ms'] <= now_ms:
            # Expired — clean up
            self._lockouts.pop(flask_session_id, None)
            emit('lockout:status', {'locked': False, 'expired': True})
        else:
            emit('lockout:status', {
                'locked': True,
                'identifier': lockout['identifier'],
                'locked_until_ms': lockout['locked_until_ms'],
                'total_duration_seconds': lockout['total_duration_seconds']
            })

    def on_lockout_set_server(self, data):
        """
        Client reports a lockout was triggered.
        Store it and broadcast to any other open sessions.
        """
        identifier       = str(data.get('identifier', ''))
        locked_until_ms  = int(data.get('locked_until_ms', 0))
        duration_seconds = int(data.get('duration_seconds', 0))

        if not identifier or locked_until_ms <= int(time.time() * 1000):
            return  # ignore stale or empty payloads

        flask_session_id = session.get('_id', request.sid)
        self._lockouts[flask_session_id] = {
            'identifier': identifier,
            'locked_until_ms': locked_until_ms,
            'total_duration_seconds': duration_seconds
        }

        # Notify any other tabs/sessions (skip sender)
        emit('lockout:set', {
            'identifier': identifier,
            'locked_until_ms': locked_until_ms,
            'total_duration_seconds': duration_seconds
        }, room=flask_session_id, skip_sid=request.sid)

    def on_lockout_clear_server(self, data=None):
        """Client reports lockout has expired. Clean up and broadcast."""
        flask_session_id = session.get('_id', request.sid)
        self._lockouts.pop(flask_session_id, None)
        emit('lockout:cleared', {}, room=flask_session_id, skip_sid=request.sid)


def register_auth_namespace(socketio):
    """Call this from your app factory after creating SocketIO."""
    socketio.on_namespace(AuthNamespace('/auth'))