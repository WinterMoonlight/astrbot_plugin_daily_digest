import asyncio
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from digest.domain import TaskConfig, parse_time, split_text, split_umo, utcnow
from digest.generation import Generator
from digest.service import Service
from digest.storage import Conflict, Store


def config(**overrides):
    return TaskConfig.from_dict({'name': 'test', 'groups': ['bot:GroupMessage:123'],
        'recipient': 'bot:FriendMessage:456', 'provider_id': 'test', **overrides})


class DomainTests(unittest.TestCase):
    def test_personal_background_is_optional_text(self):
        self.assertEqual(config().personal_background, '')
        config(personal_background='').validate()
        self.assertEqual(config(personal_background='学习委员').to_dict()['personal_background'], '学习委员')
        for value in (None, 1, [], {}):
            with self.assertRaises(ValueError):
                config(personal_background=value)

    def test_daily_weekly_defaults_and_independent_lookback(self):
        self.assertEqual(config().lookback_hours, 24)
        self.assertEqual(config(frequency='weekly').lookback_hours, 168)
        self.assertEqual(config(frequency='weekly', lookback_hours=12).lookback_hours, 12)

    def test_wrong_recipient_and_non_ascii_number(self):
        for value in ('bot:GroupMessage:1', 'bot:FriendMessage:１２', 'bot:FriendMessage:1:2'):
            with self.assertRaises(ValueError):
                config(recipient=value)

    def test_draft_not_enabled_and_numeric_validation(self):
        TaskConfig.from_dict({})
        with self.assertRaises(ValueError):
            TaskConfig().validate()
        for raw in ({'lookback_hours': True}, {'weekdays': []}, {'time': '24:00'}):
            with self.assertRaises(ValueError):
                config(**raw)

    def test_half_open_window_and_next_slot(self):
        planned = parse_time('2026-11-12T08:00:00+08:00')
        start, end = config().window(planned)
        self.assertEqual((end - start).total_seconds(), 86400)
        self.assertEqual(config().next_after(planned), planned + timedelta(days=1))
        weekly = config(frequency='weekly', weekdays=[0])
        self.assertEqual(weekly.next_after(planned), parse_time('2026-11-16T08:00:00+08:00'))

    def test_dst_nonexistent_time_is_skipped(self):
        task = config(timezone='America/New_York', time='02:30')
        self.assertEqual(task.next_after(parse_time('2026-03-08T00:00:00-05:00')),
                         parse_time('2026-03-09T02:30:00-04:00'))

    def test_text_split_preserves_every_character_and_budget(self):
        for text in ('短文', ('事项一\n\n事项二。\n' * 900), '长' * 23000):
            parts = split_text(text, 100)
            self.assertTrue(all(len(p) <= 100 for p in parts))
            rebuilt = ''.join(re.sub(r'^\[\d+/\d+\] ', '', p) for p in parts) if len(parts) > 1 else ''.join(parts)
            self.assertEqual(rebuilt, text)
        self.assertEqual(split_text('  '), [])


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'test.db')
        await self.store.initialize()
        self.task = await self.store.save_task(config())

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_disabled_default_optimistic_conflict(self):
        self.assertFalse(self.task['enabled'])
        await self.store.save_task(config(name='new'), self.task['id'], 1)
        with self.assertRaises(Conflict):
            await self.store.save_task(config(name='stale'), self.task['id'], 1)

    async def test_personal_background_is_saved_in_run_snapshot_and_can_be_cleared(self):
        task = await self.store.save_task(config(personal_background='学习委员'))
        run_id = await self.store.enqueue(task['id'])
        await self.store.save_task(config(personal_background=''), task['id'], task['version'])
        self.assertEqual((await self.store.get_run(run_id))['snapshot']['config']['personal_background'], '学习委员')
        self.assertEqual((await self.store.get_task(task['id']))['config']['personal_background'], '')

    async def test_formal_slot_idempotency_and_snapshot(self):
        task = await self.store.set_enabled(self.task['id'], 1, True)
        at = utcnow()
        first, second = await asyncio.gather(*[
            self.store.enqueue(task['id'], at, 'scheduled') for _ in range(2)])
        self.assertEqual(first, second)
        await self.store.save_task(config(name='changed'), task['id'], task['version'])
        run = await self.store.get_run(first)
        self.assertEqual(run['snapshot']['config']['name'], 'test')

    async def test_claim_respects_same_task_and_recovery(self):
        a = await self.store.enqueue(self.task['id'])
        await self.store.enqueue(self.task['id'])
        claimed = await self.store.claim(set())
        self.assertEqual(claimed['id'], a)
        self.assertIsNone(await self.store.claim({self.task['id']}))
        restarted = Store(self.store.path)
        await restarted.initialize()
        self.assertEqual((await restarted.get_run(a))['status'], 'queued')

    async def test_archive_cancels_pending_and_preserves_run(self):
        run_id = await self.store.enqueue(self.task['id'])
        await self.store.set_enabled(self.task['id'], 1, False, archive=True)
        self.assertEqual((await self.store.get_run(run_id))['status'], 'cancelled')
        self.assertEqual(await self.store.list_tasks(), [])

    async def test_cleanup_keeps_ledger_and_unknown(self):
        run_id = await self.store.enqueue(self.task['id'])
        await self.store.finish(run_id, 'ready', {'summary': 'old'})
        def fixture(db):
            db.execute("UPDATE runs SET updated_at='2000-01-01T00:00:00+00:00'")
            db.execute('INSERT INTO parts VALUES (?,?,?,?,?,?)', (run_id, 'digest', 1, 'old', 'sending', None))
        await self.store.call(fixture)
        await self.store.initialize()
        await self.store.cleanup(30)
        self.assertEqual((await self.store.get_run(run_id))['result']['summary'], 'old')
        self.assertEqual(await self.store.call(lambda db: db.execute('SELECT state FROM parts').fetchone()[0]), 'unknown')


