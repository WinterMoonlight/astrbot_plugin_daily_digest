// Shared help bubble: hover, keyboard focus and touch/click use the same text.
export function initHelp(doc = document, win = window) {
  const tip = doc.getElementById('help-tooltip');
  let anchor = null, pinned = false, timer;

  function hide() {
    win.clearTimeout(timer);
    anchor?.removeAttribute('aria-describedby');
    anchor = null;
    pinned = false;
    tip.hidden = true;
  }

  function position() {
    if (!anchor) return;
    const rect = anchor.getBoundingClientRect();
    if (rect.bottom < 0 || rect.top > win.innerHeight) { hide(); return; }
    const bounds = tip.getBoundingClientRect();
    const viewportWidth = doc.documentElement.clientWidth || win.innerWidth;
    const left = Math.max(12, Math.min(rect.left - 12, viewportWidth - bounds.width - 12));
    const above = rect.top - bounds.height - 10;
    const top = above >= 12 ? above : Math.min(rect.bottom + 10, win.innerHeight - bounds.height - 12);
    tip.style.left = `${left}px`;
    tip.style.top = `${Math.max(12, top)}px`;
  }

  function show(button) {
    win.clearTimeout(timer);
    if (anchor !== button) {
      anchor?.removeAttribute('aria-describedby');
      pinned = false;
    }
    anchor = button;
    // Dialogs are in the top layer, so their help bubbles must be inside them too.
    (button.closest('dialog') || doc.body).append(tip);
    tip.textContent = button.dataset.help;
    button.setAttribute('aria-describedby', 'help-tooltip');
    tip.hidden = false;
    position();
  }

  function deferHide() {
    win.clearTimeout(timer);
    timer = win.setTimeout(() => {
      if (!pinned && doc.activeElement !== anchor && !tip.matches(':hover')) hide();
    }, 140);
  }

  doc.addEventListener('pointerover', event => {
    const button = event.target.closest?.('button[data-help]');
    if (button) show(button);
  });
  doc.addEventListener('pointerout', event => {
    if (event.target.closest?.('button[data-help]') || tip.contains(event.target)) deferHide();
  });
  doc.addEventListener('focusin', event => {
    if (event.target.matches?.('button[data-help]')) show(event.target);
  });
  doc.addEventListener('focusout', event => {
    if (event.target === anchor) { pinned = false; deferHide(); }
  });
  doc.addEventListener('click', event => {
    const button = event.target.closest?.('button[data-help]');
    if (button) {
      event.preventDefault();
      if (anchor === button && pinned) hide();
      else { show(button); pinned = true; }
    } else if (!tip.contains(event.target)) hide();
  });
  doc.addEventListener('keydown', event => {
    if (event.key === 'Escape' && anchor) {
      event.preventDefault();
      event.stopPropagation();
      hide();
    }
  }, true);
  doc.addEventListener('scroll', position, true);
  win.addEventListener('resize', position);
  for (const dialog of doc.querySelectorAll('dialog')) dialog.addEventListener('close', hide);
  return {hide};
}
