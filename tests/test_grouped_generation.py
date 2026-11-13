import json
import unittest
from datetime import timedelta
from types import SimpleNamespace

from digest.adapters import NativeHistory
from digest.domain import utcnow
from digest.generation import Generator
from test_core import config


class History:
    def __init__(self, messages, names=None):
        self.messages = messages
        self.names = names or {}

    async def read(self, groups, start, end):
        return self.messages, [{'group': group, 'group_name': self.names.get(group),
            'count': sum(message['group'] == group for message in self.messages), 'note': '覆盖未知'}
            for group in groups]


class Models:
    def __init__(self, fact_size=16, final=None):
        self.calls = []
        self.fact_size = fact_size
        self.final = final
        self.window = 8192

    async def prepare(self, task):
        return {'window': self.window, 'persona': '任务选定的人格', 'warnings': []}

    async def generate(self, prepared, prompt, system):
        data = json.loads(prompt.split('\n资料：\n', 1)[1])
        self.calls.append((data, prompt, system))
        if data['phase'] == 'group_messages':
            value = {'facts': [{'text': 'F' * self.fact_size, 'sources': [item['id']]}
                               for item in data['messages']]}
        elif data['phase'] == 'group_facts':
            value = {'facts': [{'text': '群内压缩', 'sources': list(dict.fromkeys(
                source for item in data['facts'] for source in item['sources']))}]}
        elif data['phase'] == 'cross_reduce':
            value = {'groups': []}
            for item in data['groups']:
                sources = list(dict.fromkeys(source for fact in item['facts'] for source in fact['sources']))
                value['groups'].append({'umo': item['group']['umo'],
                                        'facts': [{'text': '群级压缩', 'sources': sources}]})
        else:
            value = self.final(data) if self.final else {'summary': '多群简报', 'candidates': []}
        return json.dumps(value, ensure_ascii=False), 'fake'


def message(group, ident, text='通知', offset=0):
    return {'group': group, 'id': ident, 'text': text,
            'time': (utcnow() + timedelta(seconds=offset)).isoformat()}


class GroupedGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def run_digest(self, messages, names=None, model=None, **settings):
        groups = list(dict.fromkeys(item['group'] for item in messages))
        model = model or Models()
        result = await Generator(model, History(messages, names)).run(
            config(groups=groups), utcnow(), {'call_budget': 64, **settings})
        return result, model

    async def test_interleaved_groups_are_read_separately_then_merged_with_labels(self):
        a, b = 'bot:GroupMessage:101', 'bot:GroupMessage:202'
        rows = [message(a, 'bot/a2', offset=2), message(b, 'bot/b1', offset=1), message(a, 'bot/a1', offset=0)]
        result, model = await self.run_digest(rows, {a: '课程通知群', b: '社团群'})
        batches = [data for data, _, _ in model.calls if data['phase'] == 'group_messages']
        self.assertEqual(len(batches), 2)
        self.assertEqual([item['id'] for item in batches[0]['messages']], ['bot/a1', 'bot/a2'])
        self.assertEqual(batches[0]['group'], {'umo': a, 'id': '101', 'name': '课程通知群', 'platform': 'bot'})
        self.assertEqual([item['id'] for item in batches[1]['messages']], ['bot/b1'])
        for batch in batches:
            for item in batch['messages']:
                self.assertEqual(set(item), {'id', 'time', 'text'})
        final = model.calls[-1][0]
        self.assertEqual(final['phase'], 'final')
        self.assertEqual([item['group']['name'] for item in final['groups']], ['课程通知群', '社团群'])
        self.assertTrue(all('facts' in item and 'messages' not in item for item in final['groups']))
        self.assertEqual(result['logical_calls'], 3)
        self.assertEqual(result['summary'], '多群简报')
        self.assertEqual(result['coverage'][0]['group_name'], '课程通知群')

    async def test_one_small_group_uses_one_independent_request(self):
        group = 'bot:GroupMessage:101'
        result, model = await self.run_digest([message(group, 'bot/1')], {group: '单群'})
        self.assertEqual(result['logical_calls'], 1)
        self.assertEqual(model.calls[0][0]['groups'][0]['group']['name'], '单群')
        self.assertEqual(set(model.calls[0][0]['groups'][0]['messages'][0]), {'id', 'time', 'text'})

    async def test_long_group_only_splits_itself_and_preserves_escaped_text(self):
        a, b = 'bot:GroupMessage:101', 'bot:GroupMessage:202'
        body = '长消息"\\\n\x01' * 3000
        rows = [message(a, 'bot/long', body), message(b, 'bot/short', '短消息')]
        _, model = await self.run_digest(rows, {a: '课程通知群', b: '工作任务群'})
        batches = [data for data, _, _ in model.calls if data['phase'] == 'group_messages']
        long = [batch for batch in batches if batch['group']['umo'] == a]
        short = [batch for batch in batches if batch['group']['umo'] == b]
        self.assertGreater(len(long), 1)
        self.assertEqual(len(short), 1)
        self.assertTrue(all(batch['group']['name'] == '课程通知群' for batch in long))
        self.assertEqual(short[0]['group']['name'], '工作任务群')
        self.assertEqual(''.join(item['text'] for batch in long for item in batch['messages']), body)
        for data, prompt, system in model.calls:
            self.assertLessEqual(len((prompt + system).encode()), model.window - max(1024, model.window // 4))
            if data['phase'] == 'group_messages':
                self.assertTrue(all(item['id'] == ('bot/long' if data['group']['umo'] == a else 'bot/short')
                                    for item in data['messages']))

    async def test_large_group_fact_set_is_reduced_before_cross_group_stage(self):
        a, b = 'bot:GroupMessage:101', 'bot:GroupMessage:202'
        rows = [message(a, f'bot/a{i}', offset=i) for i in range(8)] + [message(b, 'bot/b')]
        _, model = await self.run_digest(rows, model=Models(fact_size=700))
        stages = [data for data, _, _ in model.calls if data['phase'] == 'group_facts']
        self.assertTrue(stages)
        self.assertTrue(all(data['group']['umo'] == a for data in stages))

    async def test_many_group_summaries_are_compressed_without_losing_labels(self):
        rows = [message(f'bot:GroupMessage:{i}', f'bot/{i}') for i in range(101, 109)]
        names = {row['group']: f'群名-{i}' for i, row in enumerate(rows)}
        result, model = await self.run_digest(rows, names, Models(fact_size=1000))
        reductions = [data for data, _, _ in model.calls if data['phase'] == 'cross_reduce']
        self.assertTrue(reductions)
        final = model.calls[-1][0]
        self.assertEqual({item['group']['umo']: item['group']['name'] for item in final['groups']}, names)
        self.assertGreater(result['cross_group_calls'], 1)
        self.assertTrue(all('messages' not in item for data in reductions for item in data['groups']))

    async def test_missing_name_uses_group_number_without_inventing_a_name(self):
        result, model = await self.run_digest([message('bot:GroupMessage:101', 'bot/1')])
        self.assertIsNone(model.calls[-1][0]['groups'][0]['group']['name'])
        self.assertEqual(result['summary'], '多群简报')
        self.assertEqual(result['groups'][0]['id'], '101')

    async def test_final_style_keeps_persona_and_hides_internal_source_labels(self):
        result, model = await self.run_digest([message('bot:GroupMessage:101', 'bot/1')])
        _, prompt, system = model.calls[-1]
        self.assertIn('表达必须遵循所选人格', system)
        self.assertIn('任务选定的人格', system)
        self.assertIn('不展示群号', system)
        self.assertIn('不编造采纳数量', prompt)
        self.assertNotIn('可选语气', system)
        self.assertNotIn('覆盖说明', result['summary'])

    async def test_candidate_condition_is_preserved_separately_from_activity_title(self):
        title = '学院迎新活动到场签到'
        condition = '这项安排面向名单内同学，请先核对是否参加。'
        model = Models(final=lambda data: {'summary': title, 'candidates': [{
            'title': title, 'event_at': (utcnow() + timedelta(hours=1)).isoformat(),
            'condition': condition, 'sources': ['bot/1']}]})
        result, _ = await self.run_digest([message('bot:GroupMessage:101', 'bot/1')], model=model)
        self.assertEqual(result['candidates'][0]['title'], title)
        self.assertEqual(result['candidates'][0]['condition'], condition)
        self.assertEqual(result['candidates'][0]['source_groups'][0]['id'], '101')

    async def test_invalid_candidate_condition_is_not_silently_dropped(self):
        for invalid in (['名单内'], '长' * 301):
            model = Models(final=lambda data: {'summary': '活动', 'candidates': [{
                'title': '活动签到', 'event_at': (utcnow() + timedelta(hours=1)).isoformat(),
                'condition': invalid, 'sources': ['bot/1']}]})
            with self.assertRaisesRegex(ValueError, '适用条件'):
                await self.run_digest([message('bot:GroupMessage:101', 'bot/1')], model=model)

    async def test_partial_history_is_disclosed_to_final_model_without_count_claims(self):
        class Partial(History):
            async def read(self, groups, start, end):
                rows, coverage = await super().read(groups, start, end)
                coverage[1]['read'] = 'failed'
                return rows, coverage
        a, b = 'bot:GroupMessage:101', 'bot:GroupMessage:202'
        model = Models()
        result = await Generator(model, Partial([message(a, 'bot/1')], {b: '社团群'})).run(
            config(groups=[a, b]), utcnow(), {'call_budget': 64})
        self.assertIn('本次未能读取：社团群', model.calls[-1][1])
        self.assertIn('最多一句', model.calls[-1][1])
        self.assertEqual(result['coverage'][1]['read'], 'failed')
        self.assertEqual(result['summary'], '多群简报')

    async def test_same_group_number_on_different_platforms_stays_distinct(self):
        rows = [message('one:GroupMessage:101', 'one/1'), message('two:GroupMessage:101', 'two/1')]
        _, model = await self.run_digest(rows)
        final = model.calls[-1][0]
        self.assertEqual({doc['group']['platform'] for doc in final['groups']}, {'one', 'two'})
        self.assertEqual(len(final['groups']), 2)

    async def test_cross_group_source_in_group_fact_is_rejected(self):
        class Wrong(Models):
            async def generate(self, prepared, prompt, system):
                return json.dumps({'facts': [{'text': 'wrong', 'sources': ['bot/b']}]}), 'fake'
        rows = [message('bot:GroupMessage:101', 'bot/a'), message('bot:GroupMessage:202', 'bot/b')]
        with self.assertRaisesRegex(ValueError, '其他群'):
            await self.run_digest(rows, model=Wrong())

    async def test_final_candidate_source_groups_are_attached_by_code(self):
        def final(data):
            return {'summary': '两群提到同一活动', 'candidates': [{'title': '活动',
                'event_at': (utcnow()+timedelta(hours=2)).isoformat(), 'sources': ['bot/a', 'bot/b'], 'place': ''}]}
        rows = [message('bot:GroupMessage:101', 'bot/a'), message('bot:GroupMessage:202', 'bot/b')]
        result, _ = await self.run_digest(rows, model=Models(final=final))
        self.assertEqual([group['id'] for group in result['candidates'][0]['source_groups']], ['101', '202'])

    async def test_empty_group_facts_do_not_trigger_a_fabricated_final_summary(self):
        class Empty(Models):
            async def generate(self, prepared, prompt, system):
                self.calls.append(prompt)
                return '{"facts":[]}', 'fake'
        rows = [message('bot:GroupMessage:101', 'bot/a'), message('bot:GroupMessage:202', 'bot/b')]
        result, model = await self.run_digest(rows, model=Empty())
        self.assertEqual(result['summary'], '')
        self.assertEqual(len(model.calls), 2)

    async def test_call_budget_stops_before_sending_partial_group_result(self):
        rows = [message('bot:GroupMessage:101', 'bot/a'), message('bot:GroupMessage:202', 'bot/b')]
        with self.assertRaisesRegex(RuntimeError, '调用预算'):
            await self.run_digest(rows, call_budget=1)

    async def test_duplicate_source_id_is_rejected_before_model_calls(self):
        rows = [message('bot:GroupMessage:101', 'bot/a'), message('bot:GroupMessage:202', 'bot/a')]
        model = Models()
        with self.assertRaisesRegex(ValueError, '重复'):
            await self.run_digest(rows, model=model)
        self.assertFalse(model.calls)

    async def test_cross_group_reduction_cannot_drop_a_group(self):
        class Missing(Models):
            async def generate(self, prepared, prompt, system):
                output, model = await super().generate(prepared, prompt, system)
                if self.calls[-1][0]['phase'] == 'cross_reduce':
                    value = json.loads(output)
                    value['groups'].pop()
                    output = json.dumps(value)
                return output, model
        rows = [message(f'bot:GroupMessage:{i}', f'bot/{i}') for i in range(101, 109)]
        with self.assertRaisesRegex(ValueError, '遗漏'):
            await self.run_digest(rows, model=Missing(fact_size=1000))

    async def test_cross_group_reduction_that_does_not_shrink_stops(self):
        class Same(Models):
            async def generate(self, prepared, prompt, system):
                output, model = await super().generate(prepared, prompt, system)
                data = self.calls[-1][0]
                if data['phase'] == 'cross_reduce':
                    output = json.dumps({'groups': [{'umo': item['group']['umo'], 'facts': item['facts']}
                                                   for item in data['groups']]})
                return output, model
        rows = [message(f'bot:GroupMessage:{i}', f'bot/{i}') for i in range(101, 109)]
        with self.assertRaisesRegex(RuntimeError, '未缩小'):
            await self.run_digest(rows, model=Same(fact_size=1000))

    async def test_blank_messages_do_not_trigger_a_model_call(self):
        result, model = await self.run_digest([message('bot:GroupMessage:101', 'bot/1', ' \n ')])
        self.assertEqual(result['summary'], '')
        self.assertFalse(model.calls)


class GroupLabelsTests(unittest.IsolatedAsyncioTestCase):
    async def test_names_are_fetched_once_per_platform(self):
        class Bot:
            def __init__(self): self.calls = []
            async def call_action(self, action):
                self.calls.append(action)
                return [{'group_id': 101, 'group_name': '课程群'}, {'group_id': 202, 'group_name': '社团群'}]
        bot = Bot()
        platform = SimpleNamespace(meta=lambda: SimpleNamespace(id='bot', name='aiocqhttp'), bot=bot)
        history = NativeHistory(SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[platform])))
        labels = await history.group_labels(['bot:GroupMessage:101', 'bot:GroupMessage:202'])
        self.assertEqual(bot.calls, ['get_group_list'])
        self.assertEqual(labels['bot:GroupMessage:101']['group_name'], '课程群')
        self.assertEqual(labels['bot:GroupMessage:202']['group_name'], '社团群')

    async def test_unavailable_platform_preserves_number_and_records_warning(self):
        labels = await NativeHistory(SimpleNamespace()).group_labels(['missing:GroupMessage:101'])
        self.assertEqual(labels['missing:GroupMessage:101']['group_id'], '101')
        self.assertIsNone(labels['missing:GroupMessage:101']['group_name'])
        self.assertIn('metadata_warning', labels['missing:GroupMessage:101'])
