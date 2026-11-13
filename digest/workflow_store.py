"""Transactional workflow ledger; never calls a model or network in a transaction."""
import hashlib
import json
import uuid
from datetime import timedelta

from .domain import TaskConfig, parse_time, split_text, utcnow


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def uid():
    return uuid.uuid4().hex


def entities(db, kind, recipient=None):
    sql, args = 'SELECT data FROM entities WHERE kind=?', [kind]
    if recipient is not None:
        sql += ' AND recipient=?'; args.append(recipient)
    return [json.loads(r[0]) for r in db.execute(sql, args)]


def entity(db, kind, ident):
    row = db.execute('SELECT data FROM entities WHERE kind=? AND id=?', (kind, ident)).fetchone()
    if row is None:
        raise ValueError('关联记录不存在')
    return json.loads(row[0])


def put(db, kind, data):
    db.execute('INSERT INTO entities VALUES (?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data',
               (kind, data['id'], data['recipient'], data.get('task_id', ''), dump(data)))


def outgoing(db, owner, channel, recipient, body, limit=1500, task_id=None):
    for index, part in enumerate(split_text(body, limit)):
        db.execute('''INSERT OR IGNORE INTO outbox
            (id,owner,channel,ordinal,recipient,body,hash,state,attempts,next_at,task_id,created_at)
            VALUES (?,?,?,?,?,?,?,'pending',0,?,?,?)''',
            (uid(), owner, channel, index, recipient, part, hashlib.sha256(part.encode()).hexdigest(),
             utcnow().isoformat(), task_id, utcnow().isoformat()))


