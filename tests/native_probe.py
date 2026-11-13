"""Run inside the AstrBot container with an isolated ASTRBOT_ROOT.

No live provider, production database, scheduler, or message client is used.
"""
import asyncio
import json
import os
import socket
import tempfile
from pathlib import Path


async def probe(root):
    from astrbot.core.provider.provider import Provider
    from astrbot.core.provider.entities import LLMResponse, ProviderRequest
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.hooks import BaseAgentRunHooks
    from astrbot.core.agent.tool_executor import BaseFunctionToolExecutor
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.cron.manager import CronJobManager
    from astrbot.core.db.sqlite import SQLiteDatabase
    from astrbot.core.platform_message_history_mgr import PlatformMessageHistoryManager
    from aiocqhttp import CQHttp
    from loguru import logger
    logger.disable('astrbot')  # Expected fake failures should not dump long tracebacks.
    from types import SimpleNamespace
    from digest.adapters import NativeModels, NativeHistory, NativeClock, OneBot
    from digest.domain import TaskConfig, utcnow
    from datetime import timedelta

    calls = []
    prompts = []

    class Fake(Provider):
        def __init__(self, name, result, window=8192):
            super().__init__({'id': name, 'type': 'fake', 'max_context_tokens': window,
                              'modalities': ['text']}, {})
            self.result = result
            self.set_model('offline-test')

        def get_current_key(self):
            return ''

        def set_key(self, key):
            pass

        async def get_models(self):
            return ['offline-test']

        async def text_chat(self, **kwargs):
            calls.append((self.provider_config['id'], kwargs.get('request_max_retries')))
            assert not kwargs.get('func_tool')
            if self.result == 'wait':
                await asyncio.Event().wait()
            if isinstance(self.result, Exception):
                raise self.result
            return LLMResponse(role='assistant', completion_text=self.result)

    class NoTools(BaseFunctionToolExecutor):
        @classmethod
        async def execute(cls, *args, **kwargs):
            raise AssertionError('Tool execution is forbidden')
            yield  # pragma: no cover

    async def run(primary, backups):
        runner = ToolLoopAgentRunner()
        await runner.reset(provider=primary, request=ProviderRequest(prompt='test'),
                           run_context=ContextWrapper(context=None), tool_executor=NoTools(),
                           agent_hooks=BaseAgentRunHooks(), streaming=False,
                           fallback_providers=backups, request_max_retries=2)
        async for _ in runner.step_until_done(2):
            pass
        return runner.get_final_llm_resp()

    result = await run(Fake('primary', RuntimeError('simulated failure')),
                       [Fake('backup', 'OK', 4096)])
    assert result.completion_text == 'OK', result
    assert [x[0] for x in calls] == ['primary', 'backup'], calls
    assert all(x[1] == 2 for x in calls), calls
    calls.clear()
    result = await run(Fake('primary', RuntimeError('simulated failure')),
                       [Fake('backup', RuntimeError('simulated failure'))])
    assert result is None or result.role == 'err'
    try:
        await asyncio.wait_for(run(Fake('blocked', 'wait'), []), timeout=0.05)
        raise AssertionError('Cancellation failed')
    except TimeoutError:
        pass

    db = SQLiteDatabase(str(Path(root) / 'probe.db'))
    await db.initialize()
    db.inited = True
    manager = CronJobManager(db)
    ticks = []
    async def tick():
        ticks.append(1)
    job = await manager.add_basic_job(name='digest isolated probe',
        cron_expression='0 0 1 1 *', handler=tick, timezone='Asia/Shanghai',
        payload={}, persistent=False)
    await manager.run_job_now(job.job_id)
    assert ticks == [1], ticks
    await manager.delete_job(job.job_id)
    replacement = await manager.add_basic_job(name='digest reload probe',
        cron_expression='0 0 1 1 *', handler=tick, timezone='Asia/Shanghai')
    await manager.run_job_now(replacement.job_id)
    assert ticks == [1, 1]
    await manager.delete_job(replacement.job_id)
    assert not await manager.list_jobs()

    history = PlatformMessageHistoryManager(db)
    for group in ['probe:GroupMessage:1', 'probe:GroupMessage:2']:
        await history.insert('probe', group, {'message': [{'type': 'plain', 'text': 'fixture'}]})
    rows = await history.get('probe', 'probe:GroupMessage:1')
    assert len(rows) == 1 and rows[0].user_id == 'probe:GroupMessage:1'
    assert rows[0].created_at is not None
    assert callable(CQHttp.call_action)
    class FakeManager:
        async def get_provider_by_id(self, name):
            return {'primary': Fake('primary', RuntimeError('simulated')),
                    'backup': Fake('backup', 'adapter OK', 4096)}.get(name)
    context = SimpleNamespace(provider_manager=FakeManager(),
        get_config=lambda **_: {'agent_runner': {'config': {'model': {
            'fallback_provider_ids': ['backup'], 'request_max_retries': 2}}}})
    models = NativeModels(context)
    task = TaskConfig(provider_id='primary', recipient='probe:FriendMessage:42')
    prepared = await models.prepare(task)
    assert prepared['window'] == 4096
    answer, model = await models.generate(prepared, 'fixture', 'no tools')
    assert answer == 'adapter OK' and model == 'backup'
    snapshot = NativeHistory(SimpleNamespace(message_history_manager=history))
    messages, coverage = await snapshot.read(['probe:GroupMessage:1'],
        utcnow()-timedelta(hours=1), utcnow()+timedelta(seconds=1))
    assert len(messages) == 1 and coverage[0]['completeness'] == 'unknown', coverage
    class GroupNames:
        calls=0
        async def call_action(self, action):
            assert action=='get_group_list'
            self.calls+=1
            return [{'group_id':1,'group_name':'课程群'}, {'group_id':2,'group_name':'社团群'}]
    group_client=GroupNames()
    group_platforms=SimpleNamespace(platform_insts=[SimpleNamespace(
        meta=lambda:SimpleNamespace(id='probe',name='aiocqhttp'),bot=group_client)])
    named_history=NativeHistory(SimpleNamespace(message_history_manager=history,platform_manager=group_platforms))
    grouped_messages,grouped_coverage=await named_history.read(['probe:GroupMessage:2','probe:GroupMessage:1'],
        utcnow()-timedelta(hours=1),utcnow()+timedelta(seconds=1))
    assert [row['group'] for row in grouped_messages]==['probe:GroupMessage:2','probe:GroupMessage:1']
    assert [row['group_name'] for row in grouped_coverage]==['社团群','课程群']
    assert group_client.calls==1
    clock = NativeClock(manager)
    await clock.start(tick)
    old_id = clock.job_id
    await clock.start(tick)
    assert clock.job_id != old_id and len(await manager.list_jobs()) == 1
    await clock.stop()
    assert not await manager.list_jobs()

    class FakeClient:
        mode = 'ok'
        async def call_action(self, action, **kwargs):
            assert action == 'send_private_msg'
            assert kwargs['user_id'] == 42
            assert kwargs['message'] == [{'type': 'text', 'data': {'text': '[CQ:at,qq=all]'}}]
            if self.mode == 'timeout':
                raise TimeoutError()
            if self.mode == 'failed':
                from aiocqhttp.exceptions import ActionFailed
                raise ActionFailed({'retcode': 100, 'status': 'failed'})
            return {'message_id': 99} if self.mode == 'ok' else {}
    client = FakeClient()
    platform = SimpleNamespace(meta=lambda: SimpleNamespace(id='probe', name='aiocqhttp'), bot=client)
    sender = OneBot(SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[platform])))
    for mode, expected in [('ok','acknowledged'), ('timeout','unknown'), ('missing','unknown'), ('failed','failed')]:
        client.mode = mode
        receipt = await sender.send_text('probe:FriendMessage:42', '[CQ:at,qq=all]')
        assert receipt.state == expected, (mode, receipt)
    assert (await sender.send_text('probe:GroupMessage:42', 'wrong target')).state == 'failed'
    # Import the real plugin entry point as a package; use only a synthetic context.
    import importlib.util
    import sys
    source_root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('digest_probe_plugin', source_root / 'main.py',
                                                submodule_search_locations=[str(source_root)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    registrations = []
    response = [json.dumps({'summary':'合成通知','candidates':[{'title':'合成课程',
        'event_at':(utcnow()+timedelta(hours=3)).isoformat(),'sources':[messages[0]['id']],'place':'fixture'}]})]
    class FlowManager:
        async def get_provider_by_id(self, name):
            return Fake(name,RuntimeError('simulated')) if name=='primary' else Fake(name,response[0])
    class Personas:
        async def resolve_selected_persona(self, **kwargs):
            return 'native-persona',{'prompt':'称呼用户为主人，语气温和'},None,False
    plugin_context = SimpleNamespace(register_web_api=lambda *args: registrations.append(args),cron_manager=manager,
        provider_manager=FlowManager(),get_config=context.get_config,message_history_manager=history,
        persona_manager=Personas(),platform_manager=group_platforms)
    plugin = module.DailyDigest(plugin_context)
    await plugin.initialize()
    native_generate=plugin.models.generate
    async def capture_generate(prepared,prompt,system):
        prompts.append(prompt)
        if '"summary"' in prompt[:300]:
            assert prepared['persona']=='称呼用户为主人，语气温和'
            assert prepared['persona'] in system
        override=None
        if '\n资料：\n' in prompt:
            grouped_input=json.loads(prompt.split('\n资料：\n',1)[1])
            if grouped_input['phase']=='group_messages':
                override={'facts':[{'text':'群内合成事实','sources':[row['id']]} for row in grouped_input['messages']]}
        if prompt.startswith('{"purpose":"reminder_receipt_style"'):
            assert '称呼用户为主人' in system and '提取重要通知' in system
            from digest.receipts import LABELS
            override={'opening':'主人，这次的日程明细如下：','closing':'','labels':LABELS}
        if override is not None:
            backups=[(provider,provider.result) for provider in prepared['providers'] if not isinstance(provider.result,Exception)]
            for provider,_ in backups:
                provider.result=json.dumps(override,ensure_ascii=False)
            try:
                return await native_generate(prepared,prompt,system)
            finally:
                for provider,result in backups:
                    provider.result=result
        return await native_generate(prepared,prompt,system)
    plugin.models.generate=capture_generate
    assert plugin.initialized and registrations[0][0].endswith('/rpc')
    assert await plugin.store.list_tasks() == []
    grouped_result=await plugin.service.generator.run(TaskConfig(name='groups',
        groups=['probe:GroupMessage:1','probe:GroupMessage:2'],provider_id='primary',
        recipient='probe:FriendMessage:42'),utcnow(),await plugin.store.settings())
    assert grouped_result['logical_calls']==3
    group_inputs=[json.loads(prompt.split('\n资料：\n',1)[1]) for prompt in prompts if '\n资料：\n' in prompt]
    assert [item['group']['name'] for item in group_inputs if item['phase']=='group_messages']==['课程群','社团群']
    assert [item['group']['id'] for item in group_inputs[-1]['groups']]==['1','2']
    assert all('group' not in row for item in group_inputs if item['phase']=='group_messages' for row in item['messages'])
    from digest.adapters import SendResult
    from digest.workflow_store import entities
    from astrbot.api.message_components import Reply
    receipts=[]
    class FlowSender:
        async def send_text(self,recipient,text):
            assert recipient=='probe:FriendMessage:42'
            receipts.append(text)
            return SendResult('acknowledged',str(len(receipts)))
    plugin.runtime.sender=FlowSender()
    saved=await plugin.service.save(TaskConfig(name='probe',groups=['probe:GroupMessage:1'],
        provider_id='primary',recipient='probe:FriendMessage:42'))
    saved=await plugin.service.enable(saved['id'],saved['version'],True)
    planned=utcnow()
    await plugin.store.call(lambda db:db.execute('UPDATE tasks SET next_at=? WHERE id=?',(planned.isoformat(),saved['id'])))
    await plugin.runtime.tick(now=planned)
    async with asyncio.timeout(12):
        while True:
            lists=await plugin.store.call(lambda db:entities(db,'list'))
            if lists and lists[0]['state']=='active': break
            await asyncio.sleep(.02)
    assert len(receipts)==2,receipts
    stopped=[]
    event=SimpleNamespace(get_platform_name=lambda:'aiocqhttp',
        get_messages=lambda:[],unified_msg_origin='probe:FriendMessage:42',
        message_obj=SimpleNamespace(message_id='crossed-chat'),message_str='刚才聊到一半的话题',stop_event=lambda:stopped.append(True))
    response[0]=json.dumps({'action':'ignore'})
    calls_before=len(calls)
    await plugin.on_private(event)
    assert len(calls)>calls_before and not stopped
    response[0]=json.dumps({'action':'ask','question':'第一项提前半小时还是一小时？'})
    event.message_obj.message_id='clarification';event.message_str='第一项帮我选个提前量'
    await plugin.on_private(event)
    async with asyncio.timeout(8):
        while not any(item['channel']=='reply' and item['state']=='active'
                      for item in await plugin.store.call(lambda db:entities(db,'dialogue'))):
            await asyncio.sleep(.02)
    response[0]=json.dumps({'action':'select','list_id':lists[0]['id'],'indices':[1],
                            'lead_minutes':60,'evidence':'一小时'})
    event.message_obj.message_id='user-1';event.message_str='一小时'
    await plugin.on_private(event)
    parsed_prompt=json.loads(next(prompt for prompt in reversed(prompts) if prompt.startswith('{"user_message"')))
    assert [turn['content'] for turn in parsed_prompt['context']['conversation']]==[
        '第一项帮我选个提前量','第一项提前半小时还是一小时？']
    await plugin.on_private(event)
    reminders=await plugin.store.call(lambda db:entities(db,'reminder'))
    assert len(reminders)==1 and len(stopped)==3 and reminders[0]['lead_minutes']==60
    receipt=await plugin.store.call(lambda db:db.execute(
        "SELECT result FROM interactions WHERE message_id='user-1'").fetchone()[0])
    assert '主人' in receipt and '事项时间' in receipt and '实际提醒' in receipt and '任务状态' in receipt
    native_id=reminders[0]['native_job_id']
    native_job=next(j for j in await manager.list_jobs() if j.job_id==native_id)
    assert native_job.job_type=='active_agent' and native_job.run_once and native_job.persistent
    from astrbot.dashboard.services.cron_service import CronService
    cron_service=CronService(SimpleNamespace(cron_manager=manager))
    changed_at=(utcnow()+timedelta(hours=2)).isoformat()
    await cron_service.update_job(native_id,{'run_at':changed_at,'enabled':False,
                                            'note':'custom reminder note','timezone':'UTC'})
    await plugin.native_reminders.sync()
    synced=(await plugin.store.call(lambda db:entities(db,'reminder')))[0]
    assert synced['state']=='native_paused' and synced['remind_at']==changed_at
    response[0]=json.dumps({'action':'ignore'})
    event.message_obj.message_id='user-2';event.message_str='今天天气怎样'
    calls_before=len(calls)
    await plugin.on_private(event)
    assert len(stopped)==3 and len(calls)==calls_before,'closed window must bypass the model'
    await plugin.service.archive(saved['id'],saved['version'])
    async with asyncio.timeout(5):
        while True:
            deletion_preview=await plugin.deletion.preview(saved['id'])
            if not deletion_preview['busy']: break
            await asyncio.sleep(.02)
    await plugin.deletion.delete(saved['id'],deletion_preview['version'],deletion_preview['token'],False,'彻底删除')
    assert await plugin.store.list_tasks(True)==[]
    assert any(j.job_id==native_id for j in await manager.list_jobs())
    # Preserved reminders must still accept private commands after their parent task is gone.
    response[0]=json.dumps({'action':'modify','reminder_id':reminders[0]['id'],'lead_minutes':15,'evidence':'提前15分钟'})
    event.message_obj.message_id='orphan-edit';event.message_str=reminders[0]['id'][:8]+' 提前15分钟'
    event.get_messages=lambda:[]
    await plugin.on_private(event)
    orphan=(await plugin.store.call(lambda db:entities(db,'reminder')))[0]
    assert orphan['lead_minutes']==15 and orphan['task_ids']==[] and orphan['state']=='native_paused'
    edited_job=next(j for j in await manager.list_jobs() if j.job_id==native_id)
    assert edited_job.payload['note']=='custom reminder note' and edited_job.timezone=='UTC'
    # A second synthetic task exercises optional native cancellation with the real CronJobManager.
    other=await plugin.service.save(TaskConfig(name='delete-cancel',groups=['probe:GroupMessage:1'],
        provider_id='primary',recipient='probe:FriendMessage:42'))
    from digest.workflow_store import put,uid
    synthetic={**orphan,'id':uid(),'task_id':other['id'],'task_ids':[other['id']],
               'list_ids':[],'version':1,'state':'scheduled','title':'独立的取消测试',
               'event_at':(utcnow()+timedelta(hours=6)).isoformat(),
               'remind_at':(utcnow()+timedelta(hours=5)).isoformat()}
    synthetic={k:v for k,v in synthetic.items() if not k.startswith('native_')}
    synthetic['native_owned']=True
    await plugin.store.call(lambda db:put(db,'reminder',synthetic))
    await plugin.native_reminders.sync()
    await plugin.service.archive(other['id'],other['version'])
    async with asyncio.timeout(5):
        while True:
            deletion_preview=await plugin.deletion.preview(other['id'])
            if not deletion_preview['busy']: break
            await asyncio.sleep(.02)
    await plugin.deletion.delete(other['id'],deletion_preview['version'],deletion_preview['token'],True,'彻底删除')
    assert len(await plugin.store.call(lambda db:entities(db,'reminder')))==1
    await plugin.terminate()
    assert not plugin.initialized
    assert len(await manager.list_jobs())==1, 'native reminder must survive plugin unload'
    await manager.delete_job(native_id)
    await plugin.native_reminders.sync()
    assert (await plugin.store.call(lambda db:entities(db,'reminder')))[0]['state']=='native_absent'
    assert not await manager.list_jobs()
    # Check reuse and recurring collisions with the real host API, still in the isolated DB.
    target=(utcnow()+timedelta(hours=5)).replace(second=0,microsecond=0)
    foreign=await manager.add_active_job(name='已有原生日程',cron_expression=None,
        payload={'session':'probe:FriendMessage:42','sender_id':'42','note':'提醒我参加已有原生日程'},
        timezone='UTC',run_once=True,run_at=target)
    external_snapshot=plugin.native_reminders.fingerprint(foreign)
    reused={k:v for k,v in orphan.items() if not k.startswith('native_')}
    reused.update(id=uid(),task_id='',task_ids=[],list_ids=[],version=1,state='scheduled',
        native_owned=True,title='已有原生日程',place='',remind_at=target.isoformat(),
        event_at=(target+timedelta(minutes=30)).isoformat(),timezone='UTC')
    await plugin.store.call(lambda db:put(db,'reminder',reused))
    await plugin.native_reminders.sync()
    found=next(r for r in await plugin.store.call(lambda db:entities(db,'reminder')) if r['id']==reused['id'])
    assert found['native_link_mode']=='reused' and found['native_job_id']==foreign.job_id
    assert len(await manager.list_jobs())==1
    assert plugin.native_reminders.fingerprint((await manager.list_jobs())[0])==external_snapshot
    await manager.delete_job(foreign.job_id)
    await plugin.native_reminders.sync()
    recurring=await manager.add_active_job(name='已有周期任务',
        cron_expression=f'{target.minute} {target.hour} * * *',
        payload={'session':'probe:FriendMessage:42','sender_id':'42','note':'另外一个周期任务'},timezone='UTC')
    pending={**reused,'id':uid(),'title':'新的独立事项'}
    await plugin.store.call(lambda db:put(db,'reminder',pending))
    await plugin.native_reminders.sync()
    changes=await plugin.store.call(lambda db:entities(db,'change'))
    conflict=next(c for c in changes if c['proposal'].get('reminder_id')==pending['id'] and c['state']=='pending')
    assert conflict['proposal']['candidates'][0]['id']==recurring.job_id
    assert len(await manager.list_jobs())==1
    await plugin.interactions.manage_change(conflict['id'],True,{'native_choice':'create'})
    assert len(await manager.list_jobs())==2
    for job in await manager.list_jobs():
        await manager.delete_job(job.job_id)
    manager.scheduler.shutdown(wait=False)
    await asyncio.sleep(0.02)
    await db.engine.dispose()
    # The framework's import-time helper also points exclusively into root.
    from astrbot.core import db_helper
    await db_helper.engine.dispose()
    return {'native_runner_fallback': 'pass', 'native_runner_all_failed': 'pass',
            'native_runner_cancel': 'pass', 'native_cron_callback_rebuild': 'pass',
            'native_history_scope': 'pass', 'onebot_call_action_available': 'pass',
            'plugin_model_adapter': 'pass', 'plugin_history_snapshot': 'pass',
            'plugin_cron_rebuild': 'pass', 'plugin_send_receipt_classification': 'pass',
            'plugin_entry_lifecycle': 'pass',
            'plugin_scheduled_pipeline': 'pass', 'plugin_private_reply_pipeline': 'pass',
            'native_reminder_create_edit_pause_delete_persist': 'pass',
            'native_task_deletion_keep_and_cancel': 'pass', 'orphan_reminder_private_command': 'pass',
            'native_custom_note_and_timezone_preserved': 'pass',
            'reply_window_crossed_chat': 'pass', 'native_multiturn_clarification': 'pass',
            'ordinary_chat_bypasses_model_after_selection': 'pass',
            'native_existing_job_reuse': 'pass', 'native_recurring_collision': 'pass',
            'native_collision_explicit_create': 'pass',
            'persona_and_task_prompt_receipt': 'pass', 'authoritative_receipt_time_and_state': 'pass',
            'shared_persona_summary_and_receipt': 'pass',
            'native_group_history_order_and_names': 'pass', 'native_grouped_summary_pipeline': 'pass',
            'native_group_envelope_without_repeated_labels': 'pass',
            'live_model_calls': 0, 'qq_messages': 0}


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='digest_probe_') as root:
        os.environ['ASTRBOT_ROOT'] = root
        os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
        original_connect = socket.socket.connect
        def deny_connect(*args, **kwargs):
            raise AssertionError('Network connection forbidden in isolated probe')
        socket.socket.connect = deny_connect
        try:
            print(json.dumps(asyncio.run(probe(root)), ensure_ascii=True))
        finally:
            socket.socket.connect = original_connect
