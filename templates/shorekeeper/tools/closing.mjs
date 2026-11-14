import {randomInt, randomUUID} from 'node:crypto';
import {readFile, writeFile, mkdir, rename} from 'node:fs/promises';
import path from 'node:path';

export function chooseClosing(pool, lastId) {
  if (!Array.isArray(pool) || !pool.length) throw new Error('Closing library must not be empty');
  const ids = new Set();
  for (const item of pool) {
    if (!item || ['id', 'title', 'note'].some(key => typeof item[key] !== 'string' || !item[key].trim()) || ids.has(item.id)) {
      throw new Error('Each closing needs a unique id and nonempty title/note');
    }
    ids.add(item.id);
  }
  const alternatives = pool.filter(item => item.id !== lastId);
  const candidates = alternatives.length ? alternatives : pool;
  return candidates[randomInt(candidates.length)];
}

export async function readLastClosing(file) {
  try { return JSON.parse(await readFile(file, 'utf8')).lastId; }
  catch (error) {
    if (error.code === 'ENOENT' || error instanceof SyntaxError) return undefined;
    throw error;
  }
}

export async function writeLastClosing(file, lastId) {
  await mkdir(path.dirname(file), {recursive: true});
  const temp = file + '.' + randomUUID() + '.tmp';
  await writeFile(temp, JSON.stringify({lastId}) + '\n', 'utf8');
  await rename(temp, file);
}