class WorkflowStore:
    async def migrate_workflow(self):
        def migrate(db):
            # v4 removes timed reply fields; older code must not read the new dialogue format.
            db.executescript('''
                CREATE TABLE IF NOT EXISTS entities (
                    kind TEXT NOT NULL,id TEXT NOT NULL,recipient TEXT NOT NULL,
                    task_id TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(kind,id));
                CREATE INDEX IF NOT EXISTS entity_recipient ON entities(kind,recipient);
                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY,owner TEXT NOT NULL,channel TEXT NOT NULL,ordinal INTEGER NOT NULL,
                    recipient TEXT NOT NULL,body TEXT,hash TEXT NOT NULL,state TEXT NOT NULL,
                    attempts INTEGER NOT NULL,next_at TEXT NOT NULL,task_id TEXT,receipt TEXT,error TEXT,
                    created_at TEXT NOT NULL,UNIQUE(owner,channel,ordinal));
                CREATE INDEX IF NOT EXISTS outgoing_due ON outbox(state,next_at);
                CREATE TABLE IF NOT EXISTS delivery (
                    run_id TEXT PRIMARY KEY REFERENCES runs(id),authorized INTEGER NOT NULL DEFAULT 0,
                    manual INTEGER NOT NULL DEFAULT 0,completed INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS interactions (
                    recipient TEXT NOT NULL,message_id TEXT NOT NULL,state TEXT NOT NULL,
                    result TEXT,created_at TEXT NOT NULL DEFAULT '',PRIMARY KEY(recipient,message_id));
                INSERT OR IGNORE INTO schema_version VALUES (2);
                INSERT OR IGNORE INTO schema_version VALUES (3);
                INSERT OR IGNORE INTO schema_version VALUES (4);
            ''')
            if 'created_at' not in {r[1] for r in db.execute('PRAGMA table_info(interactions)')}:
                db.execute("ALTER TABLE interactions ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
            for column in ('sent_at','ack_at'):
                if column not in {r[1] for r in db.execute('PRAGMA table_info(outbox)')}:
                    db.execute(f'ALTER TABLE outbox ADD COLUMN {column} TEXT')
            for record in entities(db, 'dialogue'):
                if 'window_minutes' in record or 'expires_at' in record:
                    record.pop('window_minutes', None)
                    record.pop('expires_at', None)
                    put(db, 'dialogue', record)  # Keep remaining quota, history and closed states.
            # v0.3.1 retained quote receipts but did not mark deleted change proposals.
            # Only already-scrubbed, orphaned change rows qualify for this safe backfill.
            for row in db.execute("""SELECT DISTINCT owner,recipient FROM outbox o
                    WHERE channel='change' AND body IS NULL AND NOT EXISTS
                    (SELECT 1 FROM entities e WHERE e.kind='change' AND e.id=o.owner)""").fetchall():
                put(db,'deleted_change',{'id':row['owner'],'recipient':row['recipient']})
            db.execute("UPDATE outbox SET state='unknown',error='发送中断，需核对回执' WHERE state='sending'")
            db.execute("UPDATE interactions SET state='interrupted' WHERE state='processing'")
            for row in db.execute("SELECT recipient,message_id FROM interactions WHERE state='interrupted'").fetchall():
                outgoing(db,row['recipient']+'/'+row['message_id'],'reply',row['recipient'],
                         '日程回复处理期间插件重启，未自动重放操作。请重新说明事项及希望的操作；已经创建的提醒不会重复创建。')
                db.execute("UPDATE interactions SET state='done' WHERE recipient=? AND message_id=?",(row['recipient'],row['message_id']))
        await self.call(migrate)

    async def schedule(self, now=None, restart=False):
        now = now or utcnow()
        def work(db):
            count = 0
            for row in db.execute('SELECT * FROM tasks WHERE enabled=1 AND archived=0').fetchall():
                task = self.task(row); config = TaskConfig.from_dict(task['config'])
                slot = parse_time(task['next_at']) if task['next_at'] else config.next_after(now)
                while slot <= now:
                    # During normal operation tolerate the minute trigger's jitter only.
                    missed = restart or (now-slot).total_seconds() >= 60
                    ident, stamp = uid(), now.isoformat()
                    snapshot = {'config': task['config'], 'version': task['version'], 'planned_at': slot.isoformat()}
                    db.execute('INSERT OR IGNORE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (ident, task['id'], slot.isoformat(), 'scheduled', dump(snapshot),
                         'missed' if missed else 'queued', None, '停机或调度遗漏，需手动补发' if missed else None, stamp, stamp))
                    actual = db.execute("SELECT id FROM runs WHERE task_id=? AND slot=? AND kind='scheduled'",
                                        (task['id'],slot.isoformat())).fetchone()[0]
                    db.execute('INSERT OR IGNORE INTO delivery(run_id,authorized) VALUES (?,?)', (actual, int(not missed)))
                    count += 1
                    slot = config.next_after(slot)
                db.execute('UPDATE tasks SET next_at=? WHERE id=?', (slot.isoformat(), task['id']))
            return count
        return await self.call(work)

    async def delivery_detail(self, run_id):
        def read(db):
            run = self.run(db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())
            return {'run': run, 'parts': [dict(r) for r in db.execute(
                'SELECT * FROM outbox WHERE owner=? ORDER BY channel,ordinal', (run_id,))]}
        return await self.call(read)

    async def prepare_catchup(self, run_id):
        def prepare(db):
            run = self.run(db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())
            if run['kind'] != 'scheduled':
                raise ValueError('只能补发正式计划，预览不能直接投递')
            if run['status'] in ('queued','generating','delivering'):
                raise ValueError('本次执行仍在进行中')
            if db.execute("SELECT 1 FROM outbox WHERE owner=? AND state='sending'", (run_id,)).fetchone():
                raise ValueError('仍有发送中的分条')
            db.execute('INSERT OR IGNORE INTO delivery(run_id) VALUES (?)', (run_id,))
            db.execute('UPDATE delivery SET authorized=0,manual=1 WHERE run_id=?', (run_id,))
            has_parts = db.execute('SELECT 1 FROM outbox WHERE owner=?', (run_id,)).fetchone()
            if not has_parts and (not run['result'] or not run['result'].get('summary')):
                db.execute("UPDATE runs SET status='queued',error='根据现存历史重新生成，未恢复原稿' WHERE id=?", (run_id,))
            return run_id
        return await self.call(prepare)

    async def authorize_delivery(self, run_id, channel):
        if channel not in ('digest','inquiry'):
            raise ValueError('未知投递部分')
        def authorize(db):
            run = self.run(db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())
            task = self.task(db.execute('SELECT * FROM tasks WHERE id=?', (run['task_id'],)).fetchone())
            if not task['enabled'] or task['archived']:
                raise ValueError('请先启用任务，再明确提交补发')
            if run['kind'] != 'scheduled' or run['status'] in ('queued','generating'):
                raise ValueError('请先完成该计划的补发预览')
            rows = db.execute('SELECT * FROM outbox WHERE owner=? AND channel=?', (run_id,channel)).fetchall()
            if not rows:
                raise ValueError('当前没有可补发正文；请先生成补发预览')
            if all(r['state']=='acknowledged' for r in rows):
                raise ValueError('该部分已经全部发送成功，无需补发')
            if any(r['state'] in ('unknown','sending') for r in rows):
                raise ValueError('存在结果未知或发送中的分条，必须先核对')
            if any(r['body'] is None and r['state'] != 'acknowledged' for r in rows):
                raise ValueError('原文已清理，无法补发')
            db.execute('UPDATE delivery SET authorized=1 WHERE run_id=?', (run_id,))
            db.execute("UPDATE outbox SET state='pending',next_at=? WHERE owner=? AND channel=? AND state IN ('failed','held')",
                       (utcnow().isoformat(),run_id,channel))
        await self.call(authorize)

    async def resolve_unknown(self, ident, outcome):
        if outcome not in ('acknowledged','failed'):
            raise ValueError('只能标记核实已发送或核实未发送')
        def resolve(db):
            row = db.execute('SELECT * FROM outbox WHERE id=?', (ident,)).fetchone()
            if row is None or row['state'] != 'unknown':
                raise ValueError('仅能核对结果未知的投递')
            db.execute('UPDATE outbox SET state=?,receipt=?,error=? WHERE id=?',
                       (outcome, 'manual-verification' if outcome=='acknowledged' else None, '管理员核对',ident))
            if outcome=='acknowledged':
                db.execute('UPDATE outbox SET ack_at=COALESCE(sent_at,created_at) WHERE id=?',(ident,))
            # Definite failure after manual verification still requires explicit resubmission.
        await self.call(resolve)

    async def related(self, task_id):
        def read(db):
            result = {kind: [x for x in entities(db,kind) if x.get('task_id')==task_id or
                          task_id in x.get('task_ids',[])] for kind in ('list','reminder','change')}
            owners={x['id'] for x in result['change']} | {x['run_id'] for x in result['list']}
            owners.update(f"{x['id']}:{x['version']}" for x in result['reminder'])
            result['outbox']=[dict(r) for r in db.execute('SELECT * FROM outbox ORDER BY created_at') if r['owner'] in owners]
            return result
        return await self.call(read)

    async def cleanup_workflow(self, days):
        cutoff=(utcnow()-timedelta(days=days)).isoformat()
        def clean(db):
            active_runs={x['run_id'] for x in entities(db,'list') if x['state'] in ('active','awaiting_delivery')}
            for reminder in entities(db,'reminder'):
                if reminder['state'] not in ('cancelled','missed','acknowledged'):
                    active_runs.update(reminder['list_ids'])
            for change in entities(db,'change'):
                if change['state']=='pending':
                    proposal=change['proposal']
                    if proposal.get('list_id'):
                        active_runs.add(proposal['list_id'])
                    if proposal.get('reminder_id'):
                        active_runs.update(entity(db,'reminder',proposal['reminder_id'])['list_ids'])
            for row in db.execute("SELECT id FROM runs WHERE kind='scheduled' AND updated_at<? AND status IN ('sent','empty','cancelled','missed','failed')",(cutoff,)).fetchall():
                if row['id'] in active_runs or db.execute("SELECT 1 FROM outbox WHERE owner=? AND state!='acknowledged'",(row['id'],)).fetchone():
                    continue
                db.execute('UPDATE runs SET result=NULL,error=NULL WHERE id=?',(row['id'],))
                db.execute('UPDATE outbox SET body=NULL,error=NULL WHERE owner=?',(row['id'],))
            db.execute("UPDATE interactions SET result=NULL WHERE state IN ('done','ignored') AND created_at<?",(cutoff,))
            db.execute("UPDATE outbox SET body=NULL,error=NULL WHERE channel='reply' AND state='acknowledged' AND created_at<?",(cutoff,))
            # Minimal identities and receipt hashes remain for duplicate suppression.
        await self.call(clean)
