from pathlib import Path

from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .digest.adapters import NativeHistory, NativeModels, OneBot
from .digest.domain import TaskConfig
from .digest.generation import Generator
from .digest.service import Service
from .digest.storage import Conflict, Store

PLUGIN_NAME = 'astrbot_plugin_daily_digest'


class DailyDigest(Star):
    """Development milestone: task management and side-effect-free previews."""

    def __init__(self, context: Context):
        super().__init__(context)
        self.store = Store(Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME / 'digest.sqlite3')
        self.models = NativeModels(context)
        self.onebot = OneBot(context)
        self.service = Service(self.store, Generator(self.models, NativeHistory(context)))
        self.initialized = False
        context.register_web_api(f'/{PLUGIN_NAME}/rpc', self.rpc, ['POST'], '每日简报管理')

    async def initialize(self):
        await self.service.start()
        self.initialized = True

    async def terminate(self):
        self.initialized = False
        await self.service.stop()

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
                data = {'tasks': await self.store.list_tasks(), 'settings': await self.store.settings(),
                        'capabilities': {'preview': True, 'scheduled_delivery': False, 'reminders': False}}
            elif action == 'metadata':
                data = await self.metadata()
            elif action == 'save':
                data = await self.store.save_task(TaskConfig.from_dict(payload.get('config')),
                    payload.get('id'), payload.get('version'))
            elif action == 'archive':
                data = await self.service.archive(payload['id'], payload['version'])
            elif action == 'preview':
                data = {'id': await self.service.preview(payload['id'])}
            elif action == 'run':
                data = await self.store.get_run(payload['id'])
            elif action == 'runs':
                page = payload.get('page', 1)
                if type(page) is not int or page < 1:
                    raise ValueError('页码必须为正整数')
                data = await self.store.list_runs(payload['id'], page)
            elif action == 'settings':
                data = await self.store.settings(payload.get('values'))
                self.service.wake.set()
            else:
                raise ValueError('不支持的操作；正式投递与提醒将在后续开发阶段开放')
            return json_response({'ok': True, 'data': data})
        except Conflict as exc:
            return error_response(str(exc), status_code=409)
        except (ValueError, KeyError, TypeError) as exc:
            return error_response(str(exc), status_code=400)
        except Exception:
            return error_response('操作失败，请检查插件运行状态', status_code=500)
