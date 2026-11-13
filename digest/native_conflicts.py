"""Check existing native schedules before creating a new reminder.

Matching is deliberately conservative: ambiguous text is a question, never a
model's authority to overwrite or delete an unrelated native task.
"""
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import dialogue
from .domain import parse_time, utcnow
from .workflow_store import dump, entities, entity, outgoing, put, uid


def minute(value):
    return value.astimezone(timezone.utc).replace(second=0, microsecond=0)


def normalized_title(value):
    text = unicodedata.normalize('NFKC', str(value)).strip()
    text = re.sub(r'^(?:日程提醒|提醒)\s*[:：]\s*', '', text)
    return re.sub(r'\s+', '', text).casefold()


def native_time(value, zone):
    result = datetime.fromisoformat(str(value))
    if result.tzinfo is None:
        if not zone:
            raise ValueError('已有原生任务的时间缺少时区，请在未来任务中核对')
        result = result.replace(tzinfo=ZoneInfo(zone))
    return result.astimezone(timezone.utc)


def event_data(job):
    payload = job.payload or {}
    data = payload.get('digest_event')
    if isinstance(data, dict):
        return data
    # Compatibility with this plugin's previously generated native reminder notes.
    note = str(payload.get('note') or '')
    if payload.get('digest_reminder_owner') and '\n' in note:
        try:
            data = json.loads(note.rsplit('\n', 1)[-1])
            if isinstance(data, dict):
                return data
        except (ValueError, TypeError):
            pass
    return {}


