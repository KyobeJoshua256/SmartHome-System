window.MessagingModule = (function () {
'use strict';
// ── State ──────────────────────────────────────────────────────────────────
let _data           = null;
let _conversations  = [];
let _active         = null;
let _socket         = null;
let _initialized    = false;
let _userId         = null;
let _username       = null;
let _avatarColors   = {};
let _scrollObserver = null;
let _typingTimer    = null;

// API base path
const API_BASE = '/chat';

// ── DOM references (populated by _cacheDOM) ────────────────────────────────
let convList, convSearch, chatEmpty, chatView,
    chatMsgs, chatInput, chatCompose, sendBtn,
    chatHdAvatar, chatHdName, chatHdSub,
    chatBackBtn, chatSidebar,
    newConvBtn, newConvModal, closeNewConv, userSearch, userResults;

// ── Public API ─────────────────────────────────────────────────────────────
function preload(conversations) {
  _conversations = conversations || [];
}

function initSocket(dashData) {
  if (_socket) return;
  _data     = dashData;
  _userId   = dashData.userId || dashData.user?.id;
  _username = dashData.username || dashData.user?.username;
  
  if (dashData.chat && dashData.chat.conversations) {
    _conversations = dashData.chat.conversations;
  } else if (dashData.conversations) {
    _conversations = dashData.conversations;
  }
  _initSocket();
}

function init(dashData) {
  if (_initialized) return;
  _initialized = true;
  _data        = dashData;
  _userId      = dashData.userId || dashData.user?.id;
  _username    = dashData.username || dashData.user?.username;

  if (dashData.chat && dashData.chat.conversations) {
    _conversations = dashData.chat.conversations;
  } else if (dashData.conversations) {
    _conversations = dashData.conversations;
  } else {
    _conversations = _conversations || [];
  }

  _cacheDOM();
  _renderConvList(_conversations);
  _bindEvents();
  if (!_socket) _initSocket();
  _warmCsrf();
}

/**
 * NEW: Public method to close the active conversation.
 * Called by userdashboard.js when the user switches to a different section.
 */
function leaveActiveConversation() {
  _closeActiveConv();
}

function _warmCsrf() { /* delegated to CsrfManager */ }

// ── DOM cache ──────────────────────────────────────────────────────────────
function _cacheDOM() {
  convList     = document.getElementById('convList');
  convSearch   = document.getElementById('convSearch');
  chatEmpty    = document.getElementById('chatEmpty');
  chatView     = document.getElementById('chatView');
  chatMsgs     = document.getElementById('chatMsgs');
  chatInput    = document.getElementById('chatInput');
  chatCompose  = document.getElementById('chatCompose');
  sendBtn      = document.getElementById('sendBtn');
  chatHdAvatar = document.getElementById('chatHdAvatar');
  chatHdName   = document.getElementById('chatHdName');
  chatHdSub    = document.getElementById('chatHdSub');
  chatBackBtn  = document.getElementById('chatBackBtn');
  chatSidebar  = document.getElementById('chatSidebar');
  newConvBtn   = document.getElementById('newConvBtn');
  newConvModal = document.getElementById('newConvModal');
  closeNewConv = document.getElementById('closeNewConv');
  userSearch   = document.getElementById('userSearch');
  userResults  = document.getElementById('userResults');
}

// ── Conversation list ──────────────────────────────────────────────────────
function _renderConvList(convs, filter = '') {
  if (!convList) return;
  const filtered = filter
    ? convs.filter(c => c.display_name && c.display_name.toLowerCase().includes(filter.toLowerCase()))
    : convs;

  if (!filtered.length) {
    convList.innerHTML = `<div class="conv-empty" style="padding:24px;text-align:center;color:rgba(255,255,255,.3);font-size:.82rem;">${
      filter ? 'No conversations match.' : 'No conversations yet.'
    }</div>`;
    return;
  }

  convList.innerHTML = filtered.map(c => {
    const color  = _avatarColor(c.avatar_initial || '?');
    const unread = c.unread_count > 0
      ? `<span class="conv-badge" aria-label="${c.unread_count} unread">${c.unread_count > 99 ? '99+' : c.unread_count}</span>` : '';
    const active = _active === c.conversation_id ? ' active' : '';
    const mutedIcon = c.is_muted ? '<i class="fas fa-bell-slash conv-muted" title="Muted"></i>' : '';
    
    return `<div class="conv-item${active}" data-conv-id="${c.conversation_id}">
       <div class="conv-av" style="background:${color}">${esc(c.avatar_initial || '?')}</div>
       <div class="conv-info">
         <div class="conv-name">${esc(c.display_name)}</div>
         <div class="conv-preview">${esc(c.last_message || 'No messages yet')}</div>
       </div>
       <div class="conv-meta">
         <span class="conv-time">${esc(c.last_message_time || '')}</span>
        ${unread}
        ${mutedIcon}
       </div>
     </div>`;
  }).join('');

  convList.querySelectorAll('.conv-item').forEach(item => {
    item.addEventListener('click', () => _openConv(parseInt(item.dataset.convId, 10)));
  });
}

// ── Open / load conversation ───────────────────────────────────────────────
async function _openConv(convId) {
  _active = convId;
  const conv = _conversations.find(c => c.conversation_id === convId);
  if (!conv) return;

  convList?.querySelectorAll('.conv-item').forEach(el => {
    el.classList.toggle('active', parseInt(el.dataset.convId, 10) === convId);
  });

  chatSidebar?.closest('.chat-shell')?.classList.add('conv-open');

  const color = _avatarColor(conv.avatar_initial || '?');
  if (chatHdAvatar) {
    chatHdAvatar.textContent      = conv.avatar_initial || '?';
    chatHdAvatar.style.background = color;
  }
  if (chatHdName) chatHdName.textContent = conv.display_name;
  if (chatHdSub)  chatHdSub.textContent  = conv.is_self ? 'You (saved messages)' : 'Direct message';

  if (chatEmpty) chatEmpty.hidden = true;
  if (chatView)  chatView.hidden  = false;
  if (chatInput) chatInput.focus();

  if (chatMsgs) {
    chatMsgs.innerHTML = '<div class="loading-spin"><i class="fas fa-spinner fa-spin"></i> Loading messages...</div>';
  }

  if (_socket?.connected) {
    _socket.emit('join_conversation', { conversation_id: convId });
  }

  try {
    const res  = await fetch(`${API_BASE}/conversations/${convId}/messages`);
    const json = await res.json();
    if (!res.ok) throw new Error(json.error || 'Failed to load messages');

    _renderMessages(json.messages || []);
    _scrollToBottom();

    const c = _conversations.find(x => x.conversation_id === convId);
    if (c) c.unread_count = 0;
    _updateUnreadBadge();
    _renderConvList(_conversations, convSearch?.value || '');

    try {
      await fetch(`${API_BASE}/conversations/${convId}/read`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': await _csrf() },
      });
    } catch (_) {}

  } catch (err) {
    if (chatMsgs) {
      chatMsgs.innerHTML = `<div class="empty-state">
         <i class="fas fa-triangle-exclamation"></i><p>${esc(err.message)}</p>
       </div>`;
    }
  }
}

// ── Render messages ────────────────────────────────────────────────────────
function _renderMessages(messages) {
  if (!chatMsgs) return;
  if (!messages.length) {
    chatMsgs.innerHTML = '<div class="empty-state"><i class="fas fa-comment-slash"></i><p>No messages yet. Say hello!</p></div>';
    return;
  }
  chatMsgs.innerHTML = messages.map(m => _msgHTML(m)).join('');
  _initScrollObserver();
}

function _msgHTML(m) {
  const mine = m.sender_id === _userId;
  if (m.message_type === 'system') return `<div class="msg-sys">${esc(m.content)}</div>`;

  const time       = _fmtTime(m.created_at);
  const editedMark = m.is_edited ? '  <i class="fas fa-pen" style="font-size:.6rem;opacity:.4"></i>' : '';
  const activeConv = _conversations.find(c => c.conversation_id === _active);
  const isSelfChat = activeConv?.is_self === true;

  let readStatusHtml = '';
  if (mine) {
    if (isSelfChat) {
      readStatusHtml = `<span class="msg-read-status" title="Saved — ${time}"><i class="fas fa-check-double"></i></span>`;
    } else if (m.read_status) {
      const rs = m.read_status;
      const icon = (rs.status === 'partially_read' || rs.status === 'read') ? 'fa-check-double' : 'fa-check';
      const countBadge = (rs.status === 'partially_read' || rs.status === 'read') && m.read_count > 0 ? `<small>${m.read_count}</small>` : '';
      readStatusHtml = `<span class="msg-read-status" title="${esc(rs.label || 'Delivered')} — ${time}"><i class="fas ${icon}"></i>${countBadge}</span>`;
    } else {
      readStatusHtml = `<span class="msg-read-status" title="Sent — ${time}"><i class="fas fa-check"></i></span>`;
    }
  }

  const senderInitial = (m.sender_name || '?')[0].toUpperCase();
  const avatarColor   = _avatarColor(senderInitial);
  const avatarHtml    = !mine ? `<div class="msg-avatar" style="background:${avatarColor}">${senderInitial}</div>` : '';
  const senderLabel   = !mine && m.sender_name !== _username ? `<div class="msg-sender-name">${esc(m.sender_name || '')}</div>` : '';

  return `<div class="msg-row ${mine ? 'mine' : ''}" data-msg-id="${m.id}">
    ${avatarHtml}
     <div class="msg-body">
      ${senderLabel}
       <div class="msg-bubble">${esc(m.content)}${editedMark}</div>
       <div class="msg-meta">
         <span class="msg-time">${time}</span>
        ${readStatusHtml}
       </div>
     </div>
   </div>`;
}

function _appendMessage(m) {
  if (!chatMsgs) return;
  if (chatMsgs.querySelector(`.msg-row[data-msg-id="${m.id}"]`)) return;
  
  chatMsgs.querySelector('.empty-state')?.remove();
  chatMsgs.insertAdjacentHTML('beforeend', _msgHTML(m));
  _scrollToBottom();

  const activeConv = _conversations.find(c => c.conversation_id === _active);
  if (m.sender_id !== _userId && _active != null && !activeConv?.is_self) {
    _markMessagesRead([String(m.id)]);
  }

  if (_scrollObserver) {
    const newRow = chatMsgs.lastElementChild;
    if (newRow && !newRow.classList.contains('mine')) _scrollObserver.observe(newRow);
  }
}

// ── Read receipts ──────────────────────────────────────────────────────────
async function _markMessagesRead(messageIds) {
  if (!messageIds.length) return;
  const fresh = messageIds.filter(id => {
    const row = chatMsgs?.querySelector(`.msg-row[data-msg-id="${id}"]`);
    return row && !row.classList.contains('msg-read');
  });
  if (!fresh.length) return;

  await Promise.all(fresh.map(async id => {
    try {
      const res = await fetch(`${API_BASE}/messages/${id}/read`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': await _csrf() },
      });
      if (res.ok) {
        const row = chatMsgs?.querySelector(`.msg-row[data-msg-id="${id}"]`);
        if (row) row.classList.add('msg-read');
      }
    } catch (_) {}
  }));
}

