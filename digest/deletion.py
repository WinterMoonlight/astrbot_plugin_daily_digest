"""Explicit task deletion, with durable progress for native reminder cancellation."""
import hashlib

from .domain import utcnow
from .workflow_store import dump, entities, put


class TaskDeletion:
    def __init__(self, store, service, native):
        self.store, self.service, self.native = store, service, native

    @staticmethod
    def task_links(reminder):
        return list(dict.fromkeys(reminder.get('task_ids') or
                                  ([reminder['task_id']] if reminder.get('task_id') else [])))

    def inspect(self, db, task_id):
        task = self.store.task(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())
        runs = [dict(x) for x in db.execute('SELECT id,status FROM runs WHERE task_id=?', (task_id,))]
        run_ids = {r['id'] for r in runs}
        lists = [x for x in entities(db, 'list') if x['task_id'] == task_id]
        reminders = [x for x in entities(db, 'reminder') if task_id in self.task_links(x)]
        unique = [r for r in reminders if self.task_links(r) == [task_id]]
        changes = [x for x in entities(db, 'change') if x.get('task_id') == task_id or
                   x.get('origin_run') in run_ids]
        owners = run_ids | {x['id'] for x in changes}
        reminder_ids = {r['id'] for r in unique}
        refs = entities(db, 'interaction_refs')
        ref_by_id = {r['id']: r for r in refs}
        recipients = {task['config']['recipient']} | {r['recipient'] for r in reminders}
        replies = []
        for row in db.execute('SELECT recipient,message_id FROM interactions'):
            if row['recipient'] not in recipients:
                continue
            key = row['recipient'] + '/' + row['message_id']
            ref = ref_by_id.get(key)
            # User explicitly chose to clear unscoped legacy reply text for the recipient.
            if ref is None or task_id in ref.get('task_ids', []):
                replies.append((row['recipient'], row['message_id'], key))
        owners.update(key for _, _, key in replies)
        outbox = [dict(row) for row in db.execute('SELECT * FROM outbox') if
                  row['task_id'] == task_id or row['owner'] in owners or
                  (row['channel'] == 'reminder' and row['owner'].split(':')[0] in reminder_ids)]
        busy = (any(r['status'] in ('queued', 'generating') for r in runs) or
                any(r['state'] == 'sending' for r in outbox) or
                any(db.execute("SELECT 1 FROM interactions WHERE recipient=? AND state='processing'",
                               (recipient,)).fetchone() for recipient in recipients))
        journal = next((j for j in entities(db, 'task_deletion') if j['id'] == task_id), None)
        signature = [task['version'], sorted(run_ids), sorted(x['id'] for x in lists),
                     sorted((r['id'], r['version'], r['state'], self.task_links(r), r.get('native_job_id'))
                            for r in reminders), sorted(x['id'] for x in changes)]
        token = hashlib.sha256(dump(signature).encode()).hexdigest()
        return dict(task=task, runs=runs, lists=lists, reminders=reminders, unique=unique,
                    changes=changes, outbox=outbox, replies=replies, refs=refs, busy=bool(busy),
                    token=token, journal=journal)

    @staticmethod
    def public(info):
        task = info['task']
        return {'id': task['id'], 'version': task['version'], 'name': task['config']['name'],
                'archived': task['archived'], 'busy': info['busy'], 'token': info['token'],
                'runs': len(info['runs']), 'lists': len(info['lists']),
                'unique_reminders': len(info['unique']),
                'shared_reminders': len(info['reminders']) - len(info['unique']),
                'legacy_reply_texts': sum(1 for _, _, key in info['replies']
                                         if not any(r['id'] == key for r in info['refs'])),
                'resuming': info['journal'] is not None,
                'cancel_unique': bool(info['journal'] and info['journal']['cancel_unique'])}

    async def preview(self, task_id):
        async with self.native.lock:
            await self.native.sync(locked=True)
            return self.public(await self.store.call(lambda db: self.inspect(db, task_id)))

    async def delete(self, task_id, version, token, cancel_unique, confirmation):
        if type(cancel_unique) is not bool or confirmation != '彻底删除':
            raise ValueError('请在确认页输入“彻底删除”并确认提醒处理方式')
        async with self.native.lock:
            await self.native.sync(locked=True)
            def prepare(db):
                # A repeated HTTP request must not touch any other task.
                if any(x['id'] == task_id for x in entities(db, 'deleted_task')):
                    return None
                info = self.inspect(db, task_id)
                if info['task']['version'] != version or info['token'] != token:
                    raise ValueError('任务或关联提醒已变化，请重新查看删除范围')
                if not info['task']['archived']:
                    raise ValueError('请先归档任务，再彻底删除')
                if info['busy'] or task_id in self.service.active:
                    raise ValueError('仍有生成、发送或私聊操作正在进行，请稍后刷新再删除')
                if info['journal'] and info['journal']['cancel_unique'] != cancel_unique:
                    raise ValueError('删除已开始，请沿用原提醒处理方式继续完成')
                journal = info['journal'] or {'id': task_id, 'task_id': task_id,
                    'recipient': info['task']['config']['recipient'], 'cancel_unique': cancel_unique,
                    'created_at': utcnow().isoformat(), 'state': 'pending'}
                put(db, 'task_deletion', journal)
                if cancel_unique:
                    for r in info['unique']:
                        r['delete_pending_task'] = task_id
                        put(db, 'reminder', r)
                return info
            info = await self.store.call(prepare)
            if info is None:
                return {'deleted': True, 'already_deleted': True}
            if cancel_unique:
                try:
                    jobs = {j.job_id: j for j in await self.native.manager.list_jobs()}
                    for reminder in info['unique']:
                        linked = [j for j in jobs.values() if j.job_id == reminder.get('native_job_id') or
                                  ((j.payload or {}).get('digest_reminder_owner') == self.native.OWNER and
                                   (j.payload or {}).get('digest_reminder_id') == reminder['id'])]
                        for job in linked:
                            if (job.payload or {}).get('digest_reminder_owner') != self.native.OWNER or (
                                    job.payload or {}).get('digest_reminder_id') != reminder['id']:
                                raise ValueError('原生提醒归属不匹配')
                            await self.native.manager.delete_job(job.job_id)
                        if reminder.get('native_creating') and not reminder.get('native_job_id') and not linked:
                            raise ValueError('原生创建结果仍未知，不能确认取消')
                except Exception as exc:
                    raise ValueError('部分原生提醒尚未确认取消，任务数据仍保留。请检查未来任务后，'
                                     '重新打开删除确认页继续；已取消的提醒不会重建。') from exc
            await self.store.call(lambda db: self.purge(db, task_id, cancel_unique))
            return {'deleted': True, 'cancelled_unique': len(info['unique']) if cancel_unique else 0,
                    'retained_reminders': len(info['reminders']) - (len(info['unique']) if cancel_unique else 0)}

    def purge(self, db, task_id, cancel_unique):
        info = self.inspect(db, task_id)
        if info['busy']:
            raise ValueError('仍有发送中的消息，已保留数据，请稍后继续删除')
        task = info['task']; run_ids = {x['id'] for x in info['runs']}
        list_ids = {x['id'] for x in info['lists']}
        unique_ids = {x['id'] for x in info['unique']}
        if cancel_unique:
            extra = [x for x in entities(db, 'change') if x.get('proposal', {}).get('reminder_id') in unique_ids
                     and x['id'] not in {c['id'] for c in info['changes']}]
            info['changes'].extend(extra)
            for change in extra:
                info['outbox'].extend(dict(r) for r in db.execute('SELECT * FROM outbox WHERE owner=?', (change['id'],)))
            if any(r['state'] == 'sending' for r in info['outbox']):
                raise ValueError('关联变更询问仍在发送，请稍后继续删除')
        for r in info['reminders']:
            remaining = [ident for ident in self.task_links(r) if ident != task_id]
            if not remaining and cancel_unique:
                db.execute("DELETE FROM entities WHERE kind='reminder' AND id=?", (r['id'],))
                put(db, 'deleted_reminder', {'id': r['id'], 'recipient': r['recipient']})
                continue
            r['task_ids'] = remaining
            if r.get('task_id') == task_id:
                r['task_id'] = remaining[0] if remaining else ''
            r['list_ids'] = [ident for ident in r['list_ids'] if ident not in list_ids | run_ids]
            if not remaining:
                r['interaction_config'] = {k: task['config'][k] for k in
                                          ('provider_id', 'persona_id', 'recipient', 'timezone')}
            r.pop('delete_pending_task', None)
            put(db, 'reminder', r)
            db.execute("UPDATE entities SET task_id=? WHERE kind='reminder' AND id=?", (r['task_id'], r['id']))
        for record in info['lists']:
            put(db, 'deleted_list', {'id': record['id'], 'recipient': record['recipient']})
            db.execute("DELETE FROM entities WHERE kind='list' AND id=?", (record['id'],))
        for record in info['changes']:
            db.execute("DELETE FROM entities WHERE kind='change' AND id=?", (record['id'],))
        for row in info['outbox']:
            # Retained reminders remain independent business data, not task execution history.
            if row['channel'] == 'reminder' and not cancel_unique:
                continue
            db.execute("UPDATE outbox SET body=NULL,error=NULL,task_id=NULL,state=CASE WHEN state='acknowledged' "
                       "THEN state ELSE 'held' END WHERE id=?", (row['id'],))
        for recipient, message_id, _ in info['replies']:
            db.execute('UPDATE interactions SET result=NULL WHERE recipient=? AND message_id=?',
                       (recipient, message_id))
        for ref in info['refs']:
            if task_id not in ref.get('task_ids', []):
                continue
            ref['task_ids'] = [t for t in ref['task_ids'] if t != task_id]
            ref['list_ids'] = [i for i in ref.get('list_ids', []) if i not in list_ids]
            if cancel_unique:
                ref['reminder_ids'] = [i for i in ref.get('reminder_ids', []) if i not in unique_ids]
            put(db, 'interaction_refs', ref)
        for run_id in run_ids:
            db.execute('DELETE FROM parts WHERE run_id=?', (run_id,))
            db.execute('DELETE FROM delivery WHERE run_id=?', (run_id,))
        db.execute('DELETE FROM runs WHERE task_id=?', (task_id,))
        db.execute("DELETE FROM entities WHERE kind IN ('latest','task_deletion') AND id=?", (task_id,))
        db.execute('DELETE FROM tasks WHERE id=?', (task_id,))
        put(db, 'deleted_task', {'id': task_id, 'recipient': task['config']['recipient']})
