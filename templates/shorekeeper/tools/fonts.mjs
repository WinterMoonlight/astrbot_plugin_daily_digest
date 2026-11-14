import {access} from 'node:fs/promises';
import path from 'node:path';

// Declarative, template-relative font assets; no generated font bytes enter HTML.
export async function fontStyles(fonts, root, output) {
  if (!fonts) return {css:'', warnings:[]};
  const css = [], warnings = [], families = new Set();
  for (const face of fonts.faces) {
    if (!['private', 'bundled'].includes(face.distribution) || (face.distribution === 'private' && !face.file?.startsWith('assets/fonts/private/'))) throw new Error('Private fonts must stay in assets/fonts/private/');
    if (!/^[A-Za-z][A-Za-z0-9 -]*$/.test(face.family) || !Number.isInteger(face.weight) || face.weight < 100 || face.weight > 900) throw new Error('Invalid font family or weight');
    if (typeof face.file !== 'string' || !face.file.startsWith('assets/fonts/') || face.file.includes('\\') || face.file.split('/').includes('..') || !/\.(ttf|otf|woff2?)$/i.test(face.file)) throw new Error('Font must be a template-relative font file');
    families.add(face.family);
    const file = path.join(root, face.file);
    try { await access(file); } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      warnings.push(`Missing ${face.file}; use configured fallback fonts`);
      continue;
    }
    const url = path.relative(path.dirname(output), file).split(path.sep).map(encodeURIComponent).join('/');
    css.push(`@font-face{font-family:'${face.family}';src:url('${url}');font-weight:${face.weight};font-style:normal;font-display:block;}`);
  }
  for (const [role, value] of Object.entries(fonts.roles)) {
    if (!['body','display','latin'].includes(role) || !families.has(value.family) || !Array.isArray(value.fallback) || !value.fallback.every(name => /^[A-Za-z][A-Za-z0-9 -]*$/.test(name))) throw new Error('Invalid font role');
    const names = [value.family, ...value.fallback].map(name => ['serif','sans-serif','monospace'].includes(name) ? name : `'${name}'`);
    css.push(`:root{--${role}-font:${names.join(',')};}`);
  }
  return {css:css.join('\n'), warnings};
}
