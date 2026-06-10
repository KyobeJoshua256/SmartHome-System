"""
Message and Conversation Forms
Aligned to: Conversation, ConversationParticipant, Message (chats.py)
"""

from flask_wtf import FlaskForm
from wtforms import (
    StringField, TextAreaField, SelectField, BooleanField,
    SelectMultipleField, HiddenField, IntegerField, SubmitField,
)
from wtforms.validators import (
    DataRequired, Optional, Length, URL, NumberRange,
)

from HomeServer.models.chats import ConversationType, MessageType


# ============================================================================
# CONSTANTS — match model column lengths exactly
# ============================================================================

MAX_CONTENT      = 10_000   # Message.content @validates
MAX_DESCRIPTION  = 500      # Conversation.description String(500)
MAX_NAME         = 100      # Conversation.name        String(100)
MAX_ICON         = 200      # Conversation.icon        String(200)
MAX_FILE_NAME    = 255      # sensible cap; column is EncryptedText (unbounded)
MAX_MIME_TYPE    = 100      # Message.file_mime_type   String(100)
MAX_THUMBNAIL    = 500      # Message.thumbnail_url    String(500)
MAX_SEARCH       = 100      # search / filter fields


# ============================================================================
# CONVERSATION FORMS
# ============================================================================

class ConversationCreateForm(FlaskForm):
    """Create a new Conversation (direct, group, or broadcast)."""

    # Maps to Conversation.name — optional for DIRECT, required for GROUP/BROADCAST
    name = StringField(
        'Conversation Name',
        validators=[Optional(), Length(max=MAX_NAME)],
        render_kw={"placeholder": "e.g. Home Alerts (leave blank for direct chats)"},
    )
    description = StringField(
        'Description',
        validators=[Optional(), Length(max=MAX_DESCRIPTION)],
    )
    # Maps to Conversation.icon — file path or single emoji, String(200)
    icon = StringField(
        'Icon',
        validators=[Optional(), Length(max=MAX_ICON)],
        render_kw={"placeholder": "🏠 or /static/icons/home.png"},
    )

    # Aligned to ConversationType enum: DIRECT | GROUP | BROADCAST
    conversation_type = SelectField(
        'Type',
        choices=[
            (ConversationType.DIRECT.value,    'Direct — person to person'),
            (ConversationType.GROUP.value,     'Group — named multi-user chat'),
            (ConversationType.BROADCAST.value, 'Broadcast — one-to-all announcement'),
        ],
        default=ConversationType.DIRECT.value,
        validators=[DataRequired()],
    )

    participants = SelectMultipleField(
        'Initial Participants',
        coerce=int,
        validators=[DataRequired(message="Select at least one participant.")],
    )

    submit = SubmitField('Create Conversation')


class ConversationFilterForm(FlaskForm):
    """Filter the conversation list view."""

    # Only the three types that exist in ConversationType
    conversation_type = SelectField(
        'Type',
        choices=[
            ('',                               'All Types'),
            (ConversationType.DIRECT.value,    'Direct Messages'),
            (ConversationType.GROUP.value,     'Group Chats'),
            (ConversationType.BROADCAST.value, 'Broadcasts'),
        ],
        validators=[Optional()],
    )

    search = StringField(
        'Search',
        validators=[Optional(), Length(max=MAX_SEARCH)],
        render_kw={"placeholder": "Search by name or description…"},
    )

    # Maps to Conversation.is_archived
    show_archived = BooleanField('Show Archived', default=False)

    submit = SubmitField('Filter')


# ============================================================================
# MESSAGE FORMS
# ============================================================================

class MessageForm(FlaskForm):
    """Compose and send a chat message."""

    # Maps to Message.content (EncryptedText, validated to max 10 000 chars)
    body = TextAreaField(
        'Message',
        validators=[
            DataRequired(message="Message cannot be empty."),
            Length(max=MAX_CONTENT),
        ],
        render_kw={"rows": 2, "placeholder": "Type a message…"},
    )

    # Aligned to MessageType enum: TEXT 
    message_type = SelectField(
        'Message Type',
        choices=[
            (MessageType.TEXT.value,   'Text'),
        ],
        default=MessageType.TEXT.value,
        validators=[Optional()],
    )

    # Attachment fields — map directly to Message columns
    # file_url / original_url / file_name are EncryptedText (no length cap in DB,
    # but we enforce reasonable UI limits)
    file_url = StringField(
        'File URL',
        validators=[Optional(), URL(), Length(max=MAX_THUMBNAIL)],
    )
    original_url = StringField(
        'Original URL',
        validators=[Optional(), URL(), Length(max=MAX_THUMBNAIL)],
    )
    file_name = StringField(
        'File Name',
        validators=[Optional(), Length(max=MAX_FILE_NAME)],
    )
    # Message.file_size — Integer, bytes; CheckConstraint: > 0 when set
    file_size = IntegerField(
        'File Size (bytes)',
        validators=[Optional(), NumberRange(min=1)],
    )
    # Message.file_mime_type — String(100)
    file_mime_type = StringField(
        'MIME Type',
        validators=[Optional(), Length(max=MAX_MIME_TYPE)],
        render_kw={"placeholder": "e.g. image/jpeg"},
    )
    # Message.thumbnail_url — plain String(500)
    thumbnail_url = StringField(
        'Thumbnail URL',
        validators=[Optional(), URL(), Length(max=MAX_THUMBNAIL)],
    )

    # Threading — maps to Message.reply_to_id ForeignKey
    reply_to_id = HiddenField('Reply To Message ID', validators=[Optional()])

    submit = SubmitField('Send')


