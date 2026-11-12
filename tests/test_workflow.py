import asyncio
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from digest.adapters import SendResult
from digest.domain import utcnow, parse_time
from digest.interactions import Interactions
from digest.runtime import Runtime
from digest.storage import Store
from digest.workflow_store import entity, entities, outgoing, put, uid
from test_core import config


class Sender:
    def __init__(self):
        self.calls=[];self.results=[]
    async def send_text(self,recipient,body):
        self.calls.append((recipient,body))
        return self.results.pop(0) if self.results else SendResult('acknowledged',str(len(self.calls)))


class Parser:
    def __init__(self,intent=None): self.intent=intent;self.calls=0
    async def parse(self,*args): self.calls+=1;return self.intent


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'workflow.db')
        await self.store.initialize()
        self.task=await self.store.save_task(config())
        self.task=await self.store.set_enabled(self.task['id'],self.task['version'],True)
        self.sender=Sender()
        self.runtime=Runtime(self.store,SimpleNamespace(wake=asyncio.Event()),self.sender)
        self.parser=Parser()
        self.interactions=Interactions(self.store,self.parser,self.runtime)

    async def asyncTearDown(self): self.tmp.cleanup()

    async def formal(self,summary='通知正文',candidates=None,task=None,changes=None):
        task=task or self.task
        ident=await self.store.enqueue(task['id'],utcnow(),'scheduled')
        await self.store.finish(ident,'ready',{'summary':summary,'candidates':candidates or [],'changes':changes or []})
        await self.runtime.freeze(ident)
        return ident

    def candidate(self,hours=3,title='课程说明会'):
        return {'title':title,'event_at':(utcnow()+timedelta(hours=hours)).isoformat(),'sources':['bot/1'],'place':'教室'}

    async def drain(self):
        for _ in range(50):
            if not await self.runtime.deliver_one(): return
        self.fail('outbox did not become idle')

    async def active_list(self,candidate=None,task=None):
        ident=await self.formal(candidates=[candidate or self.candidate()],task=task)
        await self.drain()
        return ident

    async def select(self,ident,**extra):
        self.parser.intent={'action':'select','list_id':ident,'indices':[1],**extra}
        return await self.interactions.handle(config().recipient,uid(),f'清单 {ident[:8]} 加入第1项'+('，现在提醒' if extra.get('immediate') else ''))

    async def reminders(self):
        return await self.store.call(lambda db:entities(db,'reminder'))

    async def test_schedule_idempotent_and_restart_missed(self):
        now=utcnow().replace(second=0,microsecond=0)
        await self.store.call(lambda db:db.execute('UPDATE tasks SET next_at=?',(now.isoformat(),)))
        await asyncio.gather(self.store.schedule(now),self.store.schedule(now))
        runs=await self.store.list_runs(self.task['id'])
        self.assertEqual(len(runs),1);self.assertEqual(runs[0]['status'],'queued')
        old=now-timedelta(days=2)
        await self.store.call(lambda db:db.execute('UPDATE tasks SET next_at=?',(old.isoformat(),)))
        await self.store.schedule(now,restart=True)
        runs=await self.store.list_runs(self.task['id'])
        self.assertTrue(any(r['status']=='missed' for r in runs))
        self.assertEqual(sum(r['status']=='queued' for r in runs),1)

    async def test_partial_resume_skips_acknowledged_and_inquiry_is_separate(self):
        await self.store.settings({'text_limit':100})
        ident=await self.formal(summary='长正文。'*50,candidates=[self.candidate()])
        self.sender.results=[SendResult('acknowledged','first'),SendResult('failed')]
        await self.drain()
        self.assertEqual(len(self.sender.calls),2)
        detail=await self.store.delivery_detail(ident)
        self.assertEqual(detail['run']['status'],'partial')
        self.assertFalse(any(p['channel']=='inquiry' for p in detail['parts']))
        await self.store.prepare_catchup(ident)
        await self.store.authorize_delivery(ident,'digest')
        await self.drain()
        self.assertEqual(self.sender.calls.count(self.sender.calls[0]),1)
        self.assertEqual((await self.store.get_run(ident))['status'],'sent')
        self.assertEqual((await self.store.call(lambda db:entity(db,'list',ident)))['state'],'active')

    async def test_unknown_does_not_retry_and_requires_verification(self):
        ident=await self.formal()
        self.sender.results=[SendResult('unknown')]
        await self.drain();await self.runtime.tick();await self.drain()
        self.assertEqual(len(self.sender.calls),1)
        with self.assertRaises(ValueError): await self.store.authorize_delivery(ident,'digest')
        part=(await self.store.delivery_detail(ident))['parts'][0]
        await self.store.resolve_unknown(part['id'],'acknowledged')
        await self.runtime.reconcile()
        self.assertEqual((await self.store.get_run(ident))['status'],'sent')

    async def test_resolving_unknown_part_does_not_auto_send_remaining_parts(self):
        await self.store.settings({'text_limit':100})
        ident=await self.formal('分条。'*80)
        self.sender.results=[SendResult('unknown')];await self.drain()
        part=(await self.store.delivery_detail(ident))['parts'][0]
        await self.store.resolve_unknown(part['id'],'acknowledged')
        await self.runtime.reconcile();await self.drain()
        self.assertEqual(len(self.sender.calls),1)
        await self.store.authorize_delivery(ident,'digest');await self.drain()
        self.assertGreater(len(self.sender.calls),1)

    async def test_inquiry_failure_retries_without_resending_digest(self):
        ident=await self.formal(candidates=[self.candidate()])
        self.sender.results=[SendResult('acknowledged','digest'),SendResult('failed')]
        await self.drain()
        self.assertEqual((await self.store.get_run(ident))['status'],'sent')
        record=await self.store.call(lambda db:entity(db,'list',ident))
        self.assertEqual(record['state'],'awaiting_delivery');self.assertIsNone(record['expires_at'])
        await self.store.authorize_delivery(ident,'inquiry');await self.drain()
        self.assertEqual(len(self.sender.calls),3)
        self.assertEqual(self.sender.calls[1],self.sender.calls[2])
        self.assertEqual((await self.store.call(lambda db:entity(db,'list',ident)))['state'],'active')

    async def test_late_manual_ack_does_not_renew_expired_list(self):
        ident=await self.formal(candidates=[self.candidate(hours=72)])
        self.sender.results=[SendResult('acknowledged','digest'),SendResult('unknown')]
        await self.drain()
        part=next(p for p in (await self.store.delivery_detail(ident))['parts'] if p['channel']=='inquiry')
        old=(utcnow()-timedelta(days=2)).isoformat()
        await self.store.call(lambda db:db.execute('UPDATE outbox SET sent_at=? WHERE id=?',(old,part['id'])))
        await self.store.resolve_unknown(part['id'],'acknowledged');await self.runtime.reconcile()
        self.assertEqual((await self.store.call(lambda db:entity(db,'list',ident)))['state'],'expired')

    async def test_restart_sending_becomes_unknown(self):
        ident=await self.formal();part=await self.runtime.claim()
        self.assertIsNotNone(part)
        await self.store.initialize();await self.runtime.reconcile();await self.drain()
        self.assertEqual((await self.store.delivery_detail(ident))['parts'][0]['state'],'unknown')
        self.assertFalse(self.sender.calls)

    async def test_manual_generation_never_sends_before_submit(self):
        ident=await self.store.enqueue(self.task['id'],utcnow(),'scheduled')
        await self.store.finish(ident,'failed',error='模拟模型故障')
        await self.store.prepare_catchup(ident)
        await self.store.finish(ident,'ready',{'summary':'重新生成正文','candidates':[]})
        await self.runtime.freeze(ident);await self.drain()
        self.assertFalse(self.sender.calls)
        await self.store.authorize_delivery(ident,'digest');await self.drain()
        self.assertEqual(len(self.sender.calls),1)

    async def test_disable_stops_next_part_but_keeps_reminders(self):
        ident=await self.active_list();await self.select(ident)
        await self.store.settings({'text_limit':100})
        formal=await self.formal('长文。'*100)
        await self.runtime.deliver_one()  # queued interaction response
        task=await self.store.get_task(self.task['id'])
        await self.store.set_enabled(task['id'],task['version'],False)
        await self.drain()
        parts=(await self.store.delivery_detail(formal))['parts']
        self.assertTrue(all(p['state']=='held' for p in parts))
        self.assertEqual((await self.reminders())[0]['state'],'scheduled')
        current=await self.store.get_task(task['id'])
        await self.store.set_enabled(current['id'],current['version'],True)
        before=len(self.sender.calls);await self.drain()
        self.assertEqual(len(self.sender.calls),before)

    async def test_select_supplement_deduplicate_and_duplicate_message(self):
        candidate=self.candidate();ident=await self.active_list(candidate)
        record=await self.store.call(lambda db:entity(db,'list',ident))
        self.parser.intent={'action':'select','list_id':ident,'indices':[1]}
        for _ in range(2): await self.interactions.handle(config().recipient,'same-message','加入第1项')
        self.assertEqual(self.parser.calls,1)
        await self.select(ident)
        self.assertEqual(len(await self.reminders()),1)
        after=await self.store.call(lambda db:entity(db,'list',ident))
        self.assertEqual(after['expires_at'],record['expires_at'])
        second=await self.store.save_task(config(name='second'))
        second=await self.store.set_enabled(second['id'],second['version'],True)
        other=await self.active_list(candidate,second);await self.select(other)
        reminders=await self.reminders()
        self.assertEqual(len(reminders),1);self.assertEqual(len(reminders[0]['task_ids']),2)

    async def test_old_list_superseded_even_when_no_new_candidates(self):
        ident=await self.active_list();await self.select(ident)
        await self.formal('新简报，没有日程');await self.drain()
        self.assertEqual((await self.store.call(lambda db:entity(db,'list',ident)))['state'],'superseded')
        await self.select(ident)
        self.assertEqual(len(await self.reminders()),1)

    async def test_ambiguous_lists_never_choose_latest(self):
        first=await self.active_list()
        other=await self.store.save_task(config(name='other'))
        other=await self.store.set_enabled(other['id'],other['version'],True)
        await self.active_list(task=other)
        self.parser.intent={'action':'select','list_id':first,'indices':[1]}
        await self.interactions.handle(config().recipient,'ambiguous','加入第1项')
        self.assertEqual(await self.reminders(),[])

    async def test_expired_list_and_wrong_recipient_cannot_create(self):
        ident=await self.active_list()
        def expire(db):
            record=entity(db,'list',ident);record['expires_at']=(utcnow()-timedelta(seconds=1)).isoformat();put(db,'list',record)
        await self.store.call(expire);await self.select(ident)
        self.assertEqual(await self.reminders(),[])
        self.assertFalse(await self.interactions.handle('bot:FriendMessage:789','foreign','加入第1项'))

    async def test_past_reminder_asks_then_explicit_now_creates(self):
        ident=await self.active_list(self.candidate(hours=.2));await self.select(ident)
        self.assertFalse(await self.reminders())
        change=(await self.store.call(lambda db:entities(db,'change')))[0]
        self.parser.intent={'action':'confirm','change_id':change['id'],'immediate':True}
        await self.interactions.handle(config().recipient,'confirm',f"确认 {change['id'][:8]} 现在提醒")
        self.assertEqual(len(await self.reminders()),1)

    async def test_explicit_recent_past_time_is_not_silently_sent_now(self):
        ident=await self.active_list()
        await self.select(ident,remind_at=(utcnow()-timedelta(seconds=30)).isoformat())
        self.assertEqual(await self.reminders(),[])
        self.assertEqual(len(await self.store.call(lambda db:entities(db,'change'))),1)

    async def test_group_change_needs_confirmation_and_preserves_lead(self):
        candidate=self.candidate();ident=await self.active_list(candidate);await self.select(ident)
        original=(await self.reminders())[0]
        later=parse_time(candidate['event_at'])+timedelta(hours=2)
        changes=[{'title':candidate['title'],'old_event_at':candidate['event_at'],'event_at':later.isoformat(),'cancelled':False}]
        await self.formal('改期通知',changes=changes);await self.drain()
        self.assertEqual((await self.reminders())[0]['event_at'],original['event_at'])
        change=(await self.store.call(lambda db:entities(db,'change')))[0]
        self.parser.intent={'action':'confirm','change_id':change['id']}
        await self.interactions.handle(config().recipient,'accept-change',f"确认 {change['id'][:8]}")
        modified=(await self.reminders())[0]
        self.assertEqual(modified['lead_minutes'],30)
        self.assertEqual(parse_time(modified['remind_at']),later-timedelta(minutes=30))

    async def test_reminder_definite_failure_retries_twice_and_unknown_stops(self):
        ident=await self.active_list();await self.select(ident,immediate=True);await self.drain()
        # First reminder may have been sent by drain; use another occurrence with controlled sender.
        reminder=(await self.reminders())[0]
        await self.interactions.manage(reminder['id'],'modify',reminder['version'],{'immediate':True})
        self.sender.results=[SendResult('failed')]*3
        await self.runtime.reconcile()
        for expected in range(1,4):
            await self.runtime.deliver_one()
            await self.store.call(lambda db:db.execute("UPDATE outbox SET next_at=? WHERE channel='reminder'",((utcnow()-timedelta(seconds=1)).isoformat(),)))
        self.assertEqual((await self.reminders())[0]['state'],'failed')
        before=len(self.sender.calls);await self.drain();self.assertEqual(len(self.sender.calls),before)
        reminder=(await self.reminders())[0]
        await self.interactions.manage(reminder['id'],'modify',reminder['version'],{'immediate':True})
        self.sender.results=[SendResult('unknown')];await self.runtime.reconcile();await self.drain()
        self.assertEqual((await self.reminders())[0]['state'],'unknown')

    async def test_restart_missed_reminder_asks_instead_of_sending(self):
        ident=await self.active_list();await self.select(ident)
        def past(db):
            reminder=entities(db,'reminder')[0];reminder['remind_at']=(utcnow()-timedelta(minutes=5)).isoformat();put(db,'reminder',reminder)
        await self.store.call(past)
        await self.runtime.reconcile(restart=True)
        self.assertEqual((await self.reminders())[0]['state'],'awaiting_choice')
        changes=await self.store.call(lambda db:entities(db,'change'))
        self.assertEqual(len(changes),1)
        await self.runtime.reconcile(restart=True)
        self.assertEqual(len(await self.store.call(lambda db:entities(db,'change'))),1)

    async def test_restart_pending_reminder_does_not_reactivate_after_question(self):
        ident=await self.active_list();await self.select(ident,immediate=True)
        await self.runtime.reconcile()
        await self.runtime.reconcile(restart=True)
        await self.runtime.reconcile()
        self.assertEqual((await self.reminders())[0]['state'],'awaiting_choice')
        await self.drain()
        self.assertFalse(any('【日程提醒】' in body for _,body in self.sender.calls))

    async def test_change_version_conflict_does_not_overwrite_user_edit(self):
        candidate=self.candidate();ident=await self.active_list(candidate);await self.select(ident)
        later=(parse_time(candidate['event_at'])+timedelta(hours=2)).isoformat()
        await self.formal('变更',changes=[{'title':candidate['title'],'old_event_at':candidate['event_at'],'event_at':later,'cancelled':False}]);await self.drain()
        reminder=(await self.reminders())[0]
        await self.interactions.manage(reminder['id'],'modify',reminder['version'],{'lead_minutes':60})
        change=(await self.store.call(lambda db:entities(db,'change')))[0]
        with self.assertRaises(ValueError): await self.interactions.manage_change(change['id'],True)
        self.assertEqual((await self.reminders())[0]['lead_minutes'],60)

    async def test_late_older_catchup_does_not_supersede_new_list(self):
        older=await self.formal(candidates=[self.candidate()])
        await self.store.prepare_catchup(older)
        newer=await self.active_list()
        await self.store.authorize_delivery(older,'digest');await self.drain()
        record=await self.store.call(lambda db:entity(db,'list',newer))
        self.assertEqual(record['state'],'active')
        self.assertFalse(any(x['id']==older for x in await self.store.call(lambda db:entities(db,'list'))))

    async def test_cleanup_keeps_active_business_and_minimal_ledger(self):
        ident=await self.active_list();await self.select(ident)
        await self.store.call(lambda db:db.execute("UPDATE runs SET updated_at='2000-01-01T00:00:00+00:00'"))
        await self.store.cleanup(30)
        self.assertIsNotNone((await self.store.get_run(ident))['result'])
        empty=await self.formal('无需日程');await self.drain()
        await self.store.call(lambda db:db.execute("UPDATE runs SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?",(empty,)))
        await self.store.cleanup(30)
        detail=await self.store.delivery_detail(empty)
        self.assertIsNone(detail['run']['result']);self.assertIsNone(detail['parts'][0]['body'])
        self.assertEqual(detail['parts'][0]['state'],'acknowledged');self.assertTrue(detail['parts'][0]['hash'])

    async def test_interrupted_reply_recovery_never_replays_operation(self):
        await self.store.call(lambda db:db.execute('INSERT INTO interactions(recipient,message_id,state) VALUES (?,?,?)',
                                                   (config().recipient,'interrupted','processing')))
        await self.store.initialize();await self.drain()
        self.assertFalse(await self.reminders());self.assertEqual(len(self.sender.calls),1)
        await self.store.initialize();await self.drain();self.assertEqual(len(self.sender.calls),1)

    async def test_explicit_cancel_idempotent_and_archived_task_keeps_reminder(self):
        ident=await self.active_list();await self.select(ident)
        reminder=(await self.reminders())[0]
        task=await self.store.get_task(self.task['id'])
        await self.store.set_enabled(task['id'],task['version'],False,archive=True)
        self.assertEqual((await self.reminders())[0]['state'],'scheduled')
        self.parser.intent={'action':'cancel','reminder_id':reminder['id']}
        await self.interactions.handle(config().recipient,'cancel',f"取消 {reminder['id'][:8]} 的提醒")
        self.assertEqual((await self.reminders())[0]['state'],'cancelled')

    async def test_schema_v2_restart_keeps_entities_and_receipts(self):
        ident=await self.active_list();await self.select(ident)
        await self.store.initialize()
        self.assertEqual(len(await self.reminders()),1)
        self.assertEqual((await self.store.get_run(ident))['status'],'sent')


if __name__=='__main__': unittest.main()