class FakeModels:
    def __init__(self, output=None):
        self.calls = []
        self.output = output

    async def prepare(self, task):
        return {'window': 8192, 'persona': '', 'warnings': []}

    async def generate(self, prepared, prompt, system):
        self.calls.append(prompt)
        if self.output:
            return self.output, 'fake'
        if '"facts"' in prompt[:300]:
            return json.dumps({'facts': [{'text': 'notice', 'sources': ['bot/1']}]}), 'fake'
        return json.dumps({'summary': '重要通知\n测试事项', 'candidates': []}), 'fake'


class FakeHistory:
    def __init__(self, text='fixture'):
        self.text = text

    async def read(self, groups, start, end):
        return ([{'id': 'bot/1', 'group': groups[0], 'time': start.isoformat(), 'text': self.text}],
                [{'group': groups[0], 'count': 1, 'note': '覆盖未知'}])


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_archive_cancels_active_generation(self):
        started = asyncio.Event()
        class Blocked:
            models = FakeModels()
            async def run(self, *args):
                started.set()
                await asyncio.Event().wait()
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root) / 'test.db')
            service = Service(store, Blocked())
            await service.start()
            try:
                task = await store.save_task(config())
                run_id = await service.preview(task['id'])
                await asyncio.wait_for(started.wait(), 3)
                await service.archive(task['id'], task['version'])
                self.assertEqual((await store.get_run(run_id))['status'], 'cancelled')
                self.assertFalse(service.active)
            finally:
                await service.stop()

    async def test_global_concurrency_with_two_tasks(self):
        running = 0
        peak = 0
        release = asyncio.Event()
        class Blocked:
            models = FakeModels()
            async def run(self, *args):
                nonlocal running, peak
                running += 1; peak = max(peak, running)
                try:
                    await release.wait()
                    return {'summary': 'done'}
                finally:
                    running -= 1
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root) / 'test.db')
            service = Service(store, Blocked())
            await service.start()
            try:
                one = await store.save_task(config(name='one'))
                two = await store.save_task(config(name='two'))
                ids = [await service.preview(t['id']) for t in (one, two)]
                async with asyncio.timeout(3):
                    while running == 0:
                        await asyncio.sleep(.01)
                self.assertEqual(peak, 1)
                self.assertEqual(sum([(await store.get_run(i))['status']=='queued' for i in ids]), 1)
                await store.settings({'concurrency': 2})
                service.wake.set()
                async with asyncio.timeout(3):
                    while running < 2:
                        await asyncio.sleep(.01)
                self.assertEqual(peak, 2)
                release.set()
            finally:
                await service.stop()

    async def test_short_and_long_input(self):
        for text, minimum in [('hello', 1), ('长消息' * 4000, 2)]:
            models = FakeModels()
            result = await Generator(models, FakeHistory(text)).run(config(), utcnow(), {'call_budget': 64})
            self.assertGreaterEqual(len(models.calls), minimum)
            self.assertNotIn('覆盖说明', result['summary'])
            self.assertTrue(result['coverage'])
            self.assertEqual(result['logical_calls'], len(models.calls))

    async def test_bad_model_format_and_budget_fail_closed(self):
        for models, budget in [(FakeModels('not-json'), 64), (FakeModels(), 1)]:
            with self.assertRaises((ValueError, RuntimeError)):
                await Generator(models, FakeHistory('文本' * 9000)).run(config(), utcnow(), {'call_budget': budget})

    async def test_unknown_candidate_source_rejected(self):
        output = json.dumps({'summary': 'item', 'candidates': [{'title': 'item',
            'event_at': (utcnow() + timedelta(hours=2)).isoformat(), 'sources': ['invented']}]})
        with self.assertRaises(ValueError):
            await Generator(FakeModels(output), FakeHistory()).run(config(), utcnow(), {'call_budget': 64})

    async def test_preview_queue_is_persistent_and_has_no_sender(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root) / 'test.db')
            service = Service(store, Generator(FakeModels(), FakeHistory()))
            await service.start()
            try:
                task = await store.save_task(config())
                run_id = await service.preview(task['id'])
                async with asyncio.timeout(3):
                    while (await store.get_run(run_id))['status'] not in ('ready', 'failed'):
                        await asyncio.sleep(.01)
                run = await store.get_run(run_id)
                self.assertEqual(run['status'], 'ready', run)
                self.assertEqual(run['kind'], 'preview')
                self.assertFalse(hasattr(service, 'sender'))
            finally:
                await service.stop()


if __name__ == '__main__':
    unittest.main()
