"""Persona-aware wording around authoritative reminder facts, with a local fallback."""
import asyncio
import re
from zoneinfo import ZoneInfo

from .domain import TaskConfig, parse_time
from .generation import json_object
from .workflow_store import dump


LABELS = {'title': '事项', 'outcome': '本次处理', 'event_time': '事项时间',
          'reminder_time': '实际提醒', 'state': '任务状态'}


def time_text(value, zone):
    local = parse_time(value).astimezone(ZoneInfo(zone))
    return f"{local:%Y-%m-%d %H:%M:%S}（{zone}，UTC{local:%z}）"


def receipt_item(reminder, operation, created_ids):
    zone = reminder.get('timezone', 'Asia/Shanghai')
    state = reminder['state']
    reused = reminder.get('native_link_mode') == 'reused'
    note = ''
    if reminder.get('native_error'):
        outcome, status = '原生任务处理尚未确认', '请核对原生任务'
        note = reminder['native_error']
    elif state == 'awaiting_native_choice':
        outcome, status = '等待你确认，尚未新建', '未创建'
        note = '请按对应的提醒确认消息选择复用、仍然新建或放弃。'
    elif state == 'cancelled':
        outcome = '已放弃本次新增' if operation == 'reject' else '已取消'
        status = '不会按此提醒继续执行'
    elif state == 'native_absent':
        outcome, status = '原生任务已结束或删除', '送达未确认'
    elif not reminder.get('native_job_id'):
        outcome = '尚未确认登记新的原生提醒'
        status = {'failed':'旧版投递失败', 'unknown':'旧版投递结果未知',
                  'acknowledged':'旧版投递已确认'}.get(state, '尚未生效')
    elif reused:
        outcome = '已复用原生任务，未新建第二份'
        status = '已启用' if reminder.get('native_enabled') else '已停用，不会自动提醒'
        note = '修改或取消此复用任务，请在 AstrBot「未来任务」中操作。'
    else:
        outcome = ('已新建原生提醒' if reminder['id'] in created_ids or operation == 'native_conflict'
                   else '已修改原生提醒' if operation == 'modify' else '已关联现有提醒')
        status = '已停用，不会自动提醒' if reminder.get('native_enabled') is False else '已启用'
    if reminder.get('native_error'):
        scheduled = '尚未确认；本次请求为 ' + time_text(reminder['remind_at'], zone)
    elif state in ('cancelled', 'native_absent'):
        scheduled = '无待执行计划'
    elif not reminder.get('native_job_id'):
        scheduled = '尚未生效；本次计划为 ' + time_text(reminder['remind_at'], zone)
    elif reminder.get('native_run_once') is False:
        scheduled = f"周期计划 {reminder.get('native_schedule', '请查看未来任务')}（{zone}）"
    else:
        scheduled = time_text(reminder['remind_at'], zone)
    requested = reminder.get('native_reused_requested_at')
    if reused and requested and parse_time(requested) != parse_time(reminder['remind_at']):
        note += '\n本次原计划提醒：' + time_text(requested, zone) + '；实际采用上面的原生计划。'
    return {'id': reminder['id'], 'title': reminder['title'], 'outcome': outcome,
            'event_time': time_text(reminder['event_at'], zone), 'reminder_time': scheduled,
            'state': status, 'note': note}


class ReplyRenderer:
    def __init__(self, models):
        self.models = models

    @staticmethod
    def style(value):
        if set(value) != {'opening', 'closing', 'labels'} or not isinstance(value['labels'], dict):
            raise ValueError('回执表达格式不正确')
        if set(value['labels']) != set(LABELS):
            raise ValueError('回执缺少必要字段')
        for key, text in {**value['labels'], 'opening': value['opening'], 'closing': value['closing']}.items():
            limit = 80 if key in ('opening', 'closing') else 24
            if not isinstance(text, str) or len(text) > limit or '\n' in text or '\r' in text or re.search(r'\d', text):
                raise ValueError('回执表达包含不适用的内容')
            if key in LABELS and not text.strip():
                raise ValueError('回执字段名为空')
            if key in LABELS and LABELS[key] not in text:
                raise ValueError('回执不能改换事实字段的含义')
        # Result assertions and promises belong to the fixed factual fields, not generated framing.
        framing = value['opening'] + value['closing']
        if re.search(r'成功|失败|已(?:创建|新建|复用|修改|取消|设置)|搞定|安排好|保证|一定|准时|明天|后天|昨天', framing):
            raise ValueError('回执衔接文字不得替代实际状态')
        return value

    async def render(self, config, records, operation, created_ids=(), original=''):
        if not records:
            return original
        try:
            items = [receipt_item(record, operation, created_ids) for record in records]
        except (KeyError, TypeError, ValueError):
            return original
        style = {'opening': '本次日程处理结果：', 'closing': '', 'labels': dict(LABELS)}
        try:
            task = TaskConfig.from_dict(config)
            async with asyncio.timeout(20):
                prepared = await self.models.prepare(task)
                policy = ('你负责日程回执的称呼、语气、衔接和字段名称。实际操作已结束，本次绝不执行操作或调用工具。'
                          '共同遵守下方 AstrBot 人格与插件总结提示词中适用于表达的要求。'
                          '总结提示词中的抽取群消息、忽略闲聊等业务要求不用于省略本次必要回执。'
                          '事实、时间、启停状态和新建/复用结果由程序原样填入，不得改写或自行补充。'
                          'opening 和 closing 只能写礼貌称呼与衔接，不声称成功失败、不保证送达、不另写时间或操作指令。'
                          'labels 必须完整保留示例中的字段名，可以增加语气或饰符，不能调换字段含义；每项不超过二十四字。'
                          '只输出 JSON，结构为 {"opening":"开场，不超过八十字","closing":"收尾或空字符串",'
                          '"labels":{"title":"事项","outcome":"本次处理","event_time":"事项时间",'
                          '"reminder_time":"实际提醒","state":"任务状态"}}。'
                          '\nAstrBot 人格：\n' + prepared.get('persona', '') +
                          '\n插件总结提示词：\n' + task.prompt)
                prompt = dump({'purpose': 'reminder_receipt_style', 'items': items})
                if len((policy + prompt).encode('utf-8')) + 2048 > prepared['window']:
                    raise ValueError('回执表达超过模型输入预算')
                output, _ = await self.models.generate(prepared, prompt, policy)
                style = self.style(json_object(output))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # Wording failure must never re-run or invalidate the completed operation.
        blocks = [style['opening']] if style['opening'] else []
        for item in items:
            block = '\n'.join(f"{style['labels'][key]}：{item[key]}" for key in LABELS)
            if item['note']:
                block += '\n' + item['note']
            blocks.append(block)
        # Preserve explanations about selections that did not produce a reminder, e.g. expired events.
        extra = [line for line in original.splitlines() if not any(item['title'] in line for item in items)]
        blocks.extend(extra)
        if style['closing']:
            blocks.append(style['closing'])
        return '\n\n'.join(blocks)
