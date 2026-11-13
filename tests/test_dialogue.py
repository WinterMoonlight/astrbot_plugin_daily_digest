import asyncio
import copy
import json
import unittest
from datetime import timedelta
from unittest.mock import patch

from digest import dialogue
from digest.adapters import SendResult
from digest.domain import parse_time, utcnow
from digest.interactions import Interactions, Interpreter
from digest.workflow_store import entities, entity, put, uid
from test_core import config
import test_workflow as workflows


class Parser:
    def __init__(self):
        self.calls = []
        self.intent = {'action': 'ignore'}

    async def parse(self, task, text, context):
        self.calls.append((text, copy.deepcopy(context)))
        await asyncio.sleep(0)
        if isinstance(self.intent, Exception):
            raise self.intent
        return self.intent


class DialogueTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = workflows.WorkflowTests.asyncTearDown
    formal = workflows.WorkflowTests.formal
    candidate = workflows.WorkflowTests.candidate
    drain = workflows.WorkflowTests.drain
    active_list = workflows.WorkflowTests.active_list
    reminders = workflows.WorkflowTests.reminders

    async def asyncSetUp(self):
        await workflows.WorkflowTests.asyncSetUp(self)
        self.parser = Parser()
        self.interactions = Interactions(self.store, self.parser, self.runtime)
        self.recipient = config().recipient

    async def handle(self, text, message_id=None, quote=None):
        return await self.interactions.handle(self.recipient, message_id or uid(), text, quote)

    async def windows(self):
        return await self.store.call(lambda db: entities(db, 'dialogue', self.recipient))

    async def quote_for(self, owner, channel):
        return await self.store.call(lambda db: db.execute(
            'SELECT receipt FROM outbox WHERE owner=? AND channel=? ORDER BY ordinal DESC',
            (owner, channel)).fetchone()[0])

    async def ask(self, question='你希望提前半小时还是一小时？'):
        self.parser.intent = {'action': 'ask', 'question': question}
        message_id = uid()
        self.assertTrue(await self.handle('第一项帮我设个合适的提前时间', message_id))
        return message_id

    async def test_three_messages_include_small_talk_without_closing_first(self):
        ident = await self.active_list()
        self.assertFalse(await self.handle('刚才说到一半的闲聊'))
        self.parser.intent = {'action': 'select', 'list_id': ident, 'indices': [1]}
        self.assertTrue(await self.handle('第一项就行'))
        self.assertEqual(len(self.parser.calls), 2)
        self.assertEqual(len(await self.reminders()), 1)
        self.assertFalse(await self.handle('接着聊刚才的事'))
        self.assertEqual(len(self.parser.calls), 2)

    async def test_visible_list_hides_internal_id_and_quote_still_selects(self):
        ident = await self.active_list()
        detail = await self.store.delivery_detail(ident)
        text = ''.join(part['body'] for part in detail['parts'] if part['channel'] == 'inquiry')
        self.assertNotIn(ident[:8], text)
        self.assertIn('第1项，提前半小时', text)
        receipt = await self.quote_for(ident, 'inquiry')
        self.parser.intent = {'action': 'select', 'list_id': ident, 'indices': [1]}
        self.assertTrue(await self.handle('第一项', quote=receipt))
        self.assertEqual(len(await self.reminders()), 1)
        self.assertFalse(await self.handle('接着聊刚才的事'))
        self.assertEqual(len(self.parser.calls), 1)

    async def test_shared_activity_condition_is_shown_once_and_survives_selection(self):
        condition = '这场活动面向名单内同学，请先核对是否参加。'
        first = {**self.candidate(title='学院迎新活动到场签到'), 'condition': condition}
        second = {**self.candidate(hours=4, title='学院迎新活动正式开始'), 'condition': condition}
        ident = await self.formal(candidates=[first, second])
        await self.drain()
        detail = await self.store.delivery_detail(ident)
        text = ''.join(p['body'] for p in detail['parts'] if p['channel'] == 'inquiry')
        self.assertEqual(text.count(condition), 1)
        self.assertIn('1. 学院迎新活动到场签到', text)
        self.assertNotIn(ident[:8], text)
        self.assertIn('前 3 条', text)
        self.parser.intent = {'action': 'select', 'list_id': ident, 'indices': [1]}
        self.assertTrue(await self.handle('第一项'))
        self.assertEqual((await self.reminders())[0]['condition'], condition)
        from digest.native_reminders import NativeReminders
        payload = NativeReminders(self.store, None).payload((await self.reminders())[0])
        self.assertEqual(payload['digest_event']['condition'], condition)

    async def test_only_three_messages_are_allowed_without_duplicate_counting(self):
        await self.active_list()
        self.assertFalse(await self.interactions.handle(self.recipient, '', '没有编号'))
        self.assertFalse(await self.handle('  \n '))
        for _ in range(2):
            self.assertFalse(await self.handle('同一事件重送', 'same-message'))
        self.assertEqual(len(self.parser.calls), 1)
        self.assertFalse(await self.handle('同样文字，新消息'))
        self.assertFalse(await self.handle('同样文字，新消息'))
        await self.runtime.reconcile(due=False)
        self.assertEqual((await self.windows())[0]['state'], 'active')
        self.assertFalse(await self.handle('第四条立即发送也不自动识别'))
        self.assertEqual(len(self.parser.calls), 3)
        self.assertEqual((await self.windows())[0]['remaining'], 0)

    async def test_delayed_messages_use_remaining_quota_then_stop_automatic_routing(self):
        ident = await self.active_list()
        self.assertFalse(await self.handle('第一条闲聊'))
        end = parse_time((await self.windows())[0]['opened_at']) + timedelta(hours=1)
        await self.runtime.reconcile(now=end, due=False)
        self.assertEqual((await self.windows())[0]['state'], 'active')
        with patch('digest.interactions.utcnow', return_value=end):
            self.assertFalse(await self.handle('一小时后的第二条'))
            self.assertFalse(await self.handle('第三条仍有额度'))
            self.assertFalse(await self.handle('第四条已无额度'))
        self.assertEqual(len(self.parser.calls), 3)
        await self.runtime.reconcile(now=end, due=False)
        self.assertEqual((await self.windows())[0]['remaining'], 0)
        record = await self.store.call(lambda db: entity(db, 'list', ident))
        self.assertEqual(record['state'], 'active')

    async def test_first_reply_after_one_hour_selects_without_quote(self):
        ident = await self.active_list()
        late = parse_time((await self.windows())[0]['opened_at']) + timedelta(hours=1)
        await self.runtime.reconcile(now=late, due=False)
        self.parser.intent = {'action': 'select', 'list_id': ident, 'indices': [1]}
        with patch('digest.interactions.utcnow', return_value=late):
            self.assertTrue(await self.handle('第一项，提前半小时提醒'))
        self.assertEqual(len(self.parser.calls), 1)
        self.assertEqual(len(await self.reminders()), 1)
        self.assertEqual((await self.windows())[0]['state'], 'closed')

    async def test_explicit_prefix_id_and_quote_bypass_exhausted_quota(self):
        ident = await self.active_list()
        receipt = await self.quote_for(ident, 'inquiry')
        for _ in range(3):
            await self.handle('闲聊')
        self.parser.calls.clear()
        end = parse_time((await self.windows())[0]['opened_at']) + timedelta(hours=1)
        await self.runtime.reconcile(now=end, due=False)
        with patch('digest.interactions.utcnow', return_value=end):
            await self.handle('/日程 帮我看一下日程')
            await self.handle(f'清单 {ident[:8]} 第一个')
            await self.handle('第一个', quote=receipt)
            await self.handle('普通聊天')
        self.assertEqual(len(self.parser.calls), 3)
        self.assertEqual((await self.windows())[0]['remaining'], 0)
        self.assertEqual(self.parser.calls[0][0], '帮我看一下日程')

    async def test_prefix_without_business_context_returns_help_without_model(self):
        self.assertTrue(await self.handle('/日程'))
        self.assertFalse(self.parser.calls)

    async def test_no_window_before_question_receipt_or_after_failed_question(self):
        ident = await self.formal(candidates=[self.candidate()])
        self.assertFalse(await self.handle('第一项'))
        self.sender.results = [SendResult('acknowledged', 'digest'), SendResult('failed')]
        await self.drain()
        self.assertFalse(await self.handle('闲聊'))
        self.assertFalse(self.parser.calls)
        await self.store.authorize_delivery(ident, 'inquiry')
        await self.drain()
        self.assertFalse(await self.handle('闲聊'))
        self.assertEqual(len(self.parser.calls), 1)

    async def test_multipart_question_opens_only_after_every_part_acknowledged(self):
        await self.store.settings({'text_limit': 100})
        ident = await self.formal(candidates=[self.candidate()])
        await self.runtime.deliver_one()
        await self.runtime.deliver_one()
        self.assertFalse(await self.handle('还没发完'))
        self.assertFalse(self.parser.calls)
        await self.drain()
        self.assertFalse(await self.handle('现在发完了'))
        self.assertEqual(len(self.parser.calls), 1)
        self.assertEqual((await self.windows())[0]['owner'], ident)

    async def test_manual_late_ack_preserves_delivery_time_and_allows_reply(self):
        ident = await self.formal(candidates=[self.candidate()])
        self.sender.results = [SendResult('acknowledged', 'digest'), SendResult('unknown')]
        await self.drain()
        part = next(row for row in (await self.store.delivery_detail(ident))['parts'] if row['channel'] == 'inquiry')
        old = (utcnow() - timedelta(minutes=6)).isoformat()
        await self.store.call(lambda db: db.execute('UPDATE outbox SET sent_at=? WHERE id=?', (old, part['id'])))
        await self.store.resolve_unknown(part['id'], 'acknowledged')
        await self.runtime.reconcile()
        self.assertFalse(await self.handle('普通聊天'))
        window = (await self.windows())[0]
        self.assertEqual(window['opened_at'], old)
        self.assertNotIn('expires_at', window)
        self.assertEqual(window['remaining'], 2)
        self.assertEqual(len(self.parser.calls), 1)

    async def test_future_reminder_alone_does_not_intercept_small_talk(self):
        ident = await self.active_list()
        self.parser.intent = {'action': 'select', 'list_id': ident, 'indices': [1]}
        await self.handle('第一项')
        self.assertEqual(len(await self.reminders()), 1)
        for _ in range(4):
            self.assertFalse(await self.handle('今天好累'))
        self.assertEqual(len(self.parser.calls), 1)

    async def test_followup_carries_user_request_and_question_not_small_talk(self):
        ident = await self.active_list()
        first = await self.ask()
        self.assertFalse(await self.handle('问题还没有发到 QQ'))
        self.assertEqual(len(self.parser.calls), 1)
        await self.drain()
        self.parser.intent = {'action': 'ignore'}
        self.assertFalse(await self.handle('刚才那件别的事情'))
        self.parser.intent = {'action': 'select', 'list_id': ident, 'indices': [1], 'lead_minutes': 60}
        self.assertTrue(await self.handle('一小时'))
        history = self.parser.calls[-1][1]['conversation']
        self.assertEqual(history, [
            {'role': 'user', 'content': '第一项帮我设个合适的提前时间'},
            {'role': 'assistant', 'content': '你希望提前半小时还是一小时？'},
        ])
        reminder = (await self.reminders())[0]
        self.assertEqual(reminder['lead_minutes'], 60)
        self.assertTrue(all(not window['turns'] for window in await self.windows()))
        self.assertIsNotNone(await self.quote_for(self.recipient + '/' + first, 'reply'))

    async def test_followup_quote_keeps_context_after_message_quota(self):
        ident = await self.active_list()
        first = await self.ask()
        await self.drain()
        receipt = await self.quote_for(self.recipient + '/' + first, 'reply')
        self.parser.intent = {'action': 'ignore'}
        for _ in range(3):
            await self.handle('闲聊')
        self.parser.intent = {'action': 'select', 'list_id': ident, 'indices': [1], 'lead_minutes': 60}
        self.assertTrue(await self.handle('一小时', quote=receipt))
        self.assertEqual(len(self.parser.calls[-1][1]['conversation']), 2)
        self.assertEqual(len(await self.reminders()), 1)

    async def test_expired_followup_quote_never_borrows_new_context(self):
        ident = await self.active_list()
        first = await self.ask()
        await self.drain()
        receipt = await self.quote_for(self.recipient + '/' + first, 'reply')
        self.parser.intent = {'action': 'ignore'}
        for _ in range(3):
            await self.handle('闲聊')
        record = await self.store.call(lambda db: entity(db, 'list', ident))
        calls = len(self.parser.calls)
        with patch('digest.interactions.utcnow', return_value=parse_time(record['expires_at'])):
            self.assertTrue(await self.handle('一小时', quote=receipt))
        self.assertEqual(len(self.parser.calls), calls)
        self.assertFalse(await self.reminders())

    async def test_another_recipients_quote_or_unrelated_quote_cannot_enter(self):
        ident = await self.active_list()
        receipt = await self.quote_for(ident, 'inquiry')
        self.assertFalse(await self.interactions.handle('bot:FriendMessage:987', uid(), '第一项', receipt))
        self.assertFalse(await self.handle('回复别的话题', quote='not-a-plugin-message'))
        self.assertFalse(self.parser.calls)

    async def test_settings_defaults_upgrade_validation_and_snapshot(self):
        await self.store.call(lambda db: db.execute('UPDATE settings SET data=?',
            (json.dumps({'concurrency': 2, 'retention_days': 30, 'text_limit': 1500,
                         'send_interval': 1, 'generation_timeout': 1200, 'call_budget': 64,
                         'reply_window_minutes': 5}),)))
        await self.store.initialize()
        settings = await self.store.settings()
        self.assertNotIn('reply_window_minutes', settings)
        self.assertEqual(settings['reply_message_limit'], 3)
        self.assertEqual(settings['concurrency'], 2)
        for value in (0, -1, True, '3'):
            with self.assertRaises(ValueError):
                await self.store.settings({'reply_message_limit': value})
        with self.assertRaisesRegex(ValueError, '未知全局设置'):
            await self.store.settings({'reply_window_minutes': 5})
        await self.formal(candidates=[self.candidate()])
        await self.runtime.deliver_one()
        await self.store.settings({'reply_message_limit': 7})
        await self.drain()
        first = (await self.windows())[0]
        self.assertEqual(first['remaining'], 3)
        self.assertNotIn('expires_at', first)
        self.assertNotIn('window_minutes', first)
        await self.ask()
        await self.drain()
        second = next(item for item in await self.windows() if item['channel'] == 'reply')
        self.assertEqual(second['remaining'], 7)
        self.assertNotIn('expires_at', second)
        self.parser.intent = {'action': 'ignore'}
        calls = len(self.parser.calls)
        for _ in range(8):
            await self.handle('使用自定义条数')
        self.assertEqual(len(self.parser.calls) - calls, 7)

    async def test_upgrade_removes_only_timed_fields_and_preserves_quota_and_history(self):
        ident = await self.active_list()
        await self.ask()
        await self.drain()
        original = next(item for item in await self.windows() if item['channel'] == 'reply')
        saved_list = await self.store.call(lambda db: entity(db, 'list', ident))
        samples = []
        for state, remaining in [('active', 2), ('active', 0), ('closed', 2), ('expired', 2), ('awaiting_delivery', 7)]:
            record = {**copy.deepcopy(original), 'id': uid(), 'state': state, 'remaining': remaining,
                      'window_minutes': 5, 'expires_at': (utcnow() - timedelta(minutes=1)).isoformat()}
            if state == 'awaiting_delivery':
                record.update(opened_at=None, expires_at=None)
            samples.append(record)
        settings = await self.store.settings({'reply_message_limit': 7})
        def old_schema(db):
            db.execute('DELETE FROM schema_version WHERE version=4')
            db.execute('UPDATE settings SET data=?', (json.dumps({**settings, 'reply_window_minutes': 5}),))
            for record in samples:
                put(db, 'dialogue', record)
        await self.store.call(old_schema)
        await self.store.initialize()
        for before in samples:
            after = await self.store.call(lambda db: entity(db, 'dialogue', before['id']))
            self.assertEqual(after, {k: v for k, v in before.items() if k not in ('window_minutes', 'expires_at')})
        self.assertEqual(await self.store.settings(), settings)
        self.assertEqual(await self.store.call(lambda db: entity(db, 'list', ident)), saved_list)
        self.assertEqual(await self.store.call(lambda db: db.execute('SELECT MAX(version) FROM schema_version').fetchone()[0]), 4)

    async def test_restart_keeps_history_delivery_time_and_remaining_quota(self):
        await self.active_list()
        await self.ask()
        await self.drain()
        self.parser.intent = {'action': 'ignore'}
        await self.handle('一条闲聊')
        before = next(item for item in await self.windows() if item['channel'] == 'reply')
        late = parse_time(before['opened_at']) + timedelta(hours=1)
        await self.store.initialize()
        await self.runtime.reconcile(now=late, due=False)
        self.interactions = Interactions(self.store, self.parser, self.runtime)
        with patch('digest.interactions.utcnow', return_value=late):
            await self.handle('继续闲聊')
        after = next(item for item in await self.windows() if item['channel'] == 'reply')
        self.assertEqual(after['opened_at'], before['opened_at'])
        self.assertEqual(after['remaining'], before['remaining'] - 1)
        self.assertEqual(after['turns'], before['turns'])
        self.assertEqual(after['state'], 'active')

    async def test_delayed_concurrent_messages_cannot_exceed_quota(self):
        await self.active_list()
        end = parse_time((await self.windows())[0]['opened_at']) + timedelta(hours=1)
        with patch('digest.interactions.utcnow', return_value=end):
            await asyncio.gather(*(self.handle(f'闲聊 {i}') for i in range(4)))
        self.assertEqual(len(self.parser.calls), 3)

    async def test_immediate_concurrent_messages_cannot_exceed_quota(self):
        await self.active_list()
        await asyncio.gather(*(self.handle(f'闲聊 {i}') for i in range(4)))
        self.assertEqual(len(self.parser.calls), 3)
        self.assertEqual((await self.windows())[0]['remaining'], 0)

    async def test_optional_parse_failure_passes_through_without_reply(self):
        await self.active_list()
        sent = len(self.sender.calls)
        self.parser.intent = TimeoutError()
        self.assertFalse(await self.handle('只是闲聊'))
        await self.drain()
        self.assertEqual(len(self.sender.calls), sent)
        self.assertTrue(await self.handle('/日程 第一项'))
        await self.drain()
        self.assertEqual(len(self.sender.calls), sent + 1)

    async def test_dismiss_closes_window_without_cancelling_reminders(self):
        await self.active_list()
        self.parser.intent = {'action': 'dismiss'}
        self.assertTrue(await self.handle('先不选了'))
        self.assertFalse(await self.handle('普通聊天'))
        self.assertEqual(len(self.parser.calls), 1)

    async def test_superseded_list_invalidates_followup(self):
        await self.active_list()
        first = await self.ask()
        await self.drain()
        receipt = await self.quote_for(self.recipient + '/' + first, 'reply')
        await self.active_list(candidate=self.candidate(title='新的事项'))
        calls = len(self.parser.calls)
        self.assertTrue(await self.handle('一小时', quote=receipt))
        self.assertEqual(len(self.parser.calls), calls)
        self.assertFalse(await self.reminders())

    async def test_two_followup_rounds_keep_original_request_and_latest_question(self):
        ident = await self.active_list()
        await self.ask('你要选择哪一项？')
        await self.drain()
        self.parser.intent = {'action': 'ask', 'question': '第一项提前多久提醒？'}
        await self.handle('第一项')
        await self.drain()
        self.parser.intent = {'action': 'select', 'list_id': ident, 'indices': [1], 'lead_minutes': 60}
        await self.handle('一小时')
        turns = self.parser.calls[-1][1]['conversation']
        self.assertEqual([turn['content'] for turn in turns], [
            '第一项帮我设个合适的提前时间', '你要选择哪一项？', '第一项', '第一项提前多久提醒？'])
        self.assertEqual((await self.reminders())[0]['lead_minutes'], 60)

    async def test_business_time_clarification_can_be_answered_without_repeating_id(self):
        ident = await self.active_list(candidate=self.candidate(hours=0.1))
        self.parser.intent = {'action': 'select', 'list_id': ident, 'indices': [1]}
        await self.handle('第一项')
        self.assertFalse(await self.reminders())
        await self.drain()
        change = (await self.store.call(lambda db: entities(db, 'change')))[0]
        self.parser.intent = {'action': 'confirm', 'change_id': change['id'], 'immediate': True}
        await self.handle('现在提醒')
        self.assertEqual(len(await self.reminders()), 1)
        self.assertIn('提醒时间已过', self.parser.calls[-1][1]['conversation'][-1]['content'])

    async def test_exhausted_quota_keeps_history_for_explicit_id_after_restart(self):
        ident = await self.active_list()
        await self.ask()
        await self.drain()
        self.parser.intent = {'action': 'ignore'}
        for _ in range(3):
            await self.handle('闲聊')
        before = next(item for item in await self.windows() if item['channel'] == 'reply')
        await self.store.initialize()
        late = parse_time(before['opened_at']) + timedelta(hours=1)
        await self.runtime.reconcile(now=late, due=False)
        calls = len(self.parser.calls)
        with patch('digest.interactions.utcnow', return_value=late):
            self.assertFalse(await self.handle('没有引用也没有编号'))
            self.assertEqual(len(self.parser.calls), calls)
            self.parser.intent = {'action': 'select', 'list_id': ident, 'indices': [1], 'lead_minutes': 60}
            self.assertTrue(await self.handle(f'清单 {ident[:8]}'))
        self.assertEqual(self.parser.calls[-1][1]['conversation'], before['turns'])
        self.assertEqual((await self.reminders())[0]['lead_minutes'], 60)
        record = await self.store.call(lambda db: entity(db, 'list', ident))
        self.assertEqual(record['state'], 'active')

    async def test_delayed_followup_preserves_context_and_accepts_short_answer(self):
        ident = await self.active_list()
        await self.ask()
        await self.drain()
        before = next(item for item in await self.windows() if item['channel'] == 'reply')
        late = parse_time(before['opened_at']) + timedelta(hours=1)
        await self.runtime.reconcile(now=late, due=False)
        after = next(item for item in await self.windows() if item['channel'] == 'reply')
        self.assertEqual(after['turns'], before['turns'])
        self.assertEqual(after['state'], 'active')
        self.parser.intent = {'action': 'select', 'list_id': ident, 'indices': [1], 'lead_minutes': 60}
        with patch('digest.interactions.utcnow', return_value=late):
            self.assertTrue(await self.handle('一小时'))
        self.assertEqual(self.parser.calls[-1][1]['conversation'], before['turns'])
        self.assertEqual((await self.reminders())[0]['lead_minutes'], 60)

    async def test_expired_business_list_stops_unused_message_allowance(self):
        ident = await self.active_list()
        await self.ask()
        await self.drain()
        record = await self.store.call(lambda db: entity(db, 'list', ident))
        late = parse_time(record['expires_at'])
        calls = len(self.parser.calls)
        with patch('digest.interactions.utcnow', return_value=late):
            self.assertFalse(await self.handle('一小时'))
        await self.runtime.reconcile(now=late, due=False)
        after = next(item for item in await self.windows() if item['channel'] == 'reply')
        self.assertEqual(after['remaining'], 3)
        self.assertEqual(after['state'], 'expired')
        self.assertEqual(after['turns'], [])
        self.assertEqual(len(self.parser.calls), calls)

    async def test_already_expired_window_is_not_reopened_by_unused_allowance(self):
        await self.active_list()
        window = (await self.windows())[0]
        await self.store.call(lambda db: dialogue.close(db, [window['id']], 'expired'))
        await self.runtime.reconcile(due=False)
        self.assertFalse(await self.handle('第一项'))
        self.assertEqual((await self.windows())[0]['remaining'], 3)
        self.assertFalse(self.parser.calls)

    async def test_message_received_before_question_opened_cannot_use_allowance(self):
        await self.active_list()
        before = parse_time((await self.windows())[0]['opened_at']) - timedelta(seconds=1)
        with patch('digest.interactions.utcnow', return_value=before):
            self.assertFalse(await self.handle('先前排队中的消息'))
        self.assertFalse(self.parser.calls)
        self.assertEqual((await self.windows())[0]['remaining'], 3)

    async def test_explicit_operation_does_not_close_another_tasks_window(self):
        one = await self.active_list()
        task = await self.store.save_task(config(name='second'))
        task = await self.store.set_enabled(task['id'], task['version'], True)
        two = await self.active_list(candidate=self.candidate(title='第二个任务的事项'), task=task)
        self.parser.intent = {'action': 'select', 'list_id': one, 'indices': [1]}
        await self.handle(f'清单 {one[:8]} 第一项')
        window = next(item for item in await self.windows() if item['owner'] == two)
        self.assertEqual(window['state'], 'active')
        self.assertEqual(window['remaining'], 3)
        self.parser.intent = {'action': 'ignore'}
        await self.handle('刚才的普通聊天')
        self.assertEqual([item['id'] for item in self.parser.calls[-1][1]['list']], [two])

    async def test_missing_old_quote_target_is_not_reinterpreted_as_new_list(self):
        one = await self.active_list()
        receipt = await self.quote_for(one, 'inquiry')
        await self.store.call(lambda db: db.execute("DELETE FROM entities WHERE kind='list' AND id=?", (one,)))
        two = await self.active_list(candidate=self.candidate(title='new'))
        self.parser.intent = {'action': 'select', 'list_id': two, 'indices': [1]}
        self.assertTrue(await self.handle('第一项', quote=receipt))
        self.assertFalse(self.parser.calls)
        self.assertFalse(await self.reminders())

    async def test_python_disambiguation_question_carries_request_to_id_only_answer(self):
        one = await self.active_list()
        task = await self.store.save_task(config(name='second'))
        task = await self.store.set_enabled(task['id'], task['version'], True)
        await self.active_list(candidate=self.candidate(title='second item'), task=task)
        self.parser.intent = {'action': 'select', 'list_id': one, 'indices': [1]}
        await self.handle('第一项，提前一小时')
        self.assertFalse(await self.reminders())
        await self.drain()
        self.parser.intent = {'action': 'select', 'list_id': one, 'indices': [1], 'lead_minutes': 60}
        await self.handle(f'清单 {one[:8]}')
        history = self.parser.calls[-1][1]['conversation']
        self.assertEqual(history[0]['content'], '第一项，提前一小时')
        self.assertIn('多份候选清单', history[1]['content'])
        self.assertEqual((await self.reminders())[0]['lead_minutes'], 60)


class InterpreterTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_gets_scoped_history_but_evidence_must_be_in_current_reply(self):
        class Model:
            async def prepare(self, task):
                return {'window': 20000}

            async def generate(self, prepared, prompt, system):
                self.prompt, self.system = json.loads(prompt), system
                return json.dumps({'action': 'select', 'list_id': 'list', 'indices': [1],
                                   'lead_minutes': 60, 'evidence': self.evidence}), 'fake'
        model = Model()
        parser = Interpreter(model)
        history = [{'role': 'user', 'content': '第一项'}, {'role': 'assistant', 'content': '提前多久？'}]
        model.evidence = '第一项'
        with self.assertRaises(ValueError):
            await parser.parse(config().to_dict(), '一小时', {'conversation': history})
        model.evidence = '一小时'
        intent = await parser.parse(config().to_dict(), '一小时', {'conversation': history})
        self.assertEqual(intent['lead_minutes'], 60)
        self.assertEqual(model.prompt['context']['conversation'], history)
        self.assertIn('助手的提问不是用户授权', model.system)
