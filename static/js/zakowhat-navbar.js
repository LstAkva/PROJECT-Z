/**
 * ZakoWhat — Unified Global Navigation & Identity Manager
 * Handles responsive navbar, theme toggling, auth status sync, and mobile navigation drawer.
 */

(function() {
  'use strict';

  // 1. Theme Management
  window.toggleZakoWhatTheme = function() {
    const currentTheme = document.documentElement.getAttribute('data-theme') || 'dark';
    const nextTheme = currentTheme === 'dark' ? 'light' : 'dark';
    window.applyNavbarTheme(nextTheme);
  };

  window.applyNavbarTheme = function(theme) {
    const validTheme = (theme === 'light') ? 'light' : 'dark';
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

  // 6. Initialization on DOMContentLoaded
  document.addEventListener('DOMContentLoaded', function() {
    let savedTheme = 'dark';
    try {
      savedTheme = localStorage.getItem('zakowhat_theme') || localStorage.getItem('zakowhat-theme') || 'dark';
    } catch(e) {}
    window.applyNavbarTheme(savedTheme);

    syncNavbarActiveState();
    window.checkNavbarAuth();
  });

})();
