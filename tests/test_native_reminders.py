import asyncio
import unittest
from datetime import timedelta
from types import SimpleNamespace

from digest.domain import utcnow
from digest.native_reminders import NativeReminders
from digest.interactions import Interactions
from digest.workflow_store import entities, put
import test_workflow as workflows


class Manager:
    def __init__(self):
        self.jobs = {}
        self.calls = 0
        self.lose_response = False

    async def list_jobs(self):
        return list(self.jobs.values())

    async def add_active_job(self, **kwargs):
        self.calls += 1
        kwargs['payload']['run_at'] = kwargs.pop('run_at').isoformat()
        job = SimpleNamespace(job_id=str(self.calls), **kwargs)
        self.jobs[job.job_id] = job
        if self.lose_response:
            raise TimeoutError()
        return job

    async def update_job(self, ident, **kwargs):
        job = self.jobs.get(ident)
        if job:
            for key, value in kwargs.items():
                setattr(job, key, value)
        return job

    async def delete_job(self, ident):
        self.jobs.pop(ident, None)


class NativeTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = workflows.WorkflowTests.asyncTearDown
    formal = workflows.WorkflowTests.formal
    candidate = workflows.WorkflowTests.candidate
    drain = workflows.WorkflowTests.drain
    active_list = workflows.WorkflowTests.active_list
    select = workflows.WorkflowTests.select
    reminders = workflows.WorkflowTests.reminders

    async def asyncSetUp(self):
        await workflows.WorkflowTests.asyncSetUp(self)
        self.manager = Manager()
        self.native = NativeReminders(self.store, self.manager)
        self.runtime.native = self.native
        self.interactions = Interactions(self.store, self.parser, self.runtime)

    async def create(self):
        ident = await self.active_list()
        await self.select(ident)
        return (await self.reminders())[0]

    async def test_create_dedupe_and_no_plugin_delivery(self):
        r = await self.create()
        await self.select(r['list_ids'][0])
        await asyncio.gather(self.native.sync(), self.native.sync())
        self.assertEqual(self.manager.calls, 1)
        self.assertTrue(r['native_job_id'])
        await self.runtime.reconcile(now=utcnow()+timedelta(hours=4))
        rows=await self.store.call(lambda db:list(db.execute("SELECT * FROM outbox WHERE channel='reminder'")))
        self.assertEqual(rows, [])

    async def test_native_edit_pause_delete_authoritative(self):
        r = await self.create()
        job = self.manager.jobs[r['native_job_id']]
        when = (utcnow()+timedelta(minutes=70)).isoformat()
        job.payload = {**job.payload, 'run_at':when}
        job.enabled = False
        await self.native.sync()
        changed = (await self.reminders())[0]
        self.assertEqual(changed['remind_at'], when)
        self.assertEqual(changed['state'], 'native_paused')
        self.assertGreater(changed['version'], r['version'])
        await self.manager.delete_job(job.job_id)
        await self.native.sync()
        await self.native.sync()
        self.assertEqual((await self.reminders())[0]['state'], 'native_absent')
        self.assertEqual(self.manager.calls, 1)
        with self.assertRaisesRegex(ValueError,'已结束或已删除'):
            latest=(await self.reminders())[0]
            await self.interactions.manage(r['id'],'modify',latest['version'],{'lead_minutes':20})

    async def test_modify_cancel_uses_same_native_job(self):
        r = await self.create()
        await self.interactions.manage(r['id'],'modify',r['version'],{'lead_minutes':10})
        r = (await self.reminders())[0]
        self.assertEqual(self.manager.jobs[r['native_job_id']].payload['run_at'],r['remind_at'])
        await self.interactions.manage(r['id'],'cancel',r['version'])
        self.assertEqual(self.manager.jobs,{})
        await self.native.sync()
        self.assertEqual(self.manager.calls,1)

    async def test_lost_create_response_recovers_without_duplicate(self):
        self.manager.lose_response=True
        r = await self.create()
        self.assertIn('native_error',r)
        await self.native.sync()
        self.assertEqual(self.manager.calls,1)
        self.assertEqual((await self.reminders())[0]['native_job_id'],'1')

    async def test_uncertain_create_never_blindly_retries(self):
        self.manager.lose_response=True
        await self.create()
        self.manager.jobs.clear()
        await self.native.sync()
        await self.native.sync()
        self.assertEqual(self.manager.calls,1)
        self.assertIn('结果未知',(await self.reminders())[0]['native_error'])

    async def test_restart_recovers_link_and_retains_paused_job(self):
        r = await self.create()
        self.manager.jobs[r['native_job_id']].enabled=False
        r.pop('native_job_id')
        await self.store.call(lambda db:put(db,'reminder',r))
        restored=NativeReminders(self.store,self.manager)
        await restored.sync()
        self.assertEqual(self.manager.calls,1)
        self.assertEqual((await self.reminders())[0]['state'],'native_paused')

    async def test_native_destination_and_recurrence_not_overwritten(self):
        r=await self.create()
        job=self.manager.jobs[r['native_job_id']]
        job.payload={**job.payload,'session':'different:FriendMessage:99'}
        await self.native.sync()
        r=(await self.reminders())[0]
        with self.assertRaisesRegex(ValueError,'投递目标已变化'):
            await self.interactions.manage(r['id'],'modify',r['version'],{'lead_minutes':5})

    async def test_legacy_attempt_not_migrated(self):
        self.runtime.native=None
        self.interactions=Interactions(self.store,self.parser,self.runtime)
        r=await self.create()
        from digest.workflow_store import outgoing
        def attempted(db):
            outgoing(db,r['id']+':1','reminder',r['recipient'],'legacy',1500)
            db.execute("UPDATE outbox SET attempts=1,state='unknown' WHERE channel='reminder'")
        await self.store.call(attempted)
        await self.native.sync()
        self.assertEqual(self.manager.calls,0)

    async def test_time_only_edit_preserves_native_note(self):
        r=await self.create()
        job=self.manager.jobs[r['native_job_id']]
        job.payload={**job.payload,'note':'用户在未来任务中自定义的提醒说明'}
        job.description='用户自定义说明'
        job.timezone='UTC'
        await self.native.sync()
        r=(await self.reminders())[0]
        await self.interactions.manage(r['id'],'modify',r['version'],{'lead_minutes':5})
        self.assertEqual(job.payload['note'],'用户在未来任务中自定义的提醒说明')
        self.assertEqual(job.description,'用户自定义说明')
        self.assertEqual(job.timezone,'UTC')

    async def test_event_date_edit_updates_native_event_details(self):
        r=await self.create()
        new_event=(utcnow()+timedelta(hours=5)).isoformat()
        await self.interactions.manage(r['id'],'modify',r['version'],{'event_at':new_event})
        self.assertIn(new_event,self.manager.jobs[r['native_job_id']].payload['note'])

    async def test_mismatched_ownership_prevents_private_cancellation(self):
        r=await self.create()
        self.manager.jobs[r['native_job_id']].payload['digest_reminder_owner']='unrelated owner'
        await self.native.sync()
        r=(await self.reminders())[0]
        answer=await self.interactions.manage(r['id'],'cancel',r['version'])
        self.assertIn('未确认',answer)
        self.assertEqual(len(self.manager.jobs),1)