function _initScrollObserver() {
  if (_scrollObserver) _scrollObserver.disconnect();
  if (!chatMsgs) return;
  const activeConv = _conversations.find(c => c.conversation_id === _active);
  if (activeConv?.is_self) return;

  _scrollObserver = new IntersectionObserver(entries => {
    const ids = entries
      .filter(e => e.isIntersecting && !e.target.classList.contains('mine') && !e.target.classList.contains('msg-read'))
      .map(e => e.target.dataset.msgId)
      .filter(Boolean);
    if (ids.length) _markMessagesRead(ids);
  }, { threshold: 0.5 });

  chatMsgs.querySelectorAll('.msg-row:not(.mine)').forEach(row => _scrollObserver.observe(row));
}

// ── Socket.IO ──────────────────────────────────────────────────────────────
function _initSocket() {
  if (typeof io === 'undefined') { console.warn('Socket.IO not loaded'); return; }
  
  _socket = io({ transports: ['websocket', 'polling'], reconnection: true, reconnectionDelay: 1000, withCredentials: true });

  _socket.on('connect_error', (err) => console.error('Socket.IO connection error:', err.message));
  _socket.on('connect', () => {
    console.log('Socket connected successfully!');
    if (_active) _socket.emit('join_conversation', { conversation_id: _active });
  });

  _socket.on('joined_conversation', data => {
    const convId = data.conversation_id;
    if (convId === _active) {
      const c = _conversations.find(x => x.conversation_id === convId);
      if (c && c.unread_count > 0) {
        c.unread_count = 0;
        _updateUnreadBadge();
        _renderConvList(_conversations, convSearch?.value || '');
      }
    }
  });

  _socket.on('new_message', data => {
    const { conversation_id, message_id, sender_id, sender_name, content, created_at, message_type } = data;

    if (conversation_id === _active) {
      const isSelfConv = _conversations.find(c => c.conversation_id === conversation_id)?.is_self;
      const shouldShow = isSelfConv ? true : sender_id !== _userId;
      if (shouldShow) {
        _appendMessage({
          id: message_id, sender_id, sender_name, content, created_at,
          message_type: message_type || 'text', is_edited: false, read_count: 0, read_status: null,
        });
      }
    }

    _updateConvPreview(conversation_id, content, _fmtTime(created_at));

    if (conversation_id !== _active && sender_id !== _userId) {
      const c = _conversations.find(x => x.conversation_id === conversation_id);
      if (c) c.unread_count = (c.unread_count || 0) + 1;
      _updateUnreadBadge();
      _renderConvList(_conversations, convSearch?.value || '');
    }
  });

  _socket.on('message_read_receipt', data => {
    const { message_id, conversation_id, read_count, status } = data;
    if (conversation_id !== _active) return;
    const row = chatMsgs?.querySelector(`.msg-row[data-msg-id="${message_id}"]`);
    if (!row?.classList.contains('mine')) return;

    let span = row.querySelector('.msg-read-status');
    if (!span) {
      const meta = row.querySelector('.msg-meta');
      if (meta) { span = document.createElement('span'); span.className = 'msg-read-status'; meta.appendChild(span); }
    }
    if (span) {
      const icon = (status === 'read' || status === 'partially_read') ? 'fa-check-double' : 'fa-check';
      const badge = read_count > 0 ? `<small>${read_count}</small>` : '';
      span.innerHTML = `<i class="fas ${icon}"></i>${badge}`;
      span.title = status === 'read' ? 'Read' : status === 'partially_read' ? `Read by ${read_count} people` : 'Delivered';
    }
  });

  _socket.on('conversation_read', data => {
    if (data.conversation_id !== _active) return;
    chatMsgs?.querySelectorAll('.msg-row.mine').forEach(row => {
      const span = row.querySelector('.msg-read-status');
      if (span) { span.innerHTML = '<i class="fas fa-check-double"></i>'; span.title = 'Read by recipient'; }
    });
  });
}

