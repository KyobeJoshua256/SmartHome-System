/**
 * admin_base.js — EBS SmartHome Admin
 * =====================================
 * Global utilities loaded on every admin page.
 *
 * Responsibilities:
 *   • Auto-dismiss flash messages
 *   • Mark the correct nav-item active based on current URL
 *   • Expose a global `toast(message, type)` helper for any page to use
 *   • Expose `getCsrfToken()` for AJAX POST requests
 *   • Close sidebar on resize back to desktop width
 */

'use strict';

/* ============================================================================
   CSRF TOKEN HELPER
   Reads from the <meta name="csrf-token"> injected by Flask-WTF.
   ============================================================================ */
function getCsrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute('content') : '';
}

/* ============================================================================
   TOAST NOTIFICATION
   Creates a toast and auto-removes it after `duration` ms.
   Usage: toast('Device turned on', 'success')
          toast('Connection failed', 'error')
   ============================================================================ */
function toast(message, type = 'success', duration = 3500) {
  let container = document.getElementById('toastContainer');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toastContainer';
    container.className = 'toast-container';
    document.body.appendChild(container);
  }

  const el = document.createElement('div');
  el.className = `toast toast-${type}`;

  const icon = type === 'success' ? 'fa-circle-check'
             : type === 'error'   ? 'fa-circle-xmark'
             : type === 'warning' ? 'fa-triangle-exclamation'
             :                      'fa-circle-info';

  el.innerHTML = `<i class="fas ${icon}"></i><span>${message}</span>`;
  container.appendChild(el);

  // Slide out then remove
  setTimeout(() => {
    el.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
    el.style.opacity    = '0';
    el.style.transform  = 'translateX(110%)';
    setTimeout(() => el.remove(), 320);
  }, duration);
}

/* ============================================================================
   AUTO-DISMISS FLASH MESSAGES
   Each flash message fades out after 5 s.
   ============================================================================ */
function initFlashDismiss() {
  document.querySelectorAll('.flash').forEach((el, i) => {
    setTimeout(() => {
      el.style.transition = 'opacity 0.4s ease, transform 0.4s ease, max-height 0.4s ease';
      el.style.opacity    = '0';
      el.style.transform  = 'translateY(-6px)';
      el.style.maxHeight  = '0';
      el.style.padding    = '0';
      el.style.margin     = '0';
      setTimeout(() => el.remove(), 420);
    }, 5000 + i * 400);
  });
}

/* ============================================================================
   ACTIVE NAV ITEM
   Flask sets active via Jinja `request.endpoint`, but we also do a client-side
   pass so any edge-case route still gets highlighted correctly.
   ============================================================================ */
function initActiveNav() {
  const path = window.location.pathname;
  document.querySelectorAll('.nav-item').forEach(link => {
    const href = link.getAttribute('href');
    if (!href || href === '#') return;
    // Mark active if current path starts with the link's path
    // (covers /admin/user/, /admin/user/edit/3, etc.)
    if (path.startsWith(href) && href !== '/') {
      link.classList.add('active');
    }
  });
}

/* ============================================================================
   SIDEBAR RESIZE GUARD
   When the viewport grows back to desktop width, force-close the mobile
   sidebar so the overlay doesn't linger.
   ============================================================================ */
function initResizeGuard() {
  let prevWidth = window.innerWidth;
  window.addEventListener('resize', () => {
    const w = window.innerWidth;
    if (prevWidth <= 1024 && w > 1024) {
      // Crossed from mobile → desktop
      if (typeof closeSidebar === 'function') closeSidebar();
    }
    prevWidth = w;
  });
}

/* ============================================================================
   BOOT
   ============================================================================ */
document.addEventListener('DOMContentLoaded', () => {
  initFlashDismiss();
  initActiveNav();
  initResizeGuard();
});