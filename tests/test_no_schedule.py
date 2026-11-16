import asyncio
import json
import unittest
from datetime import timedelta

from digest.adapters import SendResult
from digest.domain import utcnow
from digest.receipts import NoScheduleRenderer, NO_SCHEDULE
from digest.workflow_store import entities
import test_workflow as fixtures


class Wording:
    def __init__(self):
        self.calls = []

    async def render(self, config):
        self.calls.append(config)
        await asyncio.sleep(0)
        return '春，' + NO_SCHEDULE


class NoScheduleTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.WorkflowTests.asyncTearDown
    formal = fixtures.WorkflowTests.formal
    candidate = fixtures.WorkflowTests.candidate
    drain = fixtures.WorkflowTests.drain

    async def asyncSetUp(self):
        await fixtures.WorkflowTests.asyncSetUp(self)
        self.wording = Wording()
        self.runtime.notice_renderer = self.wording

    async def parts(self, ident):
        return (await self.store.delivery_detail(ident))['parts']

    async def test_after_digest_ack_freezes_persona_text_without_dialogue(self):
        ident = await self.formal()
        self.assertFalse(self.wording.calls)
        await self.runtime.deliver_one()
        self.assertEqual(len(self.wording.calls), 1)
        self.assertEqual(self.wording.calls[0], self.task['config'])
        self.assertEqual(len(self.sender.calls), 1)
        await self.drain()
        self.assertEqual(self.sender.calls[1][1], '春，' + NO_SCHEDULE)
        self.assertFalse(await self.store.call(lambda db: entities(db, 'list')))
        self.assertFalse(await self.store.call(lambda db: entities(db, 'dialogue')))
        self.assertFalse(await self.interactions.handle(self.task['config']['recipient'], 'chat', '今天好累'))
        self.assertEqual(self.parser.calls, 0)
        self.assertEqual([p['channel'] for p in await self.parts(ident)], ['digest', 'no_schedule'])

    async def test_restart_and_concurrent_reconcile_never_repeat(self):
        ident = await self.formal()
        # Persist an acknowledged digest before running concurrent completion checks.
        await self.store.call(lambda db: db.execute("UPDATE outbox SET state='acknowledged' WHERE owner=?", (ident,)))
        await asyncio.gather(self.runtime.reconcile(), self.runtime.reconcile())
        self.assertEqual(len(self.wording.calls), 1)
        await self.store.initialize()
        await self.drain()
        await self.store.initialize()
        await self.runtime.reconcile()
        await self.drain()
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(len(self.wording.calls), 1)

    async def test_unknown_notice_requires_manual_verification_and_resend_uses_same_text(self):
        ident = await self.formal()
        self.sender.results = [SendResult('acknowledged', 'digest'), SendResult('unknown')]
        await self.drain()
        await self.runtime.reconcile()
        await self.drain()
        self.assertEqual(len(self.sender.calls), 2)
        with self.assertRaises(ValueError):
            await self.store.authorize_delivery(ident, 'no_schedule')
        part = next(p for p in await self.parts(ident) if p['channel'] == 'no_schedule')
        await self.store.resolve_unknown(part['id'], 'failed')
        await self.drain()
        self.assertEqual(len(self.sender.calls), 2)
        await self.store.authorize_delivery(ident, 'no_schedule')
        await self.drain()
        self.assertEqual(self.sender.calls[1], self.sender.calls[2])
        self.assertEqual(len(self.wording.calls), 1)

    async def test_no_notice_until_every_digest_part_succeeds(self):
        await self.store.settings({'text_limit': 100})
        ident = await self.formal('长正文' * 100)
        self.sender.results = [SendResult('acknowledged', 'first'), SendResult('failed')]
        await self.drain()
        self.assertFalse(self.wording.calls)
        self.assertFalse(any(p['channel'] == 'no_schedule' for p in await self.parts(ident)))
        await self.store.authorize_delivery(ident, 'digest')
        await self.drain()
        self.assertEqual(len(self.wording.calls), 1)

    async def test_candidates_that_expire_before_delivery_use_notice(self):
        ident = await self.formal(candidates=[self.candidate(hours=-1)])
        await self.drain()
        self.assertEqual(len(self.wording.calls), 1)
        self.assertFalse(any(p['channel'] == 'inquiry' for p in await self.parts(ident)))

    async def test_valid_candidate_uses_inquiry_without_notice_call(self):
        ident = await self.formal(candidates=[self.candidate()])
        await self.drain()
        self.assertFalse(self.wording.calls)
        self.assertFalse(any(p['channel'] == 'no_schedule' for p in await self.parts(ident)))

    async def test_interrupted_notice_is_unknown_after_restart_without_resend(self):
        ident = await self.formal()
        await self.runtime.deliver_one()
        part = await self.runtime.claim()
        self.assertEqual(part['channel'], 'no_schedule')
        await self.store.initialize()
        await self.runtime.reconcile()
        await self.drain()
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(next(p for p in await self.parts(ident) if p['channel'] == 'no_schedule')['state'], 'unknown')

    async def test_definite_failure_requires_explicit_resend(self):
        ident = await self.formal()
        self.sender.results = [SendResult('acknowledged', 'digest'), SendResult('failed')]
        await self.drain()
        await self.runtime.reconcile()
        await self.drain()
        self.assertEqual(len(self.sender.calls), 2)
        await self.store.authorize_delivery(ident, 'no_schedule')
        await self.drain()
        self.assertEqual(self.sender.calls[1], self.sender.calls[2])
        self.assertEqual(len(self.wording.calls), 1)

    async def test_preview_empty_and_historical_completed_runs_do_not_send(self):
        preview = await self.store.enqueue(self.task['id'])
        await self.store.finish(preview, 'ready', {'summary': '预览', 'candidates': []})
        await self.runtime.freeze(preview)
        empty = await self.store.enqueue(self.task['id'], utcnow(), 'scheduled')
        await self.store.finish(empty, 'empty', {'summary': '', 'candidates': []})
        await self.runtime.freeze(empty)
        old = await self.formal()
        await self.store.call(lambda db: (db.execute("UPDATE outbox SET state='acknowledged' WHERE owner=?", (old,)),
            db.execute('UPDATE delivery SET completed=1 WHERE run_id=?', (old,))))
        await self.runtime.reconcile()
        await self.drain()
        self.assertFalse(self.wording.calls)
        self.assertFalse(self.sender.calls)


class Models:
    def __init__(self, text=None, fail=False):
        self.text = text
        self.fail = fail

    async def prepare(self, task):
        self.task = task
        return {'persona': '温和地称呼用户为春', 'window': 16384}

    async def generate(self, prepared, prompt, policy):
        self.policy = policy
        if self.fail:
            raise TimeoutError()
        return json.dumps({'text': self.text}, ensure_ascii=False), 'fake'


class NoScheduleWordingTests(unittest.IsolatedAsyncioTestCase):
    async def test_persona_and_scoped_fact(self):
        models = Models('春，' + NO_SCHEDULE)
        text = await NoScheduleRenderer(models).render({'persona_id': 'chosen', 'prompt': '简洁'})
        self.assertEqual(text, '春，' + NO_SCHEDULE)
        self.assertEqual(models.task.persona_id, 'chosen')
        self.assertIn('温和地称呼用户为春', models.policy)
        self.assertIn('简洁', models.policy)

    async def test_failure_or_unscoped_claim_uses_factual_fallback(self):
        for models in [Models(fail=True), Models('今天没有日程啦'),
                       Models(NO_SCHEDULE + '今天不用做事啦'), Models(NO_SCHEDULE + '需要提醒吗？')]:
            self.assertEqual(await NoScheduleRenderer(models).render({}), NO_SCHEDULE)