// ── Event binding ──────────────────────────────────────────────────────────
/**
 * NEW: Centralized function to close the active chat view and clear state.
 */
function _closeActiveConv() {
  if (_active && _socket?.connected) {
    _socket.emit('leave_conversation', { conversation_id: _active });
  }
  if (chatView)  chatView.hidden  = true;
  if (chatEmpty) chatEmpty.hidden = false;
  chatSidebar?.closest('.chat-shell')?.classList.remove('conv-open');
  _active = null;
}

function _bindEvents() {
  if (chatCompose) {
    chatCompose.addEventListener('submit', async e => {
      e.preventDefault();
      const content = (chatInput?.value || '').trim();
      if (!content || !_active) return;

      if (sendBtn)   sendBtn.disabled = true;
      if (chatInput) chatInput.value  = '';
      _autoResize();

      try {
        const res  = await fetch(`${API_BASE}/conversations/${_active}/messages`, {
          method:  'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRFToken': await _csrf() },
          body:    JSON.stringify({ content }),
        });
        const json = await res.json();
        if (!res.ok) throw new Error(json.error || 'Failed to send');

        _appendMessage(json.message);
        _updateConvPreview(_active, content, _fmtTime(new Date().toISOString()));
      } catch (err) {
        showToast('error', err.message);
        if (chatInput) chatInput.value = content;
      } finally {
        if (sendBtn)   sendBtn.disabled = false;
        if (chatInput) chatInput.focus();
      }
    });
  }

  if (chatInput) {
    chatInput.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        chatCompose?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
      }
    });
    chatInput.addEventListener('input', _autoResize);
  }

  if (chatBackBtn) {
    chatBackBtn.addEventListener('click', () => {
      _closeActiveConv(); // Uses the new centralized function
    });
  }

  if (convSearch) {
    convSearch.addEventListener('input', debounce(() => _renderConvList(_conversations, convSearch.value), 200));
  }

  if (newConvBtn) {
    newConvBtn.addEventListener('click', e => {
      e.preventDefault();
      if (newConvModal) { newConvModal.style.display = 'grid'; newConvModal.removeAttribute('hidden'); }
      userSearch?.focus();
    });
  }

  if (closeNewConv) closeNewConv.addEventListener('click', e => { e.preventDefault(); _closeModal(); });
  if (newConvModal) newConvModal.addEventListener('click', e => { if (e.target === newConvModal) _closeModal(); });

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && newConvModal && !newConvModal.hasAttribute('hidden')) _closeModal();
  });

  if (userSearch) userSearch.addEventListener('input', debounce(_searchUsers, 300));
}

