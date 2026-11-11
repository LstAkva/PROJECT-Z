/**
 * ZakoWhat — Unified Global Navigation & Identity Manager
 * Handles responsive navbar, theme toggling, auth status sync, and mobile navigation drawer.
 */

(function() {
  'use strict';

  // 1. Theme Management
  window.toggleZakoWhatTheme = function() {
    const currentTheme = document.documentElement.getAttribute('data-theme') || 'light';
    const nextTheme = currentTheme === 'dark' ? 'light' : 'dark';
    window.applyNavbarTheme(nextTheme);
  };

  window.applyNavbarTheme = function(theme) {
    const validTheme = (theme === 'dark') ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', validTheme);
    try {
      localStorage.setItem('zakowhat_theme', validTheme);
      localStorage.setItem('zakowhat-theme', validTheme);
    } catch(e) {}

    const toggleBtn = document.getElementById('themeToggleBtn');
    if (toggleBtn) {
      if (validTheme === 'light') {
        toggleBtn.setAttribute('aria-label', "Qorong'i rejimga o'tish (Switch to dark theme)");
        toggleBtn.setAttribute('title', "Qorong'i rejimga o'tish");
      } else {
        toggleBtn.setAttribute('aria-label', "Yorug' rejimga o'tish (Switch to light theme)");
        toggleBtn.setAttribute('title', "Yorug' rejimga o'tish");
      }
    }

    const moonIcon = document.getElementById('moonIcon');
    const sunIcon = document.getElementById('sunIcon');
    if (moonIcon && sunIcon) {
      if (validTheme === 'light') {
        // In light theme, show moon icon to allow switching to dark
        moonIcon.style.display = 'block';
        sunIcon.style.display = 'none';
      } else {
        // In dark theme, show sun icon to allow switching to light
        moonIcon.style.display = 'none';
        sunIcon.style.display = 'block';
      }
    }

    try {
      window.dispatchEvent(new CustomEvent('zakowhat-theme-change', { detail: { theme: validTheme } }));
    } catch(e) {}
  };

  // 2. Mobile Drawer Navigation
  window.toggleMobileNav = function() {
    const drawer = document.getElementById('mobileNavDrawer');
    const burger = document.getElementById('hamburgerIcon');
    const closeIcon = document.getElementById('closeMenuIcon');
    if (!drawer) return;

    const isHidden = drawer.classList.contains('hidden');
    if (isHidden) {
      drawer.classList.remove('hidden');
      if (burger) burger.classList.add('hidden');
      if (closeIcon) closeIcon.classList.remove('hidden');
    } else {
      drawer.classList.add('hidden');
      if (burger) burger.classList.remove('hidden');
      if (closeIcon) closeIcon.classList.add('hidden');
    }
  };

  // 3. Navigation Active Link State
  function syncNavbarActiveState() {
    const path = window.location.pathname;
    const search = window.location.search;

    const links = [
      { id: 'nav-link-home', mId: 'mobile-link-home', match: path === '/' },
      { id: 'nav-link-daily', mId: 'mobile-link-daily', match: path.startsWith('/daily') },
      { id: 'nav-link-packs', mId: 'mobile-link-packs', match: path.startsWith('/arena') || path.startsWith('/quiz') },
    ];

    links.forEach(item => {
      const el = document.getElementById(item.id);
      const mEl = document.getElementById(item.mId);
      if (item.match) {
        if (el) {
          el.classList.add('active');
          el.style.color = 'var(--accent-brass)';
          el.style.fontWeight = '700';
        }
        if (mEl) {
          mEl.classList.add('active');
          mEl.style.color = 'var(--accent-brass)';
          mEl.style.fontWeight = '700';
        }
      } else {
        if (el) {
          el.classList.remove('active');
          el.style.color = '';
          el.style.fontWeight = '';
        }
        if (mEl) {
          mEl.classList.remove('active');
          mEl.style.color = '';
          mEl.style.fontWeight = '';
        }
      }
    });
  }

  // 4. Auth State Synchronization
  window.checkNavbarAuth = async function() {
    try {
      const res = await fetch('/api/auth/me');
      if (!res.ok) {
        renderNavbarAuth(false, null);
        return;
      }
      const data = await res.json();
      if (data.authenticated && data.user) {
        renderNavbarAuth(true, data.user);
      } else {
        renderNavbarAuth(false, null);
      }
    } catch(err) {
      renderNavbarAuth(false, null);
    }
  };

  function renderNavbarAuth(isAuth, user) {
    const guestControls = document.getElementById('header-guest-controls');
    const userControls = document.getElementById('header-user-controls');
    const mobileGuest = document.getElementById('mobile-guest-controls');
    const mobileUser = document.getElementById('mobile-user-controls');
    const userNameEl = document.getElementById('header-user-name');
    const mobileUserNameEl = document.getElementById('mobile-user-name');

    if (isAuth && user) {
      if (guestControls) guestControls.classList.add('hidden');
      if (mobileGuest) mobileGuest.classList.add('hidden');

      if (userControls) {
        userControls.classList.remove('hidden');
        userControls.style.display = 'flex';
      }
      if (mobileUser) {
        mobileUser.classList.remove('hidden');
        mobileUser.style.display = 'flex';
      }

      const name = user.display_name || user.email || 'Foydalanuvchi';
      if (userNameEl) userNameEl.innerText = name;
      if (mobileUserNameEl) mobileUserNameEl.innerText = name;

      const avatarImg = document.getElementById('header-user-avatar-img');
      const avatarSvg = document.getElementById('header-user-avatar-svg');
      if (avatarImg && avatarSvg) {
        if (user.avatar_url) {
          avatarImg.src = user.avatar_url;
          avatarImg.classList.remove('hidden');
          avatarSvg.classList.add('hidden');
        } else {
          avatarImg.classList.add('hidden');
          avatarSvg.classList.remove('hidden');
        }
      }
    } else {
      if (guestControls) guestControls.classList.remove('hidden');
      if (mobileGuest) mobileGuest.classList.remove('hidden');

      if (userControls) {
        userControls.classList.add('hidden');
        userControls.style.display = 'none';
      }
      if (mobileUser) {
        mobileUser.classList.add('hidden');
        mobileUser.style.display = 'none';
      }
    }
  }

  // 5. Auth Navigation Handlers
  window.handleNavbarAuth = function(type) {
    if (typeof window.openAuthModal === 'function') {
      window.openAuthModal(type);
    } else {
      window.location.href = `/arena?auth=${type}`;
    }
  };

  window.handleNavbarProfile = function(e) {
    if (typeof window.openProfileScreen === 'function') {
      if (e) e.preventDefault();
      window.openProfileScreen();
    }
  };

  window.handleNavbarPacks = function(e) {
    if (typeof window.goToQuizSelection === 'function') {
      if (e) e.preventDefault();
      window.goToQuizSelection();
    }
  };

  window.handleNavbarLogout = async function() {
    try {
      await fetch('/api/auth/logout', { method: 'POST' });
    } catch(e) {}
    window.location.href = '/';
  };

  // 5b. Password visibility toggle (eye icon) for inputs marked with data-password-toggle.
  //     The input keeps its own classes/styles; only right padding is added so text never runs under the icon.
  var PW_EYE = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>';
  var PW_EYE_OFF = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 3l18 18"/><path d="M10.6 5.1A10.6 10.6 0 0 1 12 5c6.4 0 10 7 10 7a17.6 17.6 0 0 1-3.2 4.1M6.6 6.6C3.9 8.4 2 12 2 12s3.6 7 10 7a9.7 9.7 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>';
  var PW_LABEL_SHOW = 'Parolni ko‘rsatish';
  var PW_LABEL_HIDE = 'Parolni yashirish';

  function ensurePasswordToggleStyle() {
    if (document.getElementById('zw-pw-toggle-style')) return;
    var style = document.createElement('style');
    style.id = 'zw-pw-toggle-style';
    style.textContent =
      '.zw-pw-field{position:relative;display:block;width:100%;}' +
      '.zw-pw-field>input{padding-right:2.75rem !important;}' +
      '.zw-pw-toggle{position:absolute;top:50%;right:0.35rem;transform:translateY(-50%);width:2.25rem;height:2.25rem;' +
      'display:flex;align-items:center;justify-content:center;padding:0;margin:0;border:0;border-radius:6px;' +
      'background:transparent;color:var(--text-muted,#BDB4AE);cursor:pointer;transition:color .15s ease;}' +
      '.zw-pw-toggle:hover{color:var(--accent-brass,#C5A059);}' +
      '.zw-pw-toggle:focus-visible{outline:none;box-shadow:0 0 0 2px rgba(197,160,89,.55);}';
    (document.head || document.documentElement).appendChild(style);
  }

  function enhancePasswordInput(input) {
    if (!input || input.dataset.pwToggleReady === '1') return;
    input.dataset.pwToggleReady = '1';
    ensurePasswordToggleStyle();

    var wrapper = document.createElement('span');
    wrapper.className = 'zw-pw-field';
    input.parentNode.insertBefore(wrapper, input);
    wrapper.appendChild(input);

    var btn = document.createElement('button');
    btn.type = 'button';  // never submits the form
    btn.className = 'zw-pw-toggle';
    btn.setAttribute('aria-label', PW_LABEL_SHOW);
    btn.setAttribute('aria-pressed', 'false');
    btn.setAttribute('aria-controls', input.id || '');
    btn.innerHTML = PW_EYE;
    wrapper.appendChild(btn);

    // Keep focus (and the caret) in the field when the icon is clicked with a mouse/touch.
    btn.addEventListener('mousedown', function(e) { e.preventDefault(); });

    btn.addEventListener('click', function() {
      var hadFocus = document.activeElement === input || document.activeElement === btn;
      var start = input.selectionStart, end = input.selectionEnd;
      var show = input.type === 'password';
      input.type = show ? 'text' : 'password';  // value is untouched
      btn.innerHTML = show ? PW_EYE_OFF : PW_EYE;
      btn.setAttribute('aria-label', show ? PW_LABEL_HIDE : PW_LABEL_SHOW);
      btn.setAttribute('aria-pressed', show ? 'true' : 'false');
      if (hadFocus && document.activeElement !== btn) {
        input.focus();
        try { if (start !== null && end !== null) input.setSelectionRange(start, end); } catch (e) {}
      }
    });
  }

  function initPasswordToggles(root) {
    (root || document).querySelectorAll('input[data-password-toggle]').forEach(enhancePasswordInput);
  }
  window.initPasswordToggles = initPasswordToggles;

  // 6. Initialization on DOMContentLoaded
  document.addEventListener('DOMContentLoaded', function() {
    initPasswordToggles(document);
    let savedTheme = 'light';
    try {
      savedTheme = localStorage.getItem('zakowhat_theme') || localStorage.getItem('zakowhat-theme') || 'light';
    } catch(e) {}
    window.applyNavbarTheme(savedTheme);

    syncNavbarActiveState();
    window.checkNavbarAuth();
  });

})();
