"""Native-clock driven scheduling and a durable, receipt-aware text outbox."""
import asyncio
import json
from datetime import timedelta
from zoneinfo import ZoneInfo

from .adapters import SendResult
from .domain import TaskConfig, parse_time, utcnow
from .workflow_store import dump, entities, entity, outgoing, put, uid


def display_time(value, zone='Asia/Shanghai'):
    return parse_time(value).astimezone(ZoneInfo(zone)).strftime('%Y-%m-%d %H:%M')


class Runtime:
    def __init__(self, store, service, sender, native=None):
        self.store, self.service, self.sender = store, service, sender
        self.native = native
        self.wake = asyncio.Event()
        self.stopping = False
        self.worker = None
        self.tick_lock = asyncio.Lock()
        self.last_error = None

    async def start(self):
        await self.tick(restart=True)
        self.worker = asyncio.create_task(self.loop(), name='digest-outbox')

    async def stop(self):
        self.stopping = True
        self.wake.set()
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)

    async def tick(self, restart=False, now=None):
        async with self.tick_lock:
            now = now or utcnow()
            if self.native:
                await self.native.sync()
            await self.store.schedule(now, restart)
            await self.reconcile(now, restart)
            self.service.wake.set()
            self.wake.set()

    async def freeze(self, run_id):
        def work(db):
            run = self.store.run(db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())
            if run['kind'] != 'scheduled' or not run['result'] or not run['result'].get('summary'):
                return
            settings = json.loads(db.execute('SELECT data FROM settings WHERE id=1').fetchone()[0])
            config = run['snapshot']['config']
            db.execute('INSERT OR IGNORE INTO delivery(run_id,authorized) VALUES (?,1)', (run_id,))
            outgoing(db, run_id, 'digest', config['recipient'], run['result']['summary'],
                     settings['text_limit'], run['task_id'])
            db.execute("UPDATE runs SET status='prepared' WHERE id=? AND status='ready'",(run_id,))
        await self.store.call(work)
        self.wake.set()

    def complete_digest(self, db, run, now, settings):
        config = run['snapshot']['config']; recipient = config['recipient']
        db.execute('UPDATE delivery SET completed=1 WHERE run_id=?', (run['id'],))
        db.execute("UPDATE runs SET status='sent',updated_at=? WHERE id=?", (now.isoformat(),run['id']))
        latest = [x for x in entities(db,'latest') if x['id']==run['task_id']]
        if latest and parse_time(latest[0]['planned_at']) > parse_time(run['snapshot']['planned_at']):
            return  # A late catch-up must not replace a newer successful list.
        put(db,'latest',{'id':run['task_id'],'task_id':run['task_id'],'recipient':recipient,
                         'planned_at':run['snapshot']['planned_at']})
        for old in entities(db,'list'):
            if old['task_id']==run['task_id'] and old['state'] in ('active','awaiting_delivery'):
                old['state']='superseded'; put(db,'list',old)
                db.execute("UPDATE outbox SET state='held' WHERE owner=? AND channel='inquiry' AND state='pending'",(old['run_id'],))
        today=now.astimezone(ZoneInfo(config['timezone'])).date()
        items=[x for x in run['result'].get('candidates',[]) if parse_time(x['event_at'])>now and
               today <= parse_time(x['event_at']).astimezone(ZoneInfo(config['timezone'])).date() < today+timedelta(days=config['candidate_days'])]
        if items:
            record={'id':run['id'],'run_id':run['id'],'task_id':run['task_id'],'recipient':recipient,
                    'state':'awaiting_delivery','expires_at':None,'config':config,'items':items}
            put(db,'list',record)
            text=f"【日程清单 {record['id'][:8]} · {config['name']}】\n"+'\n'.join(
                f"{i}. {x['title']} · {display_time(x['event_at'],config['timezone'])}" for i,x in enumerate(items,1))
            text+=f"\n要把哪些事项加入提醒？可回复“清单 {record['id'][:8]} 加入第1项”，也可以用自然语言指定事项和提醒时间。默认提前 {config['lead_minutes']} 分钟，清单发送成功后 {config['selection_hours']} 小时内可补选。"
            outgoing(db,run['id'],'inquiry',recipient,text,settings['text_limit'],run['task_id'])
        for change in run['result'].get('changes',[]):
            matches=[r for r in entities(db,'reminder',recipient) if r['state'] not in ('cancelled','missed')
                     and r['title']==change['title'] and r['event_at']==change['old_event_at']]
            if len(matches)!=1:
                continue
            reminder=matches[0]
            proposal={'action':'cancel' if change['cancelled'] else 'modify','reminder_id':reminder['id'],
                      'event_at':change.get('event_at'), 'version':reminder['version']}
            key=dump(proposal)
            if any(x.get('revision')==key for x in entities(db,'change',recipient)):
                continue
            request={'id':uid(),'recipient':recipient,'task_id':run['task_id'],'state':'pending',
                     'revision':key,'proposal':proposal,'reason':'群通知显示事项取消或改期','created_at':now.isoformat(),
                     'origin_run':run['id']}
            put(db,'change',request)
            self.ask_change(db,request,reminder,settings)

    @staticmethod
    def ask_change(db,change,reminder,settings):
        proposal=change['proposal']
        detail='取消提醒' if proposal['action']=='cancel' else '调整提醒'
        if proposal.get('event_at'):
            detail+='，事项时间 '+display_time(proposal['event_at'],reminder.get('timezone','Asia/Shanghai'))
        outgoing(db,change['id'],'change',change['recipient'],
            f"【提醒确认 {change['id'][:8]}】{change['reason']}：{reminder['title']}。建议{detail}。"
            f"请回复“确认 {change['id'][:8]}”或说明希望的提醒时间；未回复会保留原提醒。",settings['text_limit'],
            change['task_id'] if change.get('origin_run') else None)

    async def reconcile(self, now=None, restart=False, due=True):
        now=now or utcnow()
        def work(db):
            settings=json.loads(db.execute('SELECT data FROM settings WHERE id=1').fetchone()[0])
            for row in db.execute("SELECT r.*,d.completed FROM runs r JOIN delivery d ON d.run_id=r.id WHERE d.completed=0 AND r.result IS NOT NULL").fetchall():
                run=self.store.run(row)
                parts=[dict(x) for x in db.execute("SELECT * FROM outbox WHERE owner=? AND channel='digest' ORDER BY ordinal",(run['id'],))]
                if parts and not run['completed']:
                    if all(x['state']=='acknowledged' for x in parts):
                        self.complete_digest(db,run,now,settings)
                    elif any(x['state']=='unknown' for x in parts):
                        db.execute("UPDATE runs SET status='unknown' WHERE id=?",(run['id'],))
                        db.execute('UPDATE delivery SET authorized=0 WHERE run_id=?',(run['id'],))
                    elif any(x['state']=='failed' for x in parts):
                        db.execute("UPDATE runs SET status='partial' WHERE id=?",(run['id'],))
                        db.execute('UPDATE delivery SET authorized=0 WHERE run_id=?',(run['id'],))
            for record in entities(db,'list'):
                if record['state'] not in ('active','awaiting_delivery'):
                    continue
                if record['state']=='awaiting_delivery':
                    rows=db.execute("SELECT state,ack_at,created_at FROM outbox WHERE owner=? AND channel='inquiry'",(record['run_id'],)).fetchall()
                    if rows and all(x['state']=='acknowledged' for x in rows):
                        record['state']='active'
                        confirmed=max(parse_time(x['ack_at'] or x['created_at']) for x in rows)
                        record['expires_at']=(confirmed+timedelta(hours=record['config']['selection_hours'])).isoformat()
                    elif all(parse_time(i['event_at'])<=now for i in record['items']):
                        record['state']='expired'
                if record['state']=='active' and parse_time(record['expires_at'])<=now:
                    record['state']='expired'
                put(db,'list',record)
            for reminder in entities(db,'reminder'):
                if reminder.get('native_owned'):
                    continue
                owner=f"{reminder['id']}:{reminder['version']}"
                rows=db.execute("SELECT * FROM outbox WHERE owner=? AND channel='reminder'",(owner,)).fetchall()
                if reminder['state'] in ('cancelled','missed','acknowledged'):
                    continue
                if rows:
                    if any(r['state']=='unknown' for r in rows):
                        reminder['state']='unknown'
                    elif all(r['state']=='acknowledged' for r in rows):
                        reminder['state']='acknowledged'
                    elif any(r['state']=='failed' for r in rows):
                        reminder['state']='failed'
                    elif any(r['state']=='sending' for r in rows):
                        reminder['state']='sending'
                    elif any(r['state']=='pending' for r in rows):
                        reminder['state']='retry_wait' if any(r['attempts'] for r in rows) else 'scheduled'
                if parse_time(reminder['event_at'])<=now and reminder['state'] not in ('unknown','acknowledged'):
                    reminder['state']='missed'
                    db.execute("UPDATE outbox SET state='held' WHERE owner=? AND state='pending'",(owner,))
                elif restart and parse_time(reminder['remind_at'])<=now and reminder['state'] in ('scheduled','retry_wait'):
                    reminder['state']='awaiting_choice'
                    db.execute("UPDATE outbox SET state='held' WHERE owner=? AND state='pending'",(owner,))
                    change={'id':uid(),'recipient':reminder['recipient'],'task_id':reminder['task_id'],
                            'state':'pending','created_at':now.isoformat(),'reason':'停机期间错过提醒，请指定新的提醒时间',
                            'proposal':{'action':'modify','reminder_id':reminder['id'],'version':reminder['version']}}
                    put(db,'change',change); self.ask_change(db,change,reminder,settings)
                elif due and reminder['state']=='scheduled' and not rows and parse_time(reminder['remind_at'])<=now:
                    outgoing(db,owner,'reminder',reminder['recipient'],
                        f"【日程提醒】{reminder['title']}\n事项时间：{display_time(reminder['event_at'],reminder['timezone'])}\n{reminder.get('place','')}",settings['text_limit'])
                put(db,'reminder',reminder)
        await self.store.call(work)

    async def claim(self, now=None):
        now=now or utcnow()
        def work(db):
            rows=db.execute("""SELECT * FROM outbox WHERE state='pending' AND next_at<=?
                ORDER BY CASE channel WHEN 'reminder' THEN 0 WHEN 'reply' THEN 1 ELSE 2 END,created_at,ordinal""",(now.isoformat(),)).fetchall()
            for raw in rows:
                row=dict(raw)
                if row['task_id']:
                    task=self.store.task(db.execute('SELECT * FROM tasks WHERE id=?',(row['task_id'],)).fetchone())
                    if not task['enabled'] or task['archived']:
                        continue
                if db.execute("SELECT 1 FROM outbox WHERE owner=? AND channel=? AND ordinal<? AND state!='acknowledged'",
                              (row['owner'],row['channel'],row['ordinal'])).fetchone():
                    continue
                if row['channel'] in ('digest','inquiry'):
                    task=self.store.task(db.execute('SELECT * FROM tasks WHERE id=?',(row['task_id'],)).fetchone())
                    delivery=db.execute('SELECT * FROM delivery WHERE run_id=?',(row['owner'],)).fetchone()
                    if not delivery or not delivery['authorized'] or not task['enabled'] or task['archived']:
                        continue
                    if row['channel']=='inquiry':
                        record=entity(db,'list',row['owner'])
                        if record['state']!='awaiting_delivery' or not any(parse_time(i['event_at'])>now for i in record['items']):
                            continue
                elif row['channel']=='reminder':
                    ident,version=row['owner'].split(':')
                    reminder=entity(db,'reminder',ident)
                    if reminder.get('native_owned'):
                        continue
                    if reminder['version']!=int(version) or reminder['state'] not in ('scheduled','retry_wait') or parse_time(reminder['event_at'])<=now:
                        continue
                elif row['channel']=='change':
                    if entity(db,'change',row['owner'])['state']!='pending':
                        continue
                db.execute("UPDATE outbox SET state='sending',attempts=attempts+1,sent_at=? WHERE id=?",(now.isoformat(),row['id']))
                row['attempts']+=1
                return row
            return None
        return await self.store.call(work)

    async def deliver_one(self):
        row=await self.claim()
        if row is None:
            return False
        try:
            async with asyncio.timeout(60):
                result=await self.sender.send_text(row['recipient'],row['body'])
        except asyncio.CancelledError:
            raise  # Persisted sending becomes unknown on restart.
        except Exception:
            result=SendResult('unknown',reason='发送结果无法确认')
        now=utcnow()
        def finish(db):
            state=result.state; next_at=now
            if row['channel']=='reminder' and state=='failed' and row['attempts']<3:
                reminder=entity(db,'reminder',row['owner'].split(':')[0])
                if reminder['version']==int(row['owner'].split(':')[1]) and parse_time(reminder['event_at'])>now:
                    state='pending'; next_at=now+timedelta(minutes=1)
            db.execute('UPDATE outbox SET state=?,receipt=?,error=?,next_at=?,ack_at=? WHERE id=?',
                (state,result.receipt,result.reason,next_at.isoformat(),now.isoformat() if state=='acknowledged' else None,row['id']))
            if row['channel'] in ('digest','inquiry') and state in ('failed','unknown'):
                db.execute('UPDATE delivery SET authorized=0 WHERE run_id=?',(row['owner'],))
        await self.store.call(finish)
        await self.reconcile(due=False)
        return True

    async def loop(self):
        while not self.stopping:
            self.wake.clear()
            try:
                # Recover generated but not yet frozen runs after a crash.
                ready=await self.store.call(lambda db:[r[0] for r in db.execute("SELECT id FROM runs WHERE kind='scheduled' AND status='ready'")])
                for ident in ready:
                    await self.freeze(ident)
                await self.reconcile(due=False)
                if await self.deliver_one():
                    await asyncio.sleep((await self.store.settings())['send_interval'])
                    continue
                self.last_error=None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error=type(exc).__name__
            try:
                await asyncio.wait_for(self.wake.wait(),timeout=2)
            except TimeoutError:
                pass