// ── New conversation ───────────────────────────────────────────────────────
async function _searchUsers() {
  const q = userSearch?.value?.trim();
  if (!userResults) return;
  if (!q || q.length < 2) { userResults.innerHTML = ''; return; }
  
  try {
    const res   = await fetch(`${API_BASE}/users/search?q=${encodeURIComponent(q)}`);
    const json  = await res.json();
    const users = json.users || [];

    if (!users.length) {
      userResults.innerHTML = '<p style="color:rgba(255,255,255,.3);font-size:.82rem;text-align:center;padding:12px">No users found</p>';
      return;
    }

    userResults.innerHTML = users.map(u => `
       <div class="user-result-item" data-user-id="${u.id}">
         <div class="user-result-av">${esc((u.username[0] || '?').toUpperCase())}</div>
         <div>
           <div class="user-result-name">${esc(u.username)}</div>
           <div class="user-result-phone">${esc(u.phone || '')}</div>
         </div>
       </div>
    `).join('');

    userResults.querySelectorAll('.user-result-item').forEach(item => {
      item.addEventListener('click', () => _startConversation(parseInt(item.dataset.userId, 10)));
    });
  } catch (_) {
    userResults.innerHTML = '<p style="color:rgba(255,255,255,.3);font-size:.82rem;text-align:center;padding:12px">Search error</p>';
  }
}

