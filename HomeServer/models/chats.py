from __future__ import annotations

import hashlib
import hmac as _hmac_mod
import os
from datetime import datetime, timezone
from enum import Enum as PyEnum

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum as SqlaEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    update,
)
from sqlalchemy.orm import relationship, validates
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.sql import func
from sqlalchemy.types import TypeDecorator, Text as SaText

from HomeServer import database


# ============================================================================
# Encryption helpers
# ============================================================================

def _get_fernet() -> Fernet:
    """
    Return a Fernet instance keyed by CHAT_ENCRYPTION_KEY env var.

    Generate once, store in .env, never commit to git:
        python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

    Only the chat-serving worker process needs this variable.
    The Flask-Admin process should NOT have it — admins will see ciphertext only.
    """
    raw = os.environ.get("CHAT_ENCRYPTION_KEY")
    if not raw:
        raise RuntimeError(
            "CHAT_ENCRYPTION_KEY is not set. "
            "Generate with: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    return Fernet(raw.encode())


class EncryptedText(TypeDecorator):
    """
    Transparent Fernet (AES-128-CBC + HMAC-SHA256) column type.

    - NULL  → stored as NULL
    - str   → encrypted ciphertext stored as TEXT
    - Reads decrypt automatically; bad key raises RuntimeError
    """
    impl     = SaText
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return _get_fernet().encrypt(value.encode("utf-8")).decode("utf-8")

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        try:
            return _get_fernet().decrypt(value.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError(
                "Message decryption failed — key may have changed or data is corrupt."
            ) from exc


def _hmac_uid(user_id: int) -> str:
    """
    16-char HMAC-SHA256 digest of user_id, keyed by CHAT_ENCRYPTION_KEY.

    Used as dict keys inside read_by and reactions JSON columns so that
    a DB reader cannot map receipts or reactions back to real user IDs.
    """
    key    = os.environ.get("CHAT_ENCRYPTION_KEY", "insecure-fallback")
    digest = _hmac_mod.new(
        key.encode("utf-8"),
        str(user_id).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:16]


# ============================================================================
# Enums
# ============================================================================

class MessageType(PyEnum):
    """Supported message content types — chat only."""
    TEXT   = "text"
    SYSTEM = "system"
     


class ConversationType(PyEnum):
    DIRECT    = "direct"
    GROUP     = "group"
    BROADCAST = "broadcast"   # one-to-many announcement channel


# ============================================================================
# Utility
# ============================================================================
from .utils import now_kampala, EAT

# ============================================================================
# Mixins
# ============================================================================

class TimestampMixin:
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SoftDeleteMixin:
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)
    is_deleted = Column(Boolean, default=False,  nullable=False, index=True)

    def soft_delete(self) -> None:
        self.is_deleted = True
        self.deleted_at = now_kampala()

    def restore(self) -> None:
        self.is_deleted = False
        self.deleted_at = None


# ============================================================================
# Conversation
# ============================================================================

class Conversation(TimestampMixin, SoftDeleteMixin, database.Model):
    """
    A chat conversation — direct, group, or broadcast.

    Direct    : exactly 2 participants, name is optional.
    Group     : named chat, multiple participants, admin roles apply.
    Broadcast : one sender, many listeners (announcement / alert channel).
    """
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True)
    conversation_type = Column(
        SqlaEnum(ConversationType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        index=True,
    )

    # Group / broadcast identity
    name        = Column(String(100), nullable=True)   # required for group/broadcast
    description = Column(String(500), nullable=True)
    icon        = Column(String(200), nullable=True)   # file path or single emoji char

    # State
    is_archived     = Column(Boolean,               default=False, nullable=False)
    is_muted        = Column(Boolean,               default=False, nullable=False)
    last_message_at = Column(DateTime(timezone=True), nullable=True, index=True)
    message_count   = Column(Integer,               default=0,     nullable=False)

    # Relationships
    participants = relationship(
        "ConversationParticipant",
        back_populates="conversation",
        cascade="all, delete-orphan",
    )
    messages = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )

    __table_args__ = (
        Index("idx_conv_type",         "conversation_type"),
        Index("idx_conv_last_message", "last_message_at"),
        Index("idx_conv_deleted",      "is_deleted"),
        Index("idx_conv_archived",     "is_archived"),
        CheckConstraint("message_count >= 0", name="ck_conv_message_count"),
    )

    # ------------------------------------------------------------------
    # Participant management
    # ------------------------------------------------------------------

    def add_participant(self, user_id: int, is_admin: bool = False) -> None:
        """Add user to conversation; no-op if already a member."""
        if not any(p.user_id == user_id for p in self.participants):
            self.participants.append(
                ConversationParticipant(user_id=user_id, is_admin=is_admin)
            )

    def remove_participant(self, user_id: int) -> None:
        """Soft-remove a participant (preserves message history)."""
        for p in self.participants:
            if p.user_id == user_id:
                p.soft_delete()
                break

    def get_participant(self, user_id: int) -> ConversationParticipant | None:
        return next((p for p in self.participants if p.user_id == user_id), None)

    def get_unread_count(self, user_id: int) -> int:
        """O(1) — reads cached unread_count from participant row."""
        p = self.get_participant(user_id)
        return p.unread_count if p else 0

    def get_pinned_messages(self) -> list[Message]:
        """All non-deleted pinned messages, oldest-pin first."""
        return [m for m in self.messages if m.is_pinned and not m.is_deleted]

    def __repr__(self) -> str:
        label = self.name or "unnamed"
        return f"<Conversation {self.conversation_type.value} '{label}'>"


# ============================================================================
# ConversationParticipant
# ============================================================================

class ConversationParticipant(TimestampMixin, SoftDeleteMixin, database.Model):
    """
    Membership record linking a User to a Conversation.
    Tracks per-user read state, notification preferences, and role.
    """
    __tablename__ = "conversation_participants"

    id              = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    user_id         = Column(Integer, ForeignKey("users.id"),         nullable=False)

    # Read tracking
    last_read_at = Column(DateTime(timezone=True), nullable=True,  index=True)
    unread_count = Column(Integer,                 default=0,       nullable=False)

    # Notification preferences
    is_muted             = Column(Boolean, default=False, nullable=False)
    notification_enabled = Column(Boolean, default=True,  nullable=False)

    # Role — meaningful for GROUP and BROADCAST conversations
    is_admin   = Column(Boolean, default=False, nullable=False)
    can_invite = Column(Boolean, default=False, nullable=False)

    # Relationships
    conversation = relationship("Conversation", back_populates="participants")
    user         = relationship("User",         back_populates="conversations")

    __table_args__ = (
        UniqueConstraint("conversation_id", "user_id", name="uq_participant"),
        Index("idx_part_conversation", "conversation_id"),
        Index("idx_part_user",         "user_id"),
        Index("idx_part_deleted",      "is_deleted"),
        Index("idx_part_last_read",    "last_read_at"),
        CheckConstraint("unread_count >= 0", name="ck_part_unread_count"),
    )

    def mark_as_read(self) -> None:
        """Zero the unread counter and record the read timestamp."""
        self.last_read_at = now_kampala()
        self.unread_count = 0

    def increment_unread(self) -> None:
        self.unread_count += 1

    def __repr__(self) -> str:
        return f"<Participant conv={self.conversation_id} user={self.user_id}>"


# ============================================================================
# Message
# ============================================================================

