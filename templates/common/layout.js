'use strict';
// A single flowing document: content decides the height, never the viewport.
// All report fields are inserted as text, never interpreted as HTML.
window.__RENDER_READY__ = false;
window.__RENDER_ERROR__ = null;
(async () => {
  const data = JSON.parse(document.getElementById('report-data').textContent);
  const theme = JSON.parse(document.getElementById('theme-data').textContent);
  const fragment = name => document.getElementById('component-' + name).content.cloneNode(true);
  const field = (root, name, value) => { const node = root.querySelector(`[data-field="${name}"]`); if (node) node.textContent = value ?? ""; };
  const page = document.createElement('section');
  page.className = 'report-page';
  for (const name of theme.layout) page.append(fragment(name));
  field(page, 'report-title', data.title);
  field(page, 'date', data.date);
  field(page, 'intro', data.intro);
  field(page, 'closing-title', data.closing?.title);
  field(page, 'closing-note', data.closing?.note);
  const stack = page.querySelector('.card-stack');
  for (const [index, section] of data.sections.entries()) {
    const node = fragment('card').firstElementChild;
    node.dataset.tone = String(index % 3);
    field(node, 'number', String(index + 1).padStart(2, '0'));
    field(node, 'category', section.label);
    field(node, 'title', section.title);
    field(node, 'body', section.body);
    const slot = node.querySelector('[data-slot="sticker"]');
    if (slot && theme.stickers?.[section.expression] && document.getElementById('component-sticker')) {
      const sticker = fragment('sticker').firstElementChild;
      sticker.src = theme.stickers[section.expression];
      sticker.dataset.expression = section.expression;
      slot.append(sticker);
    }
    stack.append(node);
  }
  document.getElementById('pages').append(page);
  await document.fonts.ready;
  await Promise.all([...document.images].map(img => img.decode()));
  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  window.__RENDER_READY__ = true;
})().catch(error => { window.__RENDER_ERROR__ = error.message; });