async function _startConversation(targetUserId) {
  try {
    const res  = await fetch(`${API_BASE}/conversations/find-or-create`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': await _csrf() },
      body:    JSON.stringify({ recipient_id: targetUserId }),
    });
    const json = await res.json();
    if (!res.ok) throw new Error(json.error || 'Failed to create conversation');

    const conv = json.conversation;
    if (!_conversations.find(c => c.conversation_id === conv.conversation_id)) _conversations.unshift(conv);

    _closeModal();
    _renderConvList(_conversations);
    await _openConv(conv.conversation_id);
  } catch (err) {
    showToast('error', err.message);
  }
}

function _closeModal() {
  if (newConvModal) { newConvModal.style.display = 'none'; newConvModal.setAttribute('hidden', 'hidden'); }
  if (userSearch)   userSearch.value     = '';
  if (userResults)  userResults.innerHTML = '';
}

// ── Helpers ────────────────────────────────────────────────────────────────
function _updateConvPreview(convId, text, time) {
  const c = _conversations.find(x => x.conversation_id === convId);
  if (c) { c.last_message = text.substring(0, 60); c.last_message_time = time; }
  const idx = _conversations.findIndex(x => x.conversation_id === convId);
  if (idx > 0) { const [item] = _conversations.splice(idx, 1); _conversations.unshift(item); }
  _renderConvList(_conversations, convSearch?.value || '');
}

function _updateUnreadBadge() {
  const total = _conversations.reduce((s, c) => s + (c.unread_count || 0), 0);
  if (window.dashboardSetUnread) window.dashboardSetUnread(total);
  const navBadge = document.getElementById('navUnreadBadge');
  if (navBadge) { navBadge.textContent = total; navBadge.hidden = total === 0; }
}

function _fmtTime(isoStr) {
  try {
    const d  = new Date(isoStr);
    const tz = 'Africa/Kampala';
    const toDateStr = dt => dt.toLocaleDateString('en-UG', { timeZone: tz });
    const isToday   = toDateStr(d) === toDateStr(new Date());
    return isToday
      ? d.toLocaleTimeString('en-UG', { timeZone: tz, hour: '2-digit', minute: '2-digit', hour12: false })
      : d.toLocaleDateString('en-UG', { timeZone: tz, month: 'short', day: 'numeric' });
  } catch { return ''; }
}

function _avatarColor(initial) {
  if (_avatarColors[initial]) return _avatarColors[initial];
  let hash = 0;
  for (const c of String(initial)) hash = (hash * 31 + c.charCodeAt(0)) & 0xffffffff;
  const color = `hsl(${Math.abs(hash) % 360},55%,42%)`;
  _avatarColors[initial] = color;
  return color;
}

function _autoResize() {
  if (!chatInput) return;
  chatInput.style.height = 'auto';
  chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + 'px';
}

function _scrollToBottom() {
  if (chatMsgs) chatMsgs.scrollTop = chatMsgs.scrollHeight;
}

async function _csrf() {
  if (window.CsrfManager?.ready) await window.CsrfManager.ready;
  if (_data?.csrfToken) return _data.csrfToken;
  try {
    const island = document.getElementById('dashboard-data');
    if (island) { const p = JSON.parse(island.textContent); if (p.csrfToken) return p.csrfToken; }
  } catch (_) {}
  return '';
}

function esc(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function showToast(type, message) {
  if (window.toastManager) {
    window.toastManager.show({ type, message, title: type.charAt(0).toUpperCase() + type.slice(1) });
  } else {
    console.warn(`[${type}] ${message}`);
  }
}

// ── Exports ────────────────────────────────────────────────────────────────
// FIX: Added leaveActiveConversation to the public API
return { preload, init, initSocket, leaveActiveConversation, get initialized() { return _initialized; } };
})();