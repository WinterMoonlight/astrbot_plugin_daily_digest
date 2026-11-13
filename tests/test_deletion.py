import unittest
from dataclasses import replace
from types import SimpleNamespace

from digest.deletion import TaskDeletion
from digest.workflow_store import entities, entity, outgoing, put, uid
from digest.domain import utcnow
from test_core import config
import test_native_reminders as fixtures


class DeletionTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.NativeTests.asyncTearDown
    formal = fixtures.NativeTests.formal
    candidate = fixtures.NativeTests.candidate
    drain = fixtures.NativeTests.drain
    active_list = fixtures.NativeTests.active_list
    select = fixtures.NativeTests.select
    reminders = fixtures.NativeTests.reminders
    create = fixtures.NativeTests.create

    async def asyncSetUp(self):
        await fixtures.NativeTests.asyncSetUp(self)
        self.deletion = TaskDeletion(self.store, SimpleNamespace(active={}), self.native)

    async def archived(self, task=None):
        task = await self.store.get_task((task or self.task)['id'])
        return await self.store.set_enabled(task['id'], task['version'], False, archive=True)

    async def remove(self, task=None, cancel=False, preview=None):
        task = task or self.task
        p = preview or await self.deletion.preview(task['id'])
        return await self.deletion.delete(task['id'], p['version'], p['token'], cancel, '彻底删除')

    async def test_default_purges_task_content_keeps_reminder_and_old_reply_guard(self):
        r = await self.create()
        list_id = r['list_ids'][0]
        await self.drain()
        await self.archived()
        p = await self.deletion.preview(self.task['id'])
        self.assertEqual((p['unique_reminders'], p['shared_reminders']), (1, 0))
        self.assertFalse(p['cancel_unique'])
        await self.remove(preview=p)
        self.assertEqual(await self.store.list_tasks(True), [])
        def remaining(db):
            return [db.execute('SELECT count(*) FROM '+table).fetchone()[0]
                    for table in ('runs','parts','delivery')]
        self.assertEqual(await self.store.call(remaining), [0,0,0])
        self.assertEqual(await self.store.call(lambda db:entities(db,'list')), [])
        self.assertEqual(len(self.manager.jobs), 1)
        orphan = (await self.reminders())[0]
        self.assertEqual(orphan['task_ids'], [])
        self.assertEqual(orphan['task_id'], '')
        self.assertEqual(orphan['list_ids'], [])
        self.assertEqual(set(orphan['interaction_config']), {'provider_id','persona_id','recipient','timezone'})
        rows = await self.store.call(lambda db:list(db.execute('SELECT body FROM outbox')))
        self.assertTrue(all(row['body'] is None for row in rows))
        calls = self.parser.calls
        await self.interactions.handle(r['recipient'], 'stale-list-reply', f'清单 {list_id[:8]} 加入第1项')
        self.assertEqual(self.parser.calls, calls)
        self.assertEqual(self.manager.calls, 1)
        self.parser.intent={'action':'modify','reminder_id':r['id'],'lead_minutes':10}
        await self.interactions.handle(r['recipient'], 'edit-orphan', f"修改 {r['id'][:8]} 提前10分钟")
        self.assertEqual(len(self.manager.jobs),1)
        self.assertEqual((await self.reminders())[0]['lead_minutes'],10)

    async def test_cancel_unique_deletes_native_and_scrubs_content(self):
        r=await self.create()
        await self.archived()
        p=await self.deletion.preview(self.task['id'])
        result=await self.remove(cancel=True,preview=p)
        self.assertEqual(result['cancelled_unique'],1)
        self.assertEqual(self.manager.jobs,{})
        self.assertEqual(await self.reminders(),[])
        await self.native.sync()
        self.assertEqual(self.manager.calls,1)
        again=await self.remove(cancel=True,preview=p)
        self.assertTrue(again['already_deleted'])
        self.assertEqual(await self.store.call(lambda db:entities(db,'deleted_reminder')),
                         [{'id':r['id'],'recipient':r['recipient']}])

    async def test_shared_reminder_transfers_owner_and_preserves_other_task(self):
        r=await self.create()
        candidate=await self.store.call(lambda db:entity(db,'list',r['list_ids'][0])['items'][0])
        second=await self.store.save_task(replace(config(),name='other task'))
        second=await self.store.set_enabled(second['id'],second['version'],True)
        list2=await self.active_list(candidate=candidate,task=second)
        await self.select(list2)
        await self.archived()
        p=await self.deletion.preview(self.task['id'])
        self.assertEqual((p['unique_reminders'],p['shared_reminders']),(0,1))
        await self.remove(cancel=True,preview=p)
        shared=(await self.reminders())[0]
        self.assertEqual(shared['task_id'],second['id'])
        self.assertEqual(shared['task_ids'],[second['id']])
        self.assertEqual(shared['list_ids'],[list2])
        self.assertEqual(len(self.manager.jobs),1)
        self.assertEqual(len(await self.store.list_runs(second['id'])),1)
        self.assertTrue((await self.store.get_task(second['id']))['enabled'])

    async def test_cancel_failure_is_resumable_and_never_recreates_deleted_jobs(self):
        list_id=await self.formal(candidates=[self.candidate(title='one'),self.candidate(hours=4,title='two')])
        await self.drain()
        self.parser.intent={'action':'select','list_id':list_id,'indices':[1,2]}
        await self.interactions.handle(config().recipient,uid(),f'清单 {list_id[:8]} 加入1和2')
        self.assertEqual(len(self.manager.jobs),2)
        await self.archived()
        original=self.manager.delete_job
        calls=[]
        async def flaky(ident):
            calls.append(ident)
            if len(calls)==2:
                raise TimeoutError('native deletion interrupted')
            await original(ident)
        self.manager.delete_job=flaky
        with self.assertRaisesRegex(ValueError,'数据仍保留'):
            await self.remove(cancel=True)
        self.assertEqual(len(self.manager.jobs),1)
        self.assertTrue(await self.store.get_task(self.task['id']))
        await self.native.sync()
        self.assertEqual(self.manager.calls,2)
        p=await self.deletion.preview(self.task['id'])
        self.assertTrue(p['resuming'])
        self.assertTrue(p['cancel_unique'])
        with self.assertRaisesRegex(ValueError,'沿用原'):
            await self.remove(preview=p,cancel=False)
        # Construct a fresh coordinator to simulate a plugin restart after partial native cancellation.
        self.deletion=TaskDeletion(self.store,SimpleNamespace(active={}),self.native)
        await self.remove(preview=p,cancel=True)
        self.assertEqual(self.manager.jobs,{})
        self.assertEqual(await self.reminders(),[])

    async def test_confirmation_archival_and_stale_version_enforced(self):
        p=await self.deletion.preview(self.task['id'])
        with self.assertRaisesRegex(ValueError,'输入'):
            await self.deletion.delete(p['id'],p['version'],p['token'],False,'yes')
        with self.assertRaisesRegex(ValueError,'先归档'):
            await self.remove(preview=p)
        await self.archived()
        with self.assertRaisesRegex(ValueError,'已变化'):
            await self.remove(preview=p)
        self.assertTrue(await self.store.get_task(self.task['id']))

    async def test_changed_reminder_invalidates_confirmation(self):
        r=await self.create()
        await self.archived()
        p=await self.deletion.preview(self.task['id'])
        await self.interactions.manage(r['id'],'modify',r['version'],{'lead_minutes':5})
        with self.assertRaisesRegex(ValueError,'已变化'):
            await self.remove(preview=p)

    async def test_native_page_edit_after_preview_invalidates_confirmation(self):
        r=await self.create()
        await self.archived()
        p=await self.deletion.preview(self.task['id'])
        self.manager.jobs[r['native_job_id']].enabled=False
        with self.assertRaisesRegex(ValueError,'已变化'):
            await self.remove(preview=p,cancel=True)
        self.assertEqual(len(self.manager.jobs),1)

    async def test_inflight_send_blocks_deletion(self):
        await self.formal()
        await self.archived()
        await self.store.call(lambda db:db.execute("UPDATE outbox SET state='sending'"))
        p=await self.deletion.preview(self.task['id'])
        self.assertTrue(p['busy'])
        with self.assertRaisesRegex(ValueError,'正在进行'):
            await self.remove(preview=p)
        self.assertTrue(await self.store.get_task(self.task['id']))

    async def test_processing_interaction_blocks_deletion(self):
        await self.archived()
        await self.store.call(lambda db:db.execute(
            "INSERT INTO interactions VALUES (?,?,'processing',NULL,?)",(config().recipient,'inflight',utcnow().isoformat())))
        with self.assertRaisesRegex(ValueError,'正在进行'):
            await self.remove()

    async def test_legacy_reply_text_cleared_but_other_recipient_and_tagged_task_preserved(self):
        second=await self.store.save_task(replace(config(),name='other task'))
        def seed(db):
            for message_id,recipient in [('legacy',config().recipient),('other-task',config().recipient),('other-user','bot:FriendMessage:987')]:
                db.execute('INSERT INTO interactions VALUES (?,?,?,?,?)',(recipient,message_id,'done','reply-'+message_id,utcnow().isoformat()))
                outgoing(db,recipient+'/'+message_id,'reply',recipient,'reply-'+message_id)
            put(db,'interaction_refs',{'id':config().recipient+'/other-task','recipient':config().recipient,'task_ids':[second['id']]})
        await self.store.call(seed)
        await self.archived()
        await self.remove()
        result=await self.store.call(lambda db:{r['message_id']:r['result'] for r in db.execute('SELECT * FROM interactions')})
        self.assertIsNone(result['legacy'])
        self.assertEqual(result['other-task'],'reply-other-task')
        self.assertEqual(result['other-user'],'reply-other-user')
        self.assertEqual(len(result),3)

    async def test_unowned_native_job_never_deleted(self):
        r=await self.create()
        self.manager.jobs[r['native_job_id']].payload['digest_reminder_owner']='someone else'
        await self.archived()
        with self.assertRaisesRegex(ValueError,'数据仍保留'):
            await self.remove(cancel=True)
        self.assertEqual(len(self.manager.jobs),1)
        self.assertTrue(await self.store.get_task(self.task['id']))

    async def test_lost_delete_response_can_resume_without_false_recreation(self):
        await self.create()
        await self.archived()
        original=self.manager.delete_job
        async def lost(ident):
            await original(ident)
            raise TimeoutError('response lost after success')
        self.manager.delete_job=lost
        with self.assertRaisesRegex(ValueError,'数据仍保留'):
            await self.remove(cancel=True)
        self.assertEqual(self.manager.jobs,{})
        await self.native.sync()
        await self.remove(cancel=True)
        self.assertEqual(self.manager.calls,1)
        self.assertEqual(await self.store.list_tasks(True),[])

    async def test_unconfirmed_create_cannot_be_claimed_cancelled(self):
        self.manager.lose_response=True
        await self.create()
        self.manager.jobs.clear()
        await self.archived()
        with self.assertRaisesRegex(ValueError,'尚未确认取消'):
            await self.remove(cancel=True)
        self.assertTrue(await self.store.get_task(self.task['id']))

    async def test_draft_delete_and_late_freeze_are_safe(self):
        await self.archived()
        await self.remove()
        await self.runtime.freeze('a-run-that-was-deleted')
        self.assertEqual(await self.store.list_tasks(True),[])

    async def test_deleted_change_quote_never_confirms_another_change(self):
        r=await self.create()
        other=await self.store.save_task(replace(config(),name='other task'))
        old_change,new_change=uid(),uid()
        def seed(db):
            for ident,task_id in [(old_change,self.task['id']),(new_change,other['id'])]:
                put(db,'change',{'id':ident,'task_id':task_id,'recipient':r['recipient'],
                    'state':'pending','proposal':{'action':'cancel','reminder_id':r['id'],'version':r['version']}})
            outgoing(db,old_change,'change',r['recipient'],'old proposal',task_id=self.task['id'])
            db.execute("UPDATE outbox SET state='acknowledged',receipt='old-change-quote' WHERE owner=?",(old_change,))
        await self.store.call(seed)
        await self.archived()
        await self.remove()
        self.parser.intent={'action':'confirm','change_id':new_change}
        calls=self.parser.calls
        await self.interactions.handle(r['recipient'],uid(),'确认',quote='old-change-quote')
        self.assertEqual(self.parser.calls,calls,'deleted confirmation quotes must be rejected before model parsing')
        self.assertEqual(await self.store.call(lambda db:entity(db,'change',new_change)['state']),'pending')
        self.assertEqual(len(self.manager.jobs),1)

    async def test_prior_recipient_text_is_scrubbed_after_task_recipient_changes(self):
        await self.formal()
        old_recipient=config().recipient
        def seed(db):
            db.execute('INSERT INTO interactions VALUES (?,?,?,?,?)',(old_recipient,'old-recipient-reply','done','sensitive old reply',utcnow().isoformat()))
            outgoing(db,old_recipient+'/old-recipient-reply','reply',old_recipient,'sensitive old reply')
        await self.store.call(seed)
        current=await self.store.get_task(self.task['id'])
        await self.store.save_task(replace(config(),recipient='bot:FriendMessage:987'),current['id'],current['version'])
        await self.archived()
        await self.remove()
        row=await self.store.call(lambda db:db.execute("SELECT result FROM interactions WHERE message_id='old-recipient-reply'").fetchone())
        self.assertIsNone(row['result'])
        body=await self.store.call(lambda db:db.execute('SELECT body FROM outbox WHERE owner=?',(old_recipient+'/old-recipient-reply',)).fetchone()[0])
        self.assertIsNone(body)

    async def test_upgrade_backfills_deleted_change_quotes_without_active_changes(self):
        removed,active=uid(),uid()
        def seed(db):
            for ident in (removed,active):
                outgoing(db,ident,'change',config().recipient,'previous proposal')
            db.execute("UPDATE outbox SET body=NULL,state='held' WHERE channel='change'")
            put(db,'change',{'id':active,'recipient':config().recipient,'task_id':self.task['id'],'state':'pending'})
        await self.store.call(seed)
        await self.store.initialize()
        records=await self.store.call(lambda db:entities(db,'deleted_change'))
        self.assertEqual([x['id'] for x in records],[removed])

    async def test_legacy_reply_count_excludes_already_scrubbed_text(self):
        await self.store.call(lambda db:db.execute('INSERT INTO interactions VALUES (?,?,?,?,?)',
                             (config().recipient,'scrubbed','done',None,utcnow().isoformat())))
        await self.archived()
        self.assertEqual((await self.deletion.preview(self.task['id']))['legacy_reply_texts'],0)

    async def test_retained_reminder_keeps_its_own_recipient_and_timezone(self):
        r=await self.create()
        current=await self.store.get_task(self.task['id'])
        await self.store.save_task(replace(config(),recipient='bot:FriendMessage:987',timezone='UTC'),current['id'],current['version'])
        await self.archived()
        await self.remove()
        orphan=(await self.reminders())[0]
        self.assertEqual(orphan['interaction_config']['recipient'],r['recipient'])
        self.assertEqual(orphan['interaction_config']['timezone'],r['timezone'])

    async def test_delete_erases_followup_context_and_old_quote_cannot_operate(self):
        ident=await self.active_list()
        self.parser.intent={'action':'ask','question':'敏感追问：希望提前多久？'}
        message_id=uid()
        await self.interactions.handle(config().recipient,message_id,f'清单 {ident[:8]} 帮我选择')
        await self.drain()
        windows=await self.store.call(lambda db:entities(db,'dialogue'))
        self.assertTrue(any(item['turns'] for item in windows))
        quote=await self.store.call(lambda db:db.execute(
            "SELECT receipt FROM outbox WHERE owner=? AND channel='reply'",(config().recipient+'/'+message_id,)).fetchone()[0])
        await self.archived()
        await self.remove()
        windows=await self.store.call(lambda db:entities(db,'dialogue'))
        self.assertTrue(all(not item['turns'] and not any(item['refs'].values()) for item in windows))
        self.assertNotIn('敏感追问',str(windows))
        calls=self.parser.calls
        self.assertTrue(await self.interactions.handle(config().recipient,uid(),'一小时',quote))
        self.assertEqual(self.parser.calls,calls)
