"""Host APIs are imported lazily, allowing offline domain tests."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timezone

from .domain import split_umo


class NativeModels:
    def __init__(self, context):
        self.context = context

    async def resolve_persona(self, task):
        """Use the chosen AstrBot persona, otherwise resolve the recipient's actual session persona."""
        manager = getattr(self.context, 'persona_manager', None)
        if manager is None:
            return '', ['宿主人格接口不可用，本次使用任务提示词']
        try:
            if task.persona_id:
                item = await manager.get_persona(task.persona_id)
                return item.system_prompt or '', []
            conversation_persona_id = None
            conversations = getattr(self.context, 'conversation_manager', None)
            if conversations is not None:
                cid = await conversations.get_curr_conversation_id(task.recipient)
                if cid:
                    conversation = await conversations.get_conversation(task.recipient, cid)
                    if conversation is not None:
                        conversation_persona_id = conversation.persona_id
            _, persona, _, _ = await manager.resolve_selected_persona(
                umo=task.recipient, conversation_persona_id=conversation_persona_id,
                platform_name='aiocqhttp')
            return (persona.get('prompt') or persona.get('system_prompt') or '') if persona else '', []
        except Exception:
            return '', ['人格读取失败，本次使用任务提示词']

    async def providers(self, task):
        from astrbot.core.provider.provider import Provider
        main = await self.context.provider_manager.get_provider_by_id(task.provider_id)
        if not isinstance(main, Provider):
            raise ValueError('任务主模型不可用，请检查 AstrBot 模型配置')
        model_cfg = self.context.get_config(umo=task.recipient).get(
            'agent_runner', {}).get('config', {}).get('model', {})
        providers = [main]
        seen = {task.provider_id}
        for provider_id in model_cfg.get('fallback_provider_ids', []):
            if not isinstance(provider_id, str) or provider_id in seen:
                continue
            provider = await self.context.provider_manager.get_provider_by_id(provider_id)
            if isinstance(provider, Provider):
                providers.append(provider)
                seen.add(provider_id)
        return providers, model_cfg.get('request_max_retries', 5)

    async def prepare(self, task):
        providers, retries = await self.providers(task)
        windows = []
        for provider in providers:
            window = provider.provider_config.get('max_context_tokens', 0)
            if not isinstance(window, int) or window <= 0:
                from astrbot.core.utils.llm_metadata import LLM_METADATAS
                window = LLM_METADATAS.get(provider.get_model(), {}).get('limit', {}).get('context', 0)
            if not isinstance(window, int) or window < 1024:
                raise ValueError('主模型或回退模型缺少有效上下文上限，请在 AstrBot 模型设置中填写')
            windows.append(window)
        persona, warnings = await self.resolve_persona(task)
        return {'providers': providers, 'retries': retries, 'window': min(windows),
                'persona': persona, 'warnings': warnings}

    async def generate(self, prepared, prompt, system):
        from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
        from astrbot.core.agent.run_context import ContextWrapper
        from astrbot.core.agent.hooks import BaseAgentRunHooks
        from astrbot.core.agent.tool_executor import BaseFunctionToolExecutor
        from astrbot.core.provider.entities import ProviderRequest

        class DenyTools(BaseFunctionToolExecutor):
            @classmethod
            async def execute(cls, *args, **kwargs):
                raise RuntimeError('简报生成不允许执行工具')
                yield  # pragma: no cover

        runner = ToolLoopAgentRunner()
        await runner.reset(provider=prepared['providers'][0],
            request=ProviderRequest(prompt=prompt, system_prompt=system),
            run_context=ContextWrapper(context=None), tool_executor=DenyTools(),
            agent_hooks=BaseAgentRunHooks(), streaming=False,
            fallback_providers=prepared['providers'][1:], request_max_retries=prepared['retries'])
        async for _ in runner.step_until_done(1):
            pass
        response = runner.get_final_llm_resp()
        if response is None or response.role != 'assistant' or not response.completion_text:
            raise RuntimeError('模型未返回可用结果（原生兜底已结束）')
        if getattr(response, 'tools_call_args', None) or getattr(response, 'tools_call_name', None):
            raise RuntimeError('模型返回工具请求，已拒绝执行')
        return response.completion_text, runner.provider.provider_config['id']


class NativeHistory:
    def __init__(self, context):
        self.context = context

    async def group_labels(self, groups):
        labels = {}
        for group in groups:
            platform, _, number = split_umo(group, 'GroupMessage')
            labels[group] = {'group_id': number, 'group_name': None, 'platform_id': platform}
        platforms = getattr(getattr(self.context, 'platform_manager', None), 'platform_insts', [])
        for platform_id in {item['platform_id'] for item in labels.values()}:
            try:
                platform = next(p for p in platforms if p.meta().id == platform_id and p.meta().name == 'aiocqhttp')
                async with asyncio.timeout(10):
                    items = await platform.bot.call_action('get_group_list')
                names = {str(item['group_id']): item.get('group_name') for item in items}
            except Exception:
                names = {}
            for info in labels.values():
                if info['platform_id'] != platform_id:
                    continue
                name = names.get(info['group_id'])
                if isinstance(name, str) and name.strip():
                    info['group_name'] = name
                else:
                    info['metadata_warning'] = '群名称暂不可用，使用群号标识'
        return labels

    async def read(self, groups, start, end):
        from sqlalchemy import select, func, text
        from astrbot.core.db.po import PlatformMessageHistory as Row
        messages, coverage = [], []
        labels = await self.group_labels(groups)
        for group in groups:
            platform, _, _ = split_umo(group, 'GroupMessage')
            try:
                async with self.context.message_history_manager.db.get_db() as session:
                    # SQLite SELECT alone does not reliably start a read snapshot.
                    await session.execute(text('BEGIN'))
                    scope = (Row.platform_id == platform, Row.user_id == group)
                    stats = (await session.execute(select(func.count(Row.id), func.min(Row.created_at),
                                                          func.max(Row.created_at)).where(*scope))).one()
                    result = await session.execute(select(Row).where(*scope,
                        Row.created_at >= start.astimezone(timezone.utc).replace(tzinfo=None),
                        Row.created_at < end.astimezone(timezone.utc).replace(tzinfo=None))
                        .order_by(Row.created_at, Row.id))
                    rows = result.scalars().all()
                    group_messages = []
                    for row in rows:
                        parts = row.content.get('message', [])
                        body = []
                        for part in parts if isinstance(parts, list) else []:
                            if part.get('type') == 'plain':
                                body.append(str(part.get('text', '')))
                            elif part.get('type') == 'reply':
                                body.append('[引用] ' + str(part.get('text', '')))
                            elif part.get('type') == 'at':
                                body.append('@' + str(part.get('name') or part.get('user_id', '')))
                            else:
                                body.append('[' + str(part.get('type', '媒体')) + ']')
                        group_messages.append({'id': f'{platform}/{row.id}', 'group': group,
                            'time': row.created_at.replace(tzinfo=timezone.utc).isoformat(),
                            'text': ''.join(body)})
                    messages.extend(group_messages)
                    coverage.append({'group': group, **labels[group], 'read': 'ok', 'count': len(rows),
                        'retained_count': stats[0], 'earliest': str(stats[1]) if stats[1] else None,
                        'latest': str(stats[2]) if stats[2] else None,
                        'completeness': 'unknown',
                        'note': '仅基于现存历史；时间为入库时间，无法证明采集完整'})
            except Exception:
                coverage.append({'group': group, **labels[group], 'read': 'failed', 'count': 0,
                                 'completeness': 'insufficient', 'note': '该群历史读取失败'})
        if not any(item['read'] == 'ok' for item in coverage):
            raise RuntimeError('所有群历史读取失败')
        return messages, coverage


@dataclass(frozen=True)
class SendResult:
    state: str
    receipt: str | None = None
    reason: str | None = None


class OneBot:
    def __init__(self, context):
        self.context = context

    def client(self, umo):
        platform_id, _, _ = split_umo(umo)
        for platform in self.context.platform_manager.platform_insts:
            if platform.meta().id == platform_id and platform.meta().name == 'aiocqhttp':
                return platform.bot
        raise ValueError('未找到对应的 OneBot 平台实例')

    async def send_text(self, recipient, text):
        try:
            _, _, number = split_umo(recipient, 'FriendMessage')
            client = self.client(recipient)
            if not text:
                raise ValueError('不能发送空文本')
        except ValueError as exc:
            return SendResult('failed', reason=str(exc))
        try:
            response = await client.call_action('send_private_msg', user_id=int(number),
                message=[{'type': 'text', 'data': {'text': text}}])
            # CQHttp returns action data, not the outer status/data envelope.
            if isinstance(response, dict) and type(response.get('message_id')) in (int, str):
                if str(response['message_id']).strip():
                    return SendResult('acknowledged', receipt=str(response['message_id']))
            return SendResult('unknown', reason='发送接口未返回有效消息 ID')
        except asyncio.CancelledError:
            raise  # caller's durable sending marker becomes unknown on recovery
        except Exception as exc:
            from aiocqhttp.exceptions import ActionFailed
            if isinstance(exc, ActionFailed):
                return SendResult('failed', reason='协议端明确拒绝发送')
            return SendResult('unknown', reason='发送响应中断，无法确认是否已发送')


class NativeClock:
    OWNER = 'astrbot_plugin_daily_digest/v1'

    def __init__(self, manager):
        self.manager = manager
        self.job_id = None

    async def start(self, callback):
        for job in await self.manager.list_jobs():
            if (job.payload or {}).get('digest_owner') == self.OWNER:
                await self.manager.delete_job(job.job_id)
        async def tick(**_):
            await callback()
        job = await self.manager.add_basic_job(name='每日简报：任务调度',
            cron_expression='* * * * *', handler=tick, timezone='Asia/Shanghai',
            payload={'digest_owner': self.OWNER}, persistent=False)
        self.job_id = job.job_id

    async def stop(self):
        if self.job_id:
            await self.manager.delete_job(self.job_id)
            self.job_id = None
