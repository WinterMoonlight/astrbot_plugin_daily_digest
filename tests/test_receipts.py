import json
import unittest
from types import SimpleNamespace

from digest.adapters import NativeModels
from digest.domain import TaskConfig
from digest.receipts import LABELS, ReplyRenderer, receipt_item
import test_native_reminders as fixtures


def record(**overrides):
    return {'id':'reminder-1','title':'课程说明会','event_at':'2030-11-16T15:00:00+08:00',
            'remind_at':'2030-11-16T14:30:00+08:00','timezone':'Asia/Shanghai',
            'state':'scheduled','native_job_id':'native-1','native_enabled':True,
            'native_run_once':True, **overrides}


class Models:
    def __init__(self, result=None, fail=False):
        self.calls=[]
        self.result=result or json.dumps({'opening':'主人，这次的日程明细在这里喵：',
                                         'closing':'喵～','labels':LABELS},ensure_ascii=False)
        self.fail=fail

    async def prepare(self, task):
        self.task=task
        return {'persona':'用猫娘的语气称呼用户为主人','window':16384}

    async def generate(self, prepared, prompt, system):
        self.calls.append((json.loads(prompt),system))
        if self.fail:
            raise TimeoutError()
        return self.result,'fake'


class ReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_persona_and_task_prompt_both_constrain_wording(self):
        models=Models()
        renderer=ReplyRenderer(models)
        text=await renderer.render(TaskConfig(prompt='简洁表达，使用中文').to_dict(),[record()],
                                   'select',['reminder-1'])
        self.assertIn('主人',text)
        self.assertIn('2030-11-16 15:00:00',text)
        self.assertIn('2030-11-16 14:30:00',text)
        self.assertIn('Asia/Shanghai',text)
        self.assertIn('UTC+0800',text)
        self.assertIn('已新建原生提醒',text)
        self.assertIn('用猫娘的语气',models.calls[0][1])
        self.assertIn('简洁表达，使用中文',models.calls[0][1])
        self.assertEqual(models.calls[0][0]['purpose'],'reminder_receipt_style')

    async def test_failed_style_falls_back_to_complete_facts(self):
        text=await ReplyRenderer(Models(fail=True)).render({},[record()],'modify')
        self.assertIn('本次日程处理结果',text)
        self.assertIn('已修改原生提醒',text)
        self.assertIn('2030-11-16 14:30:00',text)
        self.assertIn('已启用',text)

    async def test_model_cannot_swap_labels_or_claim_everything_succeeded(self):
        for style in (
            {'opening':'全部设置成功了','closing':'','labels':LABELS},
            {'opening':'主人','closing':'','labels':{**LABELS,'event_time':'提醒时间'}},
            {'opening':'主人','closing':'','labels':{**LABELS,'state':''}},
        ):
            models=Models(json.dumps(style,ensure_ascii=False))
            text=await ReplyRenderer(models).render({},[record()],'select',['reminder-1'])
            self.assertTrue(text.startswith('本次日程处理结果'))
            self.assertIn('事项时间：2030-11-16 15:00:00',text)

    async def test_reused_paused_task_shows_actual_time_and_original_request(self):
        row=record(native_link_mode='reused',native_enabled=False,state='native_paused',
                   native_reused_requested_at='2030-11-16T14:00:00+08:00')
        text=await ReplyRenderer(Models()).render({},[row],'native_conflict')
        self.assertIn('已复用原生任务',text)
        self.assertIn('已停用，不会自动提醒',text)
        self.assertIn('实际提醒：2030-11-16 14:30:00',text)
        self.assertIn('原计划提醒：2030-11-16 14:00:00',text)
        self.assertIn('未来任务',text)

    async def test_pending_failed_and_cancelled_are_not_reported_as_active(self):
        pending=receipt_item(record(native_job_id=None,state='awaiting_native_choice'),'select',[])
        self.assertEqual(pending['state'],'未创建')
        self.assertIn('尚未生效',pending['reminder_time'])
        failed=receipt_item(record(state='cancelled',native_error='取消结果未确认'),'cancel',[])
        self.assertIn('尚未确认',failed['reminder_time'])
        cancelled=receipt_item(record(state='cancelled'),'cancel',[])
        self.assertEqual(cancelled['reminder_time'],'无待执行计划')
        legacy=receipt_item(record(state='failed',native_job_id=None),'select',[])
        self.assertEqual(legacy['state'],'旧版投递失败')
        self.assertNotIn('已新建',legacy['outcome'])

    async def test_recurring_reuse_never_labels_requested_time_as_actual(self):
        row=record(native_link_mode='reused',native_run_once=False,native_schedule='30 14 * * *')
        result=receipt_item(row,'native_conflict',[])
        self.assertIn('周期计划 30 14 * * *',result['reminder_time'])
        self.assertNotIn('2030-11-16',result['reminder_time'])

    async def test_unmatched_explanations_are_preserved(self):
        text=await ReplyRenderer(Models()).render({},[record()],'select',['reminder-1'],
            '已添加 课程说明会\n旧活动：事项已开始，未添加')
        self.assertIn('旧活动：事项已开始，未添加',text)


class PersonaTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_plugin_persona_wins_without_reading_conversation(self):
        class Manager:
            async def get_persona(self, ident):
                self.ident=ident
                return SimpleNamespace(system_prompt='指定的人格')
        manager=Manager()
        models=NativeModels(SimpleNamespace(persona_manager=manager))
        persona,warnings=await models.resolve_persona(TaskConfig(persona_id='selected'))
        self.assertEqual(persona,'指定的人格')
        self.assertFalse(warnings)
        self.assertEqual(manager.ident,'selected')

    async def test_blank_plugin_persona_resolves_native_session_override(self):
        class Conversations:
            async def get_curr_conversation_id(self, umo): return 'existing-conversation'
            async def get_conversation(self, umo, cid):
                return SimpleNamespace(persona_id='session-persona')
        class Manager:
            async def resolve_selected_persona(self, **kwargs):
                self.kwargs=kwargs
                return 'forced-persona',{'prompt':'原生最终人格'},'forced-persona',False
        manager=Manager()
        models=NativeModels(SimpleNamespace(persona_manager=manager,conversation_manager=Conversations()))
        persona,warnings=await models.resolve_persona(TaskConfig(recipient='bot:FriendMessage:1'))
        self.assertEqual(persona,'原生最终人格')
        self.assertFalse(warnings)
        self.assertEqual(manager.kwargs['conversation_persona_id'],'session-persona')
        self.assertEqual(manager.kwargs['umo'],'bot:FriendMessage:1')

    async def test_no_existing_conversation_is_not_created(self):
        class Conversations:
            async def get_curr_conversation_id(self, umo): return None
            async def new_conversation(self, *args): raise AssertionError('must not create')
        class Manager:
            async def resolve_selected_persona(self, **kwargs):
                assert kwargs['conversation_persona_id'] is None
                return None,None,None,False
        models=NativeModels(SimpleNamespace(persona_manager=Manager(),conversation_manager=Conversations()))
        self.assertEqual(await models.resolve_persona(TaskConfig()),('',[]))

    async def test_summary_and_receipt_share_persona_and_read_it_once_per_call(self):
        from digest.generation import Generator
        from digest.domain import utcnow
        from test_core import FakeHistory, config

        class Manager:
            def __init__(self): self.reads=[]
            async def get_persona(self, ident):
                self.reads.append(ident)
                return SimpleNamespace(system_prompt='插件选择的人格')
            async def resolve_selected_persona(self, **kwargs):
                self.reads.append(kwargs['umo'])
                return 'session',{'prompt':'会话默认人格'},None,False

        class Adapter(NativeModels):
            def __init__(self, manager):
                super().__init__(SimpleNamespace(persona_manager=manager))
                self.calls=[]
            async def providers(self, task):
                return [SimpleNamespace(provider_config={'max_context_tokens':16384})],1
            async def generate(self, prepared, prompt, system):
                self.calls.append((prepared['persona'],system))
                if prompt.startswith('{"purpose":"reminder_receipt_style"'):
                    return json.dumps({'opening':'日程明细：','closing':'','labels':LABELS}), 'fake'
                return json.dumps({'summary':'测试简报','candidates':[]}), 'fake'

        for selected,expected in [('chosen','插件选择的人格'),('','会话默认人格')]:
            with self.subTest(selected=selected):
                manager=Manager()
                models=Adapter(manager)
                task=config(persona_id=selected,prompt='表达简洁')
                await Generator(models,FakeHistory()).run(task,utcnow(),{'call_budget':4})
                await ReplyRenderer(models).render(task.to_dict(),[record()],'select',['reminder-1'])
                self.assertEqual([persona for persona,_ in models.calls],[expected,expected])
                self.assertTrue(all(expected in system and '表达简洁' in system for _,system in models.calls))
                self.assertEqual(len(manager.reads),2)


class ReceiptIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown=fixtures.NativeTests.asyncTearDown
    formal=fixtures.NativeTests.formal
    candidate=fixtures.NativeTests.candidate
    drain=fixtures.NativeTests.drain
    active_list=fixtures.NativeTests.active_list
    select=fixtures.NativeTests.select
    reminders=fixtures.NativeTests.reminders

    async def asyncSetUp(self):
        await fixtures.NativeTests.asyncSetUp(self)
        self.models=Models(fail=True)
        self.interactions.renderer=ReplyRenderer(self.models)

    async def test_failed_wording_never_repeats_reminder_creation(self):
        ident=await self.active_list()
        self.parser.intent={'action':'select','list_id':ident,'indices':[1]}
        recipient=self.task['config']['recipient']
        text=f'清单 {ident[:8]} 加入第一项'
        await self.interactions.handle(recipient,'same-message',text)
        await self.interactions.handle(recipient,'same-message',text)
        self.assertEqual(self.manager.calls,1)
        self.assertEqual(len(self.models.calls),1)
        result=await self.store.call(lambda db:db.execute('SELECT result FROM interactions WHERE message_id=?',('same-message',)).fetchone()[0])
        self.assertIn('实际提醒',result)
        self.assertIn('任务状态：已启用',result)