class Message(TimestampMixin, SoftDeleteMixin, database.Model):
    """
    A single chat message.

    Encrypted columns (Fernet AES-128-CBC + HMAC-SHA256)
    -----------------------------------------------------
    content      — message text or caption
    file_url     — served file URL (may point to compressed version)
    original_url — original-quality URL when a compressed copy was sent
    file_name    — original filename as uploaded by the sender

    Plain columns (no PII, safe for queries/sorting)
    ------------------------------------------------
    file_size, file_mime_type, thumbnail_url,
    media_width, media_height, media_duration,
    message_type, is_edited, is_pinned, read_count, created_at

    read_by / reactions JSON
    ------------------------
    Keys are 16-char HMAC digests of user IDs (_hmac_uid).
    Values are ISO-8601 timestamps (read_by) or lists of digests (reactions).
    No raw user IDs are written to the database.
    """
    __tablename__ = "messages"

    id              = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    sender_id       = Column(Integer, ForeignKey("users.id"),         nullable=False)

    # ------------------------------------------------------------------
    # Content  (encrypted)
    # ------------------------------------------------------------------
    message_type = Column(
        SqlaEnum(MessageType, values_callable=lambda x: [e.value for e in x]),
        default=MessageType.TEXT,
        nullable=False,
        index=True,
    )
    content = Column(EncryptedText, nullable=False)

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------
    file_url       = Column(EncryptedText, nullable=True)   # serving URL (compressed)
    original_url   = Column(EncryptedText, nullable=True)   # original-quality URL
    file_name      = Column(EncryptedText, nullable=True)   # original filename
    file_size      = Column(Integer,       nullable=True)   # bytes — plain
    file_mime_type = Column(String(100),   nullable=True)   # e.g. "audio/ogg"
    thumbnail_url  = Column(String(500),   nullable=True)   # thumbnail — plain

    # Media layout / playback hints (plain — no PII)
    is_compressed  = Column(Boolean, default=True,  nullable=True)
    media_width    = Column(Integer, nullable=True)   # px
    media_height   = Column(Integer, nullable=True)   # px
    media_duration = Column(Integer, nullable=True)   # seconds

    # ------------------------------------------------------------------
    # Edit state
    # ------------------------------------------------------------------
    is_edited = Column(Boolean,               default=False, nullable=False)
    edited_at = Column(DateTime(timezone=True), nullable=True)

    # ------------------------------------------------------------------
    # Read receipts
    # {_hmac_uid(user_id): "ISO-8601 timestamp", ...}
    # ------------------------------------------------------------------
    read_by      = Column(database.JSON,          nullable=True, default=None)
    read_count   = Column(Integer,                default=0,     nullable=False)
    last_read_at = Column(DateTime(timezone=True), nullable=True)

    # ------------------------------------------------------------------
    # Threading
    # ------------------------------------------------------------------
    reply_to_id = Column(Integer, ForeignKey("messages.id"), nullable=True, index=True)

    # ------------------------------------------------------------------
    # Pinning
    # ------------------------------------------------------------------
    is_pinned    = Column(Boolean,               default=False, nullable=False, index=True)
    pinned_at    = Column(DateTime(timezone=True), nullable=True)
    pinned_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    # ------------------------------------------------------------------
    # Reactions & extra metadata
    # reactions: {emoji: [_hmac_uid, ...]}
    # message_metadata: arbitrary JSON (used by SYSTEM messages for
    #   rendering context, e.g. {"event": "user_joined", "user_name": "Alice"})
    # ------------------------------------------------------------------
    reactions        = Column(database.JSON, nullable=True, default=None)
    message_metadata = Column("metadata", database.JSON, nullable=True, default=None)

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    conversation = relationship("Conversation", back_populates="messages")
    sender       = relationship(
        "User",
        back_populates="messages_sent",
        foreign_keys=[sender_id],
    )

    reply_to = relationship(
    "Message",
    foreign_keys=[reply_to_id],
    backref="replies",
    remote_side="[Message.id]", 
    ) 

    pinned_by = relationship("User", foreign_keys=[pinned_by_id])

    # ------------------------------------------------------------------
    # Indexes & constraints
    # ------------------------------------------------------------------
    __table_args__ = (
        Index("idx_msg_conversation", "conversation_id"),
        Index("idx_msg_sender",       "sender_id"),
        Index("idx_msg_created",      "created_at"),
        Index("idx_msg_deleted",      "is_deleted"),
        Index("idx_msg_type",         "message_type"),
        Index("idx_msg_read_count",   "read_count"),
        Index("idx_msg_reply",        "reply_to_id"),
        Index("idx_msg_pinned",       "is_pinned"),
        # Composite index for paginated chat queries
        Index("idx_msg_composite",    "conversation_id", "is_deleted", "created_at"),
        CheckConstraint(
            "file_size IS NULL OR file_size > 0",
            name="ck_msg_file_size",
        ),
        CheckConstraint("read_count >= 0", name="ck_msg_read_count"),
        CheckConstraint(
            "media_width IS NULL OR media_width > 0",
            name="ck_msg_media_width",
        ),
        CheckConstraint(
            "media_height IS NULL OR media_height > 0",
            name="ck_msg_media_height",
        ),
        CheckConstraint(
            "media_duration IS NULL OR media_duration >= 0",
            name="ck_msg_media_duration",
        ),
    )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @validates("content")
    def validate_content(self, key, content):
        """
        SYSTEM messages carry empty content — the client renders their
        display text from message_metadata (e.g. {"event": "user_joined"}).
        All other types require non-empty content under 10 000 chars.
        """
        msg_type = getattr(self, "message_type", None)
        if msg_type is MessageType.SYSTEM:
            return content or ""
        if not content or not content.strip():
            raise ValueError("Message content cannot be empty.")
        if len(content) > 10_000:
            raise ValueError("Message content exceeds 10 000 characters.")
        return content

    # ------------------------------------------------------------------
    # Edit
    # ------------------------------------------------------------------

    def edit(self, new_content: str) -> None:
        """Replace content. Raises ValueError on soft-deleted messages."""
        if self.is_deleted:
            raise ValueError("Cannot edit a deleted message.")
        self.content   = new_content   
        self.is_edited = True
        self.edited_at = now_kampala()

    # ------------------------------------------------------------------
    # Pinning
    # ------------------------------------------------------------------

    def pin(self, by_user_id: int) -> None:
        """Pin this message; record who pinned it and when."""
        self.is_pinned    = True
        self.pinned_at    = now_kampala()
        self.pinned_by_id = by_user_id

    def unpin(self) -> None:
        self.is_pinned    = False
        self.pinned_at    = None
        self.pinned_by_id = None

    # ------------------------------------------------------------------
    # Read receipts
    # ------------------------------------------------------------------

    def mark_read(self, user_id: int) -> bool:
        """
        Record that user_id has read this message.

        Returns True the first time; False if already recorded.
        HMAC digest is used as key so raw user IDs are never in the DB.
        flag_modified() is mandatory for SQLAlchemy to flush JSON mutations.
        """
        if self.read_by is None:
            self.read_by = {}

        key = _hmac_uid(user_id)
        if key not in self.read_by:
            self.read_by[key] = now_kampala().isoformat()
            self.read_count   = len(self.read_by)
            self.last_read_at = now_kampala()
            flag_modified(self, "read_by")
            return True
        return False

    def is_read_by(self, user_id: int) -> bool:
        return bool(self.read_by) and _hmac_uid(user_id) in self.read_by

    def get_read_at(self, user_id: int) -> str | None:
        """ISO timestamp of when user_id read this message, or None."""
        if not self.read_by:
            return None
        return self.read_by.get(_hmac_uid(user_id))

    def get_read_status(self, current_user_id: int) -> dict:
        """
        Read-status summary for use in the sender's own message bubble.

        Returns one of:
            {'show_status': False}
            {'show_status': True, 'status': 'sent',           'label': 'Sent'}
            {'show_status': True, 'status': 'delivered',      'label': 'Delivered'}
            {'show_status': True, 'status': 'partially_read', 'label': 'Read by 1/3'}
            {'show_status': True, 'status': 'read',           'label': 'Read by all'}
        """
        if self.sender_id != current_user_id:
            return {"show_status": False}

        conv      = self.conversation
        all_parts = conv.participants if conv else []
        others    = [p for p in all_parts
                     if p.user_id != current_user_id and not p.is_deleted]
        total     = len(others)

        if total == 0:
            return {"show_status": True, "status": "sent", "label": "Sent"}

        read_count = sum(1 for p in others if self.is_read_by(p.user_id))

        if read_count == 0:
            return {"show_status": True, "status": "delivered", "label": "Delivered"}
        if read_count < total:
            return {
                "show_status": True,
                "status": "partially_read",
                "label": f"Read by {read_count}/{total}",
            }
        return {"show_status": True, "status": "read", "label": "Read by all"}

    # ------------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------------

    def add_reaction(self, emoji: str, user_id: int) -> None:
        """Add emoji reaction from user_id. Keys are HMAC digests."""
        if self.reactions is None:
            self.reactions = {}
        if emoji not in self.reactions:
            self.reactions[emoji] = []

        key = _hmac_uid(user_id)
        if key not in self.reactions[emoji]:
            self.reactions[emoji].append(key)
            flag_modified(self, "reactions")

    def remove_reaction(self, emoji: str, user_id: int) -> None:
        """Remove user_id's reaction for emoji."""
        if not self.reactions or emoji not in self.reactions:
            return
        key = _hmac_uid(user_id)
        if key in self.reactions[emoji]:
            self.reactions[emoji].remove(key)
            if not self.reactions[emoji]:
                del self.reactions[emoji]
            flag_modified(self, "reactions")

    def get_reaction_counts(self) -> dict[str, int]:
        """Return {emoji: count} — safe to send to any client."""
        if not self.reactions:
            return {}
        return {emoji: len(users) for emoji, users in self.reactions.items()}

    def has_reacted(self, emoji: str, user_id: int) -> bool:
        if not self.reactions or emoji not in self.reactions:
            return False
        return _hmac_uid(user_id) in self.reactions[emoji]

    # ------------------------------------------------------------------
    # Metadata helpers
    # ------------------------------------------------------------------

    def set_metadata(self, data: dict) -> None:
        """Replace message_metadata entirely."""
        self.message_metadata = data
        flag_modified(self, "message_metadata")

    def update_metadata(self, updates: dict) -> None:
        """Merge updates into existing message_metadata."""
        base = self.message_metadata or {}
        base.update(updates)
        self.message_metadata = base
        flag_modified(self, "message_metadata")

    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<Message id={self.id} "
            f"type={self.message_type.value} "
            f"sender={self.sender_id}>"
        )


# ============================================================================
# Event listeners
# ============================================================================

@event.listens_for(Message, "after_insert")
def _sync_conversation_on_insert(mapper, connection, target: Message):
    """
    Keep Conversation.last_message_at and message_count accurate after
    every new Message insert — without loading the parent Conversation object.
    """
    tbl = Conversation.__table__
    connection.execute(
        update(tbl)
        .where(tbl.c.id == target.conversation_id)
        .values(
            last_message_at=target.created_at,
            message_count=tbl.c.message_count + 1,
        )
    )


# ============================================================================
# Relationship stubs — add these to your existing User model
# ============================================================================
#
# class User(... database.Model):
#     ...
#     conversations = relationship(
#         "ConversationParticipant",
#         back_populates="user",
#         cascade="all, delete-orphan",
#     )
#     messages_sent = relationship(
#         "Message",
#         back_populates="sender",
#         foreign_keys="Message.sender_id",
#     )


# ============================================================================
# Public API
# ============================================================================

__all__ = [
    "ConversationType",
    "MessageType",
    "Conversation",
    "ConversationParticipant",
    "Message",
    "TimestampMixin",
    "SoftDeleteMixin",
    "EncryptedText",
    "_hmac_uid",
    "now_kampala",
]