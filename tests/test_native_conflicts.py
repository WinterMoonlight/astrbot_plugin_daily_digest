import copy
import unittest
from datetime import timedelta
from types import SimpleNamespace

from digest.deletion import TaskDeletion
from digest.domain import parse_time, utcnow
from digest.workflow_store import entities, entity, uid
import test_native_reminders as fixtures


class ConflictTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    formal = fixtures.NativeTests.formal
    candidate = fixtures.NativeTests.candidate
    drain = fixtures.NativeTests.drain
    active_list = fixtures.NativeTests.active_list
    select = fixtures.NativeTests.select
    reminders = fixtures.NativeTests.reminders

    async def asyncSetUp(self):
        await fixtures.NativeTests.asyncSetUp(self)
        self.item = self.candidate()
        self.item['place'] = ''
        self.list_id = await self.active_list(candidate=self.item)
        self.when = parse_time(self.item['event_at']) - timedelta(minutes=30)
        self.recipient = self.task['config']['recipient']

    async def external(self, name=None, when=None, recipient=None, enabled=True, payload=None):
        return await self.manager.add_active_job(
            name=name or self.item['title'], cron_expression=None, timezone='Asia/Shanghai',
            description='既有原生任务说明', enabled=enabled, persistent=True, run_once=True,
            run_at=when or self.when,
            payload={'session':recipient or self.recipient, 'sender_id':'456',
                     'note':'提醒我参加'+(name or self.item['title']), 'origin':'tool', **(payload or {})})

    async def pending(self):
        return await self.store.call(lambda db: [item for item in entities(db,'change')
            if item['state']=='pending' and item['proposal']['action']=='native_conflict'])

    async def resolve(self, choice, job_id=None):
        change = (await self.pending())[0]
        values = {'native_choice':choice}
        if job_id:
            values['native_job_id'] = job_id
        return await self.interactions.manage_change(change['id'], True, values)

    async def test_identical_native_reminder_reused_without_rewriting_job(self):
        job = await self.external()
        original = copy.deepcopy(vars(job))
        await self.select(self.list_id)
        reminder = (await self.reminders())[0]
        self.assertEqual(reminder['native_job_id'], job.job_id)
        self.assertEqual(reminder['native_link_mode'], 'reused')
        self.assertEqual(self.manager.calls, 1)
        self.assertEqual(vars(job), original)
        self.assertFalse(await self.pending())
        await self.native.sync()
        self.assertEqual(self.manager.calls, 1)

    async def test_utc_equivalent_time_and_normalized_title_match(self):
        job = await self.external(name='日程提醒：' + self.item['title'],
                                  payload={'note':'提醒我参加'+self.item['title']})
        job.payload['run_at'] = self.when.astimezone().isoformat()
        await self.select(self.list_id)
        self.assertEqual((await self.reminders())[0]['native_job_id'], job.job_id)
        self.assertEqual(self.manager.calls, 1)

    async def test_other_recipient_is_never_reused(self):
        await self.external(recipient='bot:FriendMessage:999')
        await self.select(self.list_id)
        self.assertEqual(self.manager.calls, 2)
        self.assertNotIn('native_link_mode', (await self.reminders())[0])

    async def test_matching_name_with_different_task_purpose_needs_confirmation(self):
        await self.external(payload={'note':'搜索课程说明会相关资料并生成报告'})
        await self.select(self.list_id)
        self.assertTrue(await self.pending())
        self.assertEqual(self.manager.calls, 1)

    async def test_schema_upgrade_preserves_data_and_future_version_is_rejected(self):
        await self.store.call(lambda db: db.execute('DELETE FROM schema_version WHERE version>=3'))
        await self.store.initialize()
        version = await self.store.call(lambda db: db.execute('SELECT MAX(version) FROM schema_version').fetchone()[0])
        self.assertEqual(version, 4)
        self.assertEqual((await self.store.get_task(self.task['id']))['id'], self.task['id'])
        await self.store.call(lambda db: db.execute('INSERT INTO schema_version VALUES (5)'))
        with self.assertRaisesRegex(RuntimeError, '拒绝降级'):
            await self.store.initialize()

    async def test_same_name_at_different_time_does_not_block(self):
        await self.external(when=self.when - timedelta(hours=1))
        await self.select(self.list_id)
        self.assertEqual(self.manager.calls, 2)

    async def test_different_item_same_time_asks_once_and_can_create_explicitly(self):
        await self.external(name='另一场活动')
        await self.select(self.list_id)
        self.assertEqual(self.manager.calls, 1)
        self.assertEqual((await self.reminders())[0]['state'], 'awaiting_native_choice')
        first = (await self.pending())[0]['id']
        await self.native.sync()
        await self.native.sync()
        self.assertEqual([item['id'] for item in await self.pending()], [first])
        await self.resolve('create')
        self.assertEqual(self.manager.calls, 2)
        self.assertEqual((await self.reminders())[0]['state'], 'scheduled')

    async def test_event_time_match_with_different_lead_asks_instead_of_silently_reusing(self):
        job = await self.external(when=parse_time(self.item['event_at']))
        await self.select(self.list_id)
        self.assertEqual(self.manager.calls, 1)
        change = (await self.pending())[0]
        self.assertEqual(change['proposal']['candidates'][0]['id'], job.job_id)
        self.assertFalse(change['proposal']['candidates'][0]['same'])
        await self.resolve('reuse', job.job_id)
        reminder = (await self.reminders())[0]
        self.assertEqual(reminder['remind_at'], job.payload['run_at'])
        self.assertEqual(self.manager.calls, 1)

    async def test_known_event_metadata_finds_existing_different_reminder_time(self):
        job = await self.external(when=self.when - timedelta(minutes=30), payload={
            'digest_event':{**self.item, 'timezone':'Asia/Shanghai'}})
        await self.select(self.list_id)
        candidate = (await self.pending())[0]['proposal']['candidates'][0]
        self.assertEqual(candidate['at'], job.payload['run_at'])
        self.assertEqual(self.manager.calls, 1)

    async def test_location_mismatch_is_not_automatic_reuse(self):
        await self.external(payload={'digest_event':{**self.item, 'place':'另一教室'}})
        await self.select(self.list_id)
        self.assertTrue(await self.pending())
        self.assertEqual(self.manager.calls, 1)

    async def test_paused_existing_job_requires_choice_and_stays_paused(self):
        job = await self.external(enabled=False)
        await self.select(self.list_id)
        self.assertTrue(await self.pending())
        await self.resolve('reuse', job.job_id)
        self.assertFalse(job.enabled)
        self.assertEqual((await self.reminders())[0]['state'], 'native_paused')
        self.assertEqual(self.manager.calls, 1)

    async def test_multiple_same_items_require_an_explicit_target(self):
        one = await self.external()
        two = await self.external()
        await self.select(self.list_id)
        change = (await self.pending())[0]
        with self.assertRaises(ValueError):
            await self.interactions.manage_change(change['id'], True, {'native_choice':'reuse'})
        await self.resolve('reuse', two.job_id)
        self.assertEqual((await self.reminders())[0]['native_job_id'], two.job_id)
        self.assertIn(one.job_id, self.manager.jobs)
        self.assertEqual(self.manager.calls, 2)

    async def test_reject_does_not_delete_existing_native_job(self):
        job = await self.external(name='另一个事项')
        await self.select(self.list_id)
        change = (await self.pending())[0]
        await self.interactions.manage_change(change['id'], False)
        self.assertEqual((await self.reminders())[0]['state'], 'cancelled')
        self.assertIn(job.job_id, self.manager.jobs)
        self.assertEqual(self.manager.calls, 1)

    async def test_plain_confirmation_is_not_permission_to_create(self):
        await self.external(name='未知事项')
        await self.select(self.list_id)
        change = (await self.pending())[0]
        with self.assertRaisesRegex(ValueError, '明确选择'):
            await self.interactions.manage_change(change['id'], True)
        self.assertEqual(self.manager.calls, 1)

    async def test_changed_candidate_invalidates_old_confirmation(self):
        job = await self.external(name='未知事项')
        await self.select(self.list_id)
        old = (await self.pending())[0]
        job.name = '任务内容已被另一个操作修改'
        await self.interactions.manage_change(old['id'], True, {'native_choice':'create'})
        self.assertEqual(self.manager.calls, 1)
        current = (await self.pending())[0]
        self.assertNotEqual(current['id'], old['id'])
        await self.resolve('create')
        self.assertEqual(self.manager.calls, 2)

    async def test_race_after_user_choice_rechecks_latest_jobs(self):
        await self.external(name='未知事项')
        await self.select(self.list_id)
        change = (await self.pending())[0]
        await self.store.call(lambda db: self.native.resolve_conflict(db, change,
            {'action':'confirm','native_choice':'create'}))
        await self.external(name='刚刚新增的另一任务')
        await self.native.sync()
        self.assertEqual(self.manager.calls, 2)  # only the two pre-existing jobs
        self.assertTrue(await self.pending())
        self.assertEqual((await self.reminders())[0]['state'], 'awaiting_native_choice')

    async def test_last_moment_list_failure_does_not_create_without_checking(self):
        original = self.manager.list_jobs
        calls = 0
        async def fail_last_read():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError('simulated list failure')
            return await original()
        self.manager.list_jobs = fail_last_read
        await self.select(self.list_id)
        self.assertEqual(self.manager.calls, 0)
        reminder = (await self.reminders())[0]
        self.assertIn('native_error', reminder)
        self.assertNotIn('native_job_id', reminder)

    async def test_last_moment_read_detects_a_new_external_job(self):
        original = self.manager.list_jobs
        calls = 0
        async def insert_before_last_read():
            nonlocal calls
            calls += 1
            if calls == 3:
                await self.external()
            return await original()
        self.manager.list_jobs = insert_before_last_read
        await self.select(self.list_id)
        self.assertEqual(self.manager.calls, 1)
        self.assertEqual((await self.reminders())[0]['native_link_mode'], 'reused')

    async def test_reused_job_is_read_only_and_not_deleted_with_digest(self):
        job = await self.external()
        await self.select(self.list_id)
        reminder = (await self.reminders())[0]
        with self.assertRaisesRegex(ValueError, '复用了'):
            await self.interactions.manage(reminder['id'], 'cancel', reminder['version'])
        await self.store.set_enabled(self.task['id'], self.task['version'], False, archive=True)
        deletion = TaskDeletion(self.store, SimpleNamespace(active={}), self.native)
        preview = await deletion.preview(self.task['id'])
        self.assertEqual(preview['reused_reminders'], 1)
        self.assertEqual(preview['cancellable_unique_reminders'], 0)
        result = await deletion.delete(self.task['id'], preview['version'], preview['token'], True, '彻底删除')
        self.assertEqual(result['cancelled_unique'], 0)
        self.assertIn(job.job_id, self.manager.jobs)

    async def test_recurring_task_is_checked_at_target_occurrence(self):
        job = await self.external()
        job.run_once = False
        job.cron_expression = '* * * * *'
        job.payload.pop('run_at')
        class Trigger:
            def get_next_fire_time(self, previous, target):
                return target
        self.manager._build_trigger = lambda job: Trigger()
        await self.select(self.list_id)
        self.assertTrue(await self.pending())
        await self.resolve('reuse', job.job_id)
        self.assertFalse(job.run_once)
        self.assertEqual(job.cron_expression, '* * * * *')
        self.assertEqual(self.manager.calls, 1)

    async def test_natural_language_conflict_reply_uses_existing_dialogue_window(self):
        job = await self.external(name='可能相同的活动')
        await self.select(self.list_id)
        await self.drain()
        change = (await self.pending())[0]
        self.parser.intent = {'action':'confirm','change_id':change['id'],
                              'native_choice':'reuse','native_job_id':job.job_id}
        self.assertTrue(await self.interactions.handle(self.recipient, uid(), '复用第一个'))
        self.assertEqual((await self.reminders())[0]['native_job_id'], job.job_id)
        self.assertEqual(self.manager.calls, 1)

    async def test_deleting_task_with_unconfirmed_choice_never_creates_later(self):
        await self.external(name='未知事项')
        await self.select(self.list_id)
        await self.store.set_enabled(self.task['id'], self.task['version'], False, archive=True)
        deletion = TaskDeletion(self.store, SimpleNamespace(active={}), self.native)
        preview = await deletion.preview(self.task['id'])
        await deletion.delete(self.task['id'], preview['version'], preview['token'], False, '彻底删除')
        await self.native.sync()
        self.assertEqual(self.manager.calls, 1)
        self.assertEqual((await self.reminders())[0]['state'], 'cancelled')

    async def test_mixed_pending_and_created_items_report_independent_results(self):
        await self.external(name='不同事项')
        second = self.candidate(hours=6,title='独立的新事项')
        ident = await self.formal(candidates=[self.item,second])
        await self.drain()
        self.parser.intent={'action':'select','list_id':ident,'indices':[1,2]}
        message_id=uid()
        await self.interactions.handle(self.recipient,message_id,f'清单 {ident[:8]} 加入1和2')
        answer=await self.store.call(lambda db:db.execute(
            'SELECT result FROM interactions WHERE recipient=? AND message_id=?',
            (self.recipient,message_id)).fetchone()[0])
        self.assertIn(self.item['title']+'：发现相同时间',answer)
        self.assertIn('已添加 独立的新事项',answer)
        self.assertEqual(self.manager.calls,2)

    async def test_mixed_reused_and_created_items_preserve_both_results(self):
        await self.external()
        second = self.candidate(hours=6,title='独立的新事项')
        ident = await self.formal(candidates=[self.item,second])
        await self.drain()
        self.parser.intent={'action':'select','list_id':ident,'indices':[1,2]}
        message_id=uid()
        await self.interactions.handle(self.recipient,message_id,f'清单 {ident[:8]} 加入1和2')
        answer=await self.store.call(lambda db:db.execute(
            'SELECT result FROM interactions WHERE recipient=? AND message_id=?',
            (self.recipient,message_id)).fetchone()[0])
        self.assertIn(self.item['title']+'：已复用',answer)
        self.assertIn('已添加 独立的新事项',answer)
        self.assertEqual(self.manager.calls,2)