class MessageEditForm(FlaskForm):
    """Edit the content of an existing message (calls Message.edit())."""

    body = TextAreaField(
        'Edit Message',
        validators=[DataRequired(), Length(max=MAX_CONTENT)],
        render_kw={"rows": 3},
    )
    submit = SubmitField('Save Changes')


class MessageSearchForm(FlaskForm):
    """Search / filter messages within a conversation."""

    query = StringField(
        'Search Messages',
        validators=[Optional(), Length(max=MAX_SEARCH)],
        render_kw={"placeholder": "Search by keyword…"},
    )

    # Only types that exist in MessageType
    message_type = SelectField(
        'Message Type',
        choices=[
            ('',                      'All Types'),
            (MessageType.TEXT.value,  'Text'),
        ],
        validators=[Optional()],
    )

    sender_id = SelectField(
        'From User',
        coerce=int,
        validators=[Optional()],
        choices=[(0, 'Everyone')],   # populate at runtime
    )

    date_from = StringField(
        'From Date',
        validators=[Optional()],
        render_kw={"placeholder": "YYYY-MM-DD", "type": "date"},
    )
    date_to = StringField(
        'To Date',
        validators=[Optional()],
        render_kw={"placeholder": "YYYY-MM-DD", "type": "date"},
    )

    submit = SubmitField('Search')


# ============================================================================
# REACTION FORM
# ============================================================================

class MessageReactionForm(FlaskForm):
    """Add an emoji reaction (calls Message.add_reaction())."""

    emoji = StringField(
        'Emoji',
        validators=[
            DataRequired(message="Please provide an emoji."),
            Length(min=1, max=10),
        ],
        render_kw={"placeholder": "👍"},
    )
    submit = SubmitField('React')


# ============================================================================
# PARTICIPANT FORMS
# ============================================================================

class AddParticipantForm(FlaskForm):
    """Add a user to a Conversation (calls Conversation.add_participant())."""

    user_id = SelectField(
        'User to Add',
        coerce=int,
        validators=[DataRequired(message="Please select a user.")],
    )
    # Maps to ConversationParticipant.is_admin
    is_admin = BooleanField('Grant Admin Privileges', default=False)
    # Maps to ConversationParticipant.can_invite
    can_invite = BooleanField('Can Invite Others', default=False)

    submit = SubmitField('Add Participant')


class UpdateParticipantForm(FlaskForm):
    """Update a participant's role / notification settings."""

    # Maps to ConversationParticipant.is_admin
    is_admin = BooleanField('Admin Privileges', default=False)
    # Maps to ConversationParticipant.can_invite
    can_invite = BooleanField('Can Invite Others', default=False)
    # Maps to ConversationParticipant.is_muted
    is_muted = BooleanField('Mute Conversation', default=False)
    # Maps to ConversationParticipant.notification_enabled
    notification_enabled = BooleanField('Enable Notifications', default=True)

    submit = SubmitField('Update')


class RemoveParticipantForm(FlaskForm):
    """Confirm removal of a participant (calls Conversation.remove_participant())."""

    confirm = BooleanField(
        'Confirm Removal',
        validators=[DataRequired(message="Please confirm you want to remove this participant.")],
    )
    submit = SubmitField('Remove Participant')


# ============================================================================
# EXPORT REGISTRY
# ============================================================================

__all__ = [
    # Conversation
    'ConversationCreateForm',
    'ConversationFilterForm',
    # Message
    'MessageForm',
    'MessageEditForm',
    'MessageSearchForm',
    'MessageReactionForm',
    # Participants
    'AddParticipantForm',
    'UpdateParticipantForm',
    'RemoveParticipantForm',
]