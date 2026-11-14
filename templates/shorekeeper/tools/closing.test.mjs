import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, readFile, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {chooseClosing, readLastClosing, writeLastClosing} from './closing.mjs';

test('paired copy avoids the persisted previous choice across sequential builds', async () => {
  const pool = JSON.parse(await readFile(new URL('../copy/closings.json', import.meta.url), 'utf8'));
  const dir = await mkdtemp(path.join(tmpdir(), 'shorekeeper-closing-'));
  const state = path.join(dir, 'state.json');
  try {
    assert.equal(await readLastClosing(state), undefined);
    for (let i = 0; i < 100; i++) {
      const previous = await readLastClosing(state);
      const chosen = chooseClosing(pool, previous);
      assert.notEqual(chosen.id, previous);
      assert.deepEqual(chosen, pool.find(item => item.id === chosen.id));
      await writeLastClosing(state, chosen.id);
      assert.equal(await readLastClosing(state), chosen.id);
    }
    await writeFile(state, '{broken');
    assert.equal(await readLastClosing(state), undefined);
    assert.ok(chooseClosing(pool, 'removed-from-library'));
    assert.deepEqual(chooseClosing([pool[0]], pool[0].id), pool[0]);
    assert.throws(() => chooseClosing([]));
    assert.throws(() => chooseClosing([pool[0], pool[0]]));
  } finally { await rm(dir, {recursive:true, force:true}); }
});