class NativeConflictCheck:
    def matching_jobs(self, reminder, jobs):
        targets = {minute(parse_time(reminder[key])) for key in ('remind_at', 'event_at')}
        result = []
        for job in jobs.values():
            payload = job.payload or {}
            if payload.get('session') != reminder['recipient']:
                continue
            if getattr(job, 'job_type', 'active_agent') != 'active_agent':
                continue
            if (payload.get('digest_reminder_owner') == self.OWNER and
                    payload.get('digest_reminder_id') == reminder['id']):
                continue
            data = event_data(job)
            times = []
            if job.run_once:
                times.append(native_time(payload.get('run_at') or job.cron_expression, job.timezone))
            else:
                # Match the occurrence near each requested time, not only the next occurrence today.
                build = getattr(self.manager, '_build_trigger', None)
                if not callable(build):
                    raise ValueError('当前宿主无法检查周期任务，请先在未来任务核对')
                trigger = build(job)
                for target in targets:
                    occurrence = trigger.get_next_fire_time(None, target)
                    if occurrence is not None and minute(occurrence) == target:
                        times.append(occurrence)
            trigger_times = list(times)
            if data.get('event_at'):
                times.append(native_time(data['event_at'], data.get('timezone') or job.timezone))
            matches = [value for value in times if minute(value) in targets]
            if not matches:
                continue
            title = data.get('title') or job.name
            same = bool(normalized_title(title)) and normalized_title(title) == normalized_title(reminder['title'])
            if not (data.get('title') and data.get('event_at') and
                    payload.get('digest_reminder_owner') == self.OWNER):
                # A matching label can also name a report/search task. Only unambiguous
                # reminder text qualifies automatically; richer natural language is a question.
                note = normalized_title(payload.get('note') or getattr(job, 'description', '') or '')
                title_text = normalized_title(reminder['title'])
                same = same and note in {prefix + title_text for prefix in
                                         ('提醒', '提醒我', '提醒我参加', '请提醒我', '请提醒我参加')}
            if reminder.get('place') or data.get('place'):
                same = same and normalized_title(data.get('place', '')) == normalized_title(reminder['place'])
            if data.get('event_at'):
                same = same and minute(native_time(data['event_at'], data.get('timezone') or job.timezone)) == minute(parse_time(reminder['event_at']))
            # Paused and recurring tasks must be explicitly chosen, even with an identical title.
            same = bool(same and job.enabled and job.run_once and trigger_times[0] > utcnow() and
                        minute(trigger_times[0]) == minute(parse_time(reminder['remind_at'])))
            result.append({'id': job.job_id, 'name': job.name, 'at': min(trigger_times or matches).isoformat(),
                           'enabled': job.enabled, 'run_once': job.run_once, 'same': same,
                           'fingerprint': hashlib.sha256(self.fingerprint(job).encode()).hexdigest()})
        return sorted(result, key=lambda item: item['id'])

    @staticmethod
    def conflict_signature(candidates):
        return hashlib.sha256(dump([(item['id'], item['fingerprint']) for item in candidates]).encode()).hexdigest()

    async def ask_native_conflict(self, reminder, candidates, reason=None):
        signature = self.conflict_signature(candidates)
        def save(db):
            previous = next((item for item in entities(db, 'change', reminder['recipient'])
                             if item['id'] == reminder.get('native_conflict_id')), None)
            if (previous and previous['state'] == 'pending' and
                    previous['proposal']['signature'] == signature and
                    previous['proposal']['version'] == reminder['version']):
                return
            if previous:
                previous['state'] = 'superseded'
                put(db, 'change', previous)
                db.execute("UPDATE outbox SET state='held' WHERE owner=? AND channel='change' AND state='pending'", (previous['id'],))
            change = {'id': uid(), 'task_id': reminder.get('task_id', ''), 'recipient': reminder['recipient'],
                      'state': 'pending', 'reason': reason or '相同时间已有原生任务，需要确认是否为同一事项',
                      'created_at': utcnow().isoformat(), 'proposal': {
                          'action': 'native_conflict', 'reminder_id': reminder['id'],
                          'version': reminder['version'], 'signature': signature, 'candidates': candidates}}
            put(db, 'change', change)
            reminder.update(state='awaiting_native_choice', native_conflict_id=change['id'])
            reminder.pop('native_resolution', None)
            reminder.pop('native_error', None)
            put(db, 'reminder', reminder)
            settings = json.loads(db.execute('SELECT data FROM settings WHERE id=1').fetchone()[0])
            zone = ZoneInfo(reminder['timezone'])
            display = lambda value: parse_time(value).astimezone(zone).strftime('%Y-%m-%d %H:%M')
            text = (f"【提醒确认 {change['id'][:8]}】{change['reason']}。\n"
                    f"本次事项：{reminder['title']}，事项时间 {display(reminder['event_at'])}，"
                    f"计划提醒 {display(reminder['remind_at'])}（{reminder['timezone']}）。\n")
            for index, item in enumerate(candidates, 1):
                text += (f"{index}. {item['name']} · {display(item['at'])} · "
                         f"{'启用' if item['enabled'] else '停用'} · {'一次性' if item['run_once'] else '周期任务'}\n")
            text += ('可以回复“复用第1个”“仍然新建”或“放弃本次新增”。复用会保留原任务的时间、内容和启停状态，'
                     '后续请在未来任务管理。' if candidates else
                     '原先的任务已变化或消失，请重新选择“仍然新建”或“放弃本次新增”。')
            text += f"也可以带上确认编号 {change['id'][:8]} 回复。"
            outgoing(db, change['id'], 'change', reminder['recipient'], text, settings['text_limit'])
            dialogue.queue(db, change['id'], 'change', reminder['recipient'],
                           {'change': [change], 'reminder': [reminder]}, settings)
        await self.store.call(save)

    async def link_existing(self, reminder, job):
        reminder.update(native_job_id=job.job_id, native_link_mode='reused',
                        native_version=reminder['version'], native_reused_requested_at=reminder['remind_at'])
        reminder.pop('native_error', None)
        reminder.pop('native_resolution', None)
        await self.save(reminder)

    async def check_before_create(self, reminder, jobs):
        """Return False after reuse or a question; True permits exactly this creation attempt."""
        fresh = {job.job_id: job for job in await self.manager.list_jobs()}
        jobs.clear()
        jobs.update(fresh)
        candidates = self.matching_jobs(reminder, fresh)
        signature = self.conflict_signature(candidates)
        if reminder.get('native_conflict_id'):
            previous = await self.store.call(lambda db: entity(db, 'change', reminder['native_conflict_id']))
            if previous['proposal']['version'] != reminder['version']:
                previous['state'] = 'superseded'
                await self.store.call(lambda db: put(db, 'change', previous))
                reminder.pop('native_conflict_id', None)
                reminder.pop('native_resolution', None)
                await self.save(reminder)
        decision = reminder.get('native_resolution')
        if decision:
            if decision['signature'] != signature:
                await self.ask_native_conflict(reminder, candidates, '原生任务在确认期间已变化，请重新核对')
                return False
            if decision['choice'] == 'reuse':
                job = fresh.get(decision['job_id'])
                if job is None:
                    await self.ask_native_conflict(reminder, candidates, '选择的原生任务已消失，请重新核对')
                    return False
                await self.link_existing(reminder, job)
                return False
            # Keep the explicit decision until the create attempt's durable marker is written.
            return True
        if reminder.get('native_conflict_id'):
            await self.ask_native_conflict(reminder, candidates)
            return False
        same = [item for item in candidates if item['same']]
        if len(same) == 1:
            await self.link_existing(reminder, fresh[same[0]['id']])
            return False
        if candidates:
            await self.ask_native_conflict(reminder, candidates)
            return False
        return True

    @staticmethod
    def resolve_conflict(db, change, intent):
        proposal = change['proposal']
        reminder = entity(db, 'reminder', proposal['reminder_id'])
        if reminder['recipient'] != change['recipient'] or reminder['version'] != proposal['version']:
            raise ValueError('提醒已变化，请重新核对当前事项')
        if reminder.get('native_job_id') or reminder.get('native_creating'):
            raise ValueError('本次提醒已经关联任务或进入创建流程，请刷新状态')
        if intent['action'] == 'reject':
            reminder['state'] = 'cancelled'
            reminder['version'] += 1
            reminder.pop('native_resolution', None)
            reminder.pop('native_error', None)
            change['state'] = 'rejected'
            answer = '已放弃本次新增，已有原生任务保持不变：' + reminder['title']
        else:
            choice = intent.get('native_choice')
            if choice not in ('reuse', 'create'):
                raise ValueError('请明确选择复用哪个已有任务、仍然新建，或放弃本次新增')
            job_id = intent.get('native_job_id')
            if choice == 'reuse':
                if not job_id and len(proposal['candidates']) == 1:
                    job_id = proposal['candidates'][0]['id']
                if job_id not in {item['id'] for item in proposal['candidates']}:
                    raise ValueError('请指定列表中要复用的原生任务')
            reminder.update(state='scheduled', native_resolution={
                'choice': choice, 'job_id': job_id, 'signature': proposal['signature']})
            change['state'] = 'confirmed'
            answer = '已记录选择，将再次核对原生任务后处理：' + reminder['title']
        put(db, 'reminder', reminder)
        put(db, 'change', change)
        return answer
