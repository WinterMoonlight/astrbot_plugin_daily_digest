from pathlib import Path

from astrbot.api.star import Context, Star
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Reply
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .digest.adapters import NativeHistory, NativeModels, OneBot, NativeClock
from .digest.domain import TaskConfig
from .digest.generation import Generator
from .digest.service import Service
from .digest.storage import Conflict, Store
from .digest.runtime import Runtime
from .digest.interactions import Interactions, Interpreter
from .digest.native_reminders import NativeReminders
from .digest.deletion import TaskDeletion
from .digest.receipts import ReplyRenderer

PLUGIN_NAME = 'astrbot_plugin_daily_digest'


class DailyDigest(Star):
    """Persistent digest delivery and opt-in reminders using AstrBot's native clock."""

    def __init__(self, context: Context):
        super().__init__(context)
        self.store = Store(Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME / 'digest.sqlite3')
        self.models = NativeModels(context)
        self.onebot = OneBot(context)
        self.service = Service(self.store, Generator(self.models, NativeHistory(context)))
        self.native_reminders = NativeReminders(self.store, context.cron_manager)
        self.runtime = Runtime(self.store, self.service, self.onebot, self.native_reminders)
        self.service.on_ready = self.runtime.freeze
        self.interactions = Interactions(self.store, Interpreter(self.models), self.runtime,
                                         ReplyRenderer(self.models))
        self.deletion = TaskDeletion(self.store, self.service, self.native_reminders)
        self.clock = NativeClock(context.cron_manager)
        self.initialized = False
        context.register_web_api(f'/{PLUGIN_NAME}/rpc', self.rpc, ['POST'], '每日简报管理')

    async def initialize(self):
        await self.service.start()
        try:
            await self.runtime.start()
            await self.clock.start(self.runtime.tick)
        except BaseException:
            await self.runtime.stop()
            await self.service.stop()
            raise
        self.initialized = True

    async def terminate(self):
        self.initialized = False
        try:
            await self.clock.stop()
        finally:
            try:
                await self.runtime.stop()
            finally:
                await self.service.stop()

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=10)
    async def on_private(self, event: AstrMessageEvent):
        if not self.initialized or event.get_platform_name() != 'aiocqhttp':
            return
        quote = None
        for component in event.get_messages():
            if isinstance(component, Reply):
                quote = str(component.id)
        if await self.interactions.handle(event.unified_msg_origin, str(event.message_obj.message_id),
                                          event.message_str, quote):
            event.stop_event()

    async def metadata(self):
        models = [{'id': p.provider_config['id'], 'model': p.get_model()}
                  for p in self.context.get_all_providers()]
        personas = [{'id': p.persona_id} for p in await self.context.persona_manager.get_all_personas()]
        groups, warnings = [], []
        for platform in self.context.platform_manager.platform_insts:
            meta = platform.meta()
            if meta.name != 'aiocqhttp':
                continue
            try:
                items = await platform.bot.call_action('get_group_list')
                groups.extend({'umo': f'{meta.id}:GroupMessage:{g["group_id"]}',
                               'name': str(g.get('group_name', '')), 'id': str(g['group_id'])}
                              for g in items)
            except Exception:
                warnings.append(f'{meta.id} 群列表读取失败，可手动填写 UMO')
        return {'models': models, 'personas': personas, 'groups': groups, 'warnings': warnings}

    async def rpc(self):
        if not self.initialized:
            return error_response('插件尚未就绪或正在卸载', status_code=503)
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                raise ValueError('请求必须为对象')
            action = payload.get('action')
            if action == 'list':
                data = {'tasks': await self.store.list_tasks(include_archived=True), 'settings': await self.store.settings(),
                        'capabilities': {'preview': True, 'scheduled_delivery': True, 'reminders': True},
                        'runtime_error': self.runtime.last_error}
            elif action == 'metadata':
                data = await self.metadata()
            elif action == 'save':
                data = await self.service.save(TaskConfig.from_dict(payload.get('config')),
                    payload.get('id'), payload.get('version'))
            elif action == 'archive':
                data = await self.service.archive(payload['id'], payload['version'])
            elif action == 'enable':
                data = await self.service.enable(payload['id'], payload['version'], payload['enabled'])
            elif action == 'preview':
                data = {'id': await self.service.preview(payload['id'])}
            elif action == 'run':
                data = await self.store.get_run(payload['id'])
            elif action == 'runs':
                page = payload.get('page', 1)
                if type(page) is not int or page < 1:
                    raise ValueError('页码必须为正整数')
                data = await self.store.list_runs(payload['id'], page)
            elif action == 'delivery':
                data = await self.store.delivery_detail(payload['id'])
            elif action == 'catchup_preview':
                data = {'id': await self.store.prepare_catchup(payload['id'])}
                self.service.wake.set()
                self.runtime.wake.set()
            elif action == 'resend':
                await self.store.authorize_delivery(payload['id'],payload['channel'])
                self.runtime.wake.set()
                data = {'submitted':True}
            elif action == 'resolve':
                await self.store.resolve_unknown(payload['id'],payload['outcome'])
                self.runtime.wake.set()
                data = {'resolved':True}
            elif action == 'related':
                await self.native_reminders.sync()
                data = await self.store.related(payload['id'])
            elif action == 'delete_preview':
                data = await self.deletion.preview(payload['id'])
            elif action == 'delete':
                data = await self.deletion.delete(payload['id'], payload['version'], payload['token'],
                                                  payload['cancel_unique'], payload['confirmation'])
            elif action == 'reminder':
                data = {'message':await self.interactions.manage(payload['id'],payload['operation'],
                                                               payload['version'],payload.get('values'))}
            elif action == 'change':
                data = {'message':await self.interactions.manage_change(payload['id'],payload['accept'],payload.get('values'))}
            elif action == 'settings':
                data = await self.store.settings(payload.get('values'))
                self.service.wake.set()
            else:
                raise ValueError('不支持的操作')
            return json_response({'ok': True, 'data': data})
        except Conflict as exc:
            return error_response(str(exc), status_code=409)
        except (ValueError, KeyError, TypeError) as exc:
            return error_response(str(exc), status_code=400)
        except Exception:
            return error_response('操作失败，请检查插件运行状态', status_code=500)
