/* Shared presentation behavior for the extension and the standalone export. */
'use strict';
document.addEventListener('DOMContentLoaded', () => {
  const toggle = document.querySelector('.menu-toggle');
  const sidebar = document.querySelector('.side');
  const scrim = document.querySelector('.scrim');
  const search = document.querySelector('#q');
  const tree = document.querySelector('#tree');
  const mobile = window.matchMedia('(max-width: 760px)');
  const setOpen = open => {
    document.body.classList.toggle('sidebar-open', open);
    toggle.setAttribute('aria-expanded', String(open));
    toggle.setAttribute('aria-label', open ? '关闭书签分类' : '打开书签分类');
    sidebar.inert = mobile.matches && !open;
    if (open) requestAnimationFrame(() => search.focus());
  };
  toggle.addEventListener('click', () => setOpen(toggle.getAttribute('aria-expanded') !== 'true'));
  scrim.addEventListener('click', () => { setOpen(false); toggle.focus(); });
  tree.addEventListener('click', e => {
    if (mobile.matches && e.target.closest('[data-id]')) { setOpen(false); toggle.focus(); }
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && document.body.classList.contains('sidebar-open')) {
      setOpen(false); toggle.focus();
    }
    const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName) || document.activeElement.isContentEditable;
    if (e.key === '/' && !typing && mobile.matches) { e.preventDefault(); setOpen(true); }
    if (e.key === 'Tab' && mobile.matches && document.body.classList.contains('sidebar-open')) {
      const targets = [...sidebar.querySelectorAll('input,button,a[href]')];
      const first = targets[0], last = targets[targets.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });
  mobile.addEventListener('change', () => setOpen(false));
  setOpen(false);
  const syncCurrent = () => tree.querySelectorAll('[data-id]').forEach(el => {
    if (el.classList.contains('on')) el.setAttribute('aria-current', 'page');
    else el.removeAttribute('aria-current');
  });
  new MutationObserver(syncCurrent).observe(tree, { childList: true });
  syncCurrent();
});
