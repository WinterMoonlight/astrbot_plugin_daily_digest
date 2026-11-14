import {readFile, writeFile, mkdir} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {chooseClosing, readLastClosing, writeLastClosing} from './closing.mjs';
import {fontStyles} from './fonts.mjs';

export const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const read = file => readFile(path.join(root, file), 'utf8');
const safeJSON = data => JSON.stringify(data).replace(/</g, '\\u003c').replace(/>/g, '\\u003e');
export async function build(input, output) {
  const data = structuredClone(typeof input === 'string' ? JSON.parse(await readFile(input, 'utf8')) : input);
  for (const key of ['title', 'date', 'intro']) if (typeof data[key] !== 'string') throw new Error(`${key} must be text`);
  if (!Array.isArray(data.sections) || !data.sections.length) throw new Error('sections must not be empty');
  for (const item of data.sections) for (const key of ['title', 'label', 'body']) {
    if (typeof item[key] !== 'string' || !item[key].trim()) throw new Error(`section.${key} must be nonempty text`);
  }
  const theme = JSON.parse(await read('template.json'));
  if (theme.schemaVersion !== 1) throw new Error('Unsupported template schemaVersion');
  const fontAssets = await fontStyles(theme.fonts, root, output);
  for (const warning of fontAssets.warnings) console.warn(warning);
  const closingState = path.join(root, '.cache', 'closing-state.json');
  data.closing = chooseClosing(JSON.parse(await read('copy/closings.json')), await readLastClosing(closingState));
  const asset = async (name, mime) => `data:${mime};base64,${(await readFile(path.join(root, 'assets', name))).toString('base64')}`;
  const character = theme.components.includes('character') ? await asset(theme.assets.character, theme.characterMime || 'image/webp') : '';
  const selected = new Set();
  for (const [index, section] of data.sections.entries()) {
    const requested = theme.stickerAliases?.[section.expression] || section.expression;
    section.expression = Object.hasOwn(theme.stickers, requested) ? requested : theme.stickerCycle[index % theme.stickerCycle.length];
    selected.add(section.expression);
  }
  const renderTheme = {...theme, stickers: Object.fromEntries(await Promise.all([...selected].map(async name => [name, await asset(theme.stickers[name], 'image/png')])))};
  const components = await Promise.all(theme.components.map(async name => {
    const html = (await read(`components/${name}.html`)).replaceAll('{{CHARACTER}}', character);
    return `<template id="component-${name}">${html}</template>`;
  }));
  const replacements = {
    STYLES: (await Promise.all(theme.styles.map(name => read('styles/' + name)))).join('\n') + `\n:root{--page-width:${theme.width}px;}\n` + fontAssets.css,
    COMPONENTS: components.join('\n'), DATA: safeJSON(data), THEME: safeJSON(renderTheme),
    RUNTIME: await readFile(path.join(root, '../common/layout.js'), 'utf8'),
  };
  const html = (await read('template.html')).replace(/\{\{([A-Z]+)\}\}/g, (_, name) => replacements[name]);
  await mkdir(path.dirname(output), {recursive: true});
  await writeFile(output, html, 'utf8');
  await writeLastClosing(closingState, data.closing.id);
  return {data, theme, output};
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const input = path.resolve(process.argv[2] || path.join(root, 'samples/standard.json'));
  const output = path.resolve(process.argv[3] || path.join(root, 'previews/standard.html'));
  await build(input, output);
  console.log(output);
}
