"""Single-process durable repository. No network calls inside transactions."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from datetime import timedelta
from pathlib import Path

from .domain import TaskConfig, parse_time, utcnow
from .workflow_store import WorkflowStore


class Conflict(ValueError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class Store(WorkflowStore):
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()

    async def call(self, function):
        async with self.lock:
            return await asyncio.to_thread(self._call, function)

    def _call(self, function):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            with db:
                db.execute('BEGIN IMMEDIATE')
                return function(db)
        finally:
            db.close()

    async def initialize(self):
        def setup(db):
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone():
                version=db.execute('SELECT MAX(version) FROM schema_version').fetchone()[0]
                if version not in (1,2):
                    raise RuntimeError('数据库版本不兼容，拒绝降级写入')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO schema_version VALUES (1);
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, version INTEGER NOT NULL, config TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
                    next_at TEXT, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                    slot TEXT NOT NULL, kind TEXT NOT NULL, snapshot TEXT NOT NULL,
                    status TEXT NOT NULL, result TEXT, error TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, UNIQUE(task_id,slot,kind));
                CREATE TABLE IF NOT EXISTS parts (
                    run_id TEXT NOT NULL REFERENCES runs(id), channel TEXT NOT NULL,
                    ordinal INTEGER NOT NULL, body TEXT, state TEXT NOT NULL,
                    receipt TEXT, PRIMARY KEY(run_id,channel,ordinal));
                CREATE INDEX IF NOT EXISTS run_status ON runs(status,created_at);
            ''')
            version = db.execute('SELECT MAX(version) FROM schema_version').fetchone()[0]
            if version not in (1, 2):
                raise RuntimeError('数据库版本不兼容，拒绝降级写入')
            db.execute('INSERT OR IGNORE INTO settings VALUES (1,?)', (encode({
                'concurrency': 1, 'retention_days': 30, 'text_limit': 1500,
                'send_interval': 1, 'generation_timeout': 1200, 'call_budget': 64}),))
            # Generated previews are retryable, sent messages are not.
            db.execute("UPDATE runs SET status='queued' WHERE status='generating'")
            db.execute("UPDATE parts SET state='unknown' WHERE state='sending'")
        await self.call(setup)
        await self.migrate_workflow()

    @staticmethod
    def task(row):
        if row is None:
            raise ValueError('任务不存在')
        result = dict(row)
        result['config'] = json.loads(result['config'])
        result['enabled'] = bool(result['enabled'])
        result['archived'] = bool(result['archived'])
        return result

    async def list_tasks(self, include_archived=False):
        return await self.call(lambda db: [self.task(r) for r in db.execute(
            'SELECT * FROM tasks '+('' if include_archived else 'WHERE archived=0 ')+'ORDER BY updated_at DESC')])

    async def get_task(self, task_id):
        return await self.call(lambda db: self.task(db.execute(
            'SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()))

    async def save_task(self, config: TaskConfig, task_id=None, version=None):
        config.validate(False)
        now = utcnow().isoformat()
        def save(db):
            if task_id:
                old = self.task(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())
                if old['archived'] or old['version'] != version:
                    raise Conflict('任务已被修改或归档，请刷新')
                if old['enabled']:
                    config.validate()
                next_at = config.next_after(utcnow()).isoformat() if old['enabled'] else None
                db.execute('UPDATE tasks SET config=?,version=version+1,next_at=?,updated_at=? WHERE id=?',
                           (encode(config.to_dict()), next_at, now, task_id))
                result_id = task_id
            else:
                result_id = uuid.uuid4().hex
                db.execute('INSERT INTO tasks(id,version,config,updated_at) VALUES (?,1,?,?)',
                           (result_id, encode(config.to_dict()), now))
            return self.task(db.execute('SELECT * FROM tasks WHERE id=?', (result_id,)).fetchone())
        return await self.call(save)

    async def set_enabled(self, task_id, version, enabled, archive=False):
        def change(db):
            task = self.task(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())
            if task['version'] != version or task['archived']:
                raise Conflict('任务已被修改，请刷新')
            config = TaskConfig.from_dict(task['config'])
            if enabled:
                config.validate()
            next_at = config.next_after(utcnow()).isoformat() if enabled else None
            db.execute('UPDATE tasks SET enabled=?,archived=?,next_at=?,version=version+1,updated_at=? WHERE id=?',
                       (int(enabled), int(archive), next_at, utcnow().isoformat(), task_id))
            if not enabled:
                db.execute("UPDATE runs SET status='cancelled' WHERE task_id=? AND status='queued'", (task_id,))
                db.execute("INSERT OR IGNORE INTO delivery(run_id,authorized) SELECT id,0 FROM runs WHERE task_id=? AND kind='scheduled'",(task_id,))
                db.execute('UPDATE delivery SET authorized=0 WHERE run_id IN (SELECT id FROM runs WHERE task_id=?)',(task_id,))
                db.execute("UPDATE outbox SET state='held' WHERE task_id=? AND state='pending'",(task_id,))
            return self.task(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())
        return await self.call(change)

    async def settings(self, updates=None):
        def change(db):
            data = json.loads(db.execute('SELECT data FROM settings WHERE id=1').fetchone()[0])
            if updates is not None:
                if set(updates) - set(data):
                    raise ValueError('未知全局设置')
                for key, value in updates.items():
                    if type(value) is not int or value < 1:
                        raise ValueError('全局设置必须为正整数')
                if updates.get('text_limit', data['text_limit']) < 32:
                    raise ValueError('单条文本预算至少 32 字符')
                data.update(updates)
                db.execute('UPDATE settings SET data=? WHERE id=1', (encode(data),))
            return data
        return await self.call(change)

    async def enqueue(self, task_id, planned=None, kind='preview'):
        if kind not in ('preview', 'scheduled'):
            raise ValueError('未知执行类型')
        def insert(db):
            task = self.task(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())
            if task['archived'] or (kind == 'scheduled' and not task['enabled']):
                raise ValueError('任务已停用或归档')
            TaskConfig.from_dict(task['config']).validate()
            run_id = uuid.uuid4().hex
            slot = (planned or utcnow()).isoformat()
            # Preview IDs are unique; scheduled slots remain stable across config edits.
            unique_slot = slot if kind == 'scheduled' else slot + '/' + run_id
            now = utcnow().isoformat()
            snapshot = {'config': task['config'], 'version': task['version'], 'planned_at': slot}
            db.execute('INSERT OR IGNORE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (run_id, task_id, unique_slot, kind, encode(snapshot), 'queued', None, None, now, now))
            return db.execute('SELECT id FROM runs WHERE task_id=? AND slot=? AND kind=?',
                              (task_id, unique_slot, kind)).fetchone()[0]
        return await self.call(insert)

    async def claim(self, active_tasks):
        def claim(db):
            for row in db.execute("SELECT * FROM runs WHERE status='queued' ORDER BY created_at,id"):
                if row['task_id'] not in active_tasks:
                    db.execute("UPDATE runs SET status='generating',updated_at=? WHERE id=? AND status='queued'",
                               (utcnow().isoformat(), row['id']))
                    return self.run(row)
            return None
        return await self.call(claim)

    @staticmethod
    def run(row):
        if row is None:
            raise ValueError('执行记录不存在')
        data = dict(row)
        for key in ('snapshot', 'result'):
            data[key] = json.loads(data[key]) if data[key] else None
        return data

    async def finish(self, run_id, status, result=None, error=None):
        if status not in ('ready', 'empty', 'failed', 'cancelled'):
            raise ValueError('无效终态')
        await self.call(lambda db: db.execute(
            'UPDATE runs SET status=?,result=?,error=?,updated_at=? WHERE id=?',
            (status, encode(result) if result is not None else None, error, utcnow().isoformat(), run_id)))

    async def get_run(self, run_id):
        return await self.call(lambda db: self.run(db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()))

    async def list_runs(self, task_id, page=1):
        return await self.call(lambda db: [self.run(r) for r in db.execute(
            'SELECT * FROM runs WHERE task_id=? ORDER BY created_at DESC LIMIT 30 OFFSET ?',
            (task_id, (max(1, page) - 1) * 30))])

    async def cleanup(self, days):
        cutoff = (utcnow() - timedelta(days=days)).isoformat()
        # Ledger rows stay; only terminal details without pending delivery are cleared.
        await self.call(lambda db: db.execute('''UPDATE runs SET result=NULL,error=NULL
            WHERE updated_at<? AND status IN ('empty','failed','cancelled','ready')
            AND kind='preview' AND NOT EXISTS (SELECT 1 FROM parts WHERE parts.run_id=runs.id)''', (cutoff,)))
        await self.cleanup_workflow(days)
