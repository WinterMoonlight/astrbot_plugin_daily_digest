import asyncio
import base64
import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from digest.adapters import OneBot, SendResult
from digest.domain import TaskConfig, utcnow
from digest.generation import Generator
from digest.presentation import checked_report, report_text
from digest.rendering import ImageRenderer, RenderError, TemplateCatalog
from digest.runtime import Runtime
from digest.storage import Store
from digest.workflow_store import entity
from test_core import config, FakeModels, FakeHistory

TEMPLATES = Path(__file__).resolve().parents[1] / 'templates'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
REPORT = {'intro': '今天值得留意的事。', 'sections': [
    {'label': '安排', 'title': '说明会签到', 'body': '已报名同学请在九点前签到。', 'sources': ['bot/1']}]}


class ImageDomainTests(unittest.TestCase):
    def test_old_configs_default_to_text_and_bad_modes_rejected(self):
        self.assertEqual(config().output_mode, 'text')
        self.assertEqual(config(output_mode='image').template_id, 'shorekeeper')
        for values in ({'output_mode': 'html'}, {'template_id': '../other'}, {'template_id': ''}):
            with self.assertRaises(ValueError):
                config(**values)

    def test_one_report_has_one_plain_text_representation_and_no_source_ids(self):
        report = checked_report(REPORT, {'bot/1'})
        self.assertEqual(report_text(report), '今天值得留意的事。\n\n说明会签到\n已报名同学请在九点前签到。')
        bad = copy.deepcopy(REPORT)
        bad['sections'][0]['sources'] = ['unknown']
        with self.assertRaises(ValueError):
            checked_report(bad, {'bot/1'})

    def test_template_discovery_paths_and_font_declarations(self):
        catalog = TemplateCatalog(TEMPLATES)
        items, warnings = catalog.list()
        self.assertFalse(warnings)
        self.assertIn('shorekeeper', [x['id'] for x in items])
        with self.assertRaises(RenderError):
            catalog.load('../shorekeeper')
        with self.assertRaises(RenderError):
            catalog.file(TEMPLATES, '../main.py')
        loaded = catalog.load('shorekeeper')
        data = {**checked_report(REPORT, {'bot/1'}), 'title': '日常简报', 'date': '2026.11.14', 'closing': loaded['choices'][0]}
        data['sections'][0]['body'] = '</script><script>alert(1)</script>'
        payload = catalog.payload(loaded, data)
        self.assertEqual(payload['data']['sections'][0]['body'], '</script><script>alert(1)</script>')
        self.assertTrue(payload['font_files'])
        self.assertTrue(all(Path(p).is_relative_to(TEMPLATES / 'shorekeeper') for p in payload['font_files']))


class ImageGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_call_produces_both_formats_without_an_extra_model_call(self):
        models = FakeModels(json.dumps({'summary': '备用稿', 'report': REPORT, 'candidates': []}))
        result = await Generator(models, FakeHistory()).run(config(output_mode='image'), utcnow(), {'call_budget': 64})
        self.assertEqual(len(models.calls), 1)
        self.assertIn('report', models.calls[0])
        self.assertEqual(result['summary'], report_text(result['report']))

    async def test_invalid_image_structure_keeps_model_summary_and_candidate(self):
        candidate = {'title': '说明会签到', 'event_at': (utcnow()+timedelta(hours=2)).isoformat(), 'sources': ['bot/1']}
        models = FakeModels(json.dumps({'summary': '说明会签到：仅限报名同学。', 'report': None, 'candidates': [candidate]}))
        result = await Generator(models, FakeHistory()).run(config(output_mode='image'), utcnow(), {'call_budget': 64})
        self.assertEqual(result['summary'], '说明会签到：仅限报名同学。')
        self.assertEqual(len(result['candidates']), 1)
        self.assertNotIn('report', result)
        self.assertTrue(result['warnings'])


class FixtureRenderer(ImageRenderer):
    failure = None
    captured = 0

    async def capture(self, loaded, data):
        self.captured += 1
        if self.failure:
            raise self.failure
        return PNG, {'width': 1, 'height': 1}


class Sender:
    def __init__(self):
        self.calls = []
        self.results = []

    async def send_image(self, recipient, png):
        self.calls.append(('image', recipient, png))
        return self.results.pop(0) if self.results else SendResult('acknowledged', str(len(self.calls)))

    async def send_text(self, recipient, text):
        self.calls.append(('text', recipient, text))
        return SendResult('acknowledged', str(len(self.calls)))


class ImageWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'test.db')
        await self.store.initialize()
        self.task = await self.store.save_task(config(output_mode='image'))
        self.task = await self.store.set_enabled(self.task['id'], self.task['version'], True)
        self.renderer = FixtureRenderer(self.store, TEMPLATES)
        self.sender = Sender()
        self.runtime = Runtime(self.store, SimpleNamespace(renderer=self.renderer, wake=asyncio.Event()), self.sender)

    async def asyncTearDown(self):
        await self.renderer.close()
        self.tmp.cleanup()

    async def make_run(self, kind='scheduled'):
        run_id = await self.store.enqueue(self.task['id'], utcnow(), kind)
        run = await self.store.get_run(run_id)
        report = checked_report(REPORT, {'bot/1'})
        result = {'summary': report_text(report), 'report': report, 'warnings': [], 'changes': [],
                  'candidates': [{'title':'说明会签到', 'event_at':(utcnow()+timedelta(hours=2)).isoformat(), 'sources':['bot/1'], 'place':'教室', 'condition':'已报名同学'}]}
        result = await self.renderer.present(run, TaskConfig.from_dict(self.task['config']), result)
        await self.store.finish(run_id, 'ready', result)
        return await self.store.get_run(run_id)

    async def test_image_ack_precedes_text_inquiry_and_restart_resends_no_image(self):
        run = await self.make_run()
        await self.runtime.freeze(run['id'])
        self.assertEqual(len((await self.store.delivery_detail(run['id']))['parts']), 1)
        await self.runtime.deliver_one()
        self.assertEqual(self.sender.calls[0][0], 'image')
        await self.runtime.deliver_one()
        self.assertEqual(self.sender.calls[1][0], 'text')
        self.assertIn('日程', self.sender.calls[1][2])
        await self.store.initialize()
        await self.runtime.freeze(run['id'])
        self.assertFalse(await self.runtime.deliver_one())
        self.assertEqual(len(self.sender.calls), 2)

    async def test_unknown_image_is_not_retried_or_replaced_by_text(self):
        run = await self.make_run()
        await self.runtime.freeze(run['id'])
        self.sender.results = [SendResult('unknown')]
        await self.runtime.deliver_one()
        await self.store.initialize()
        await self.runtime.reconcile(due=False)
        self.assertFalse(await self.runtime.deliver_one())
        self.assertEqual(len(self.sender.calls), 1)
        with self.assertRaises(ValueError):
            await self.store.authorize_delivery(run['id'], 'digest')
        self.assertEqual((await self.store.get_run(run['id']))['status'], 'unknown')

    async def test_explicit_image_failure_manual_retry_uses_identical_bytes(self):
        run = await self.make_run()
        await self.runtime.freeze(run['id'])
        self.sender.results = [SendResult('failed')]
        await self.runtime.deliver_one()
        await self.store.authorize_delivery(run['id'], 'digest')
        await self.runtime.deliver_one()
        self.assertEqual(self.sender.calls[0], self.sender.calls[1])
        self.assertEqual(self.renderer.captured, 1)

    async def test_render_failure_falls_back_to_full_text_then_inquiry(self):
        self.renderer.failure = ImportError('browser dependency missing')
        run = await self.make_run()
        self.assertEqual(run['result']['delivery']['format'], 'text')
        self.assertIn('回退文字', run['result']['warnings'][-1])
        await self.runtime.freeze(run['id'])
        await self.runtime.deliver_one()
        self.assertEqual(self.sender.calls[0][0], 'text')
        self.assertEqual(self.sender.calls[0][2], run['result']['summary'])
        await self.runtime.deliver_one()
        self.assertIn('日程', self.sender.calls[1][2])

    async def test_missing_cached_image_fails_before_network_call(self):
        run = await self.make_run()
        await self.runtime.freeze(run['id'])
        (self.renderer.directory / run['result']['delivery']['file']).unlink()
        await self.runtime.deliver_one()
        self.assertFalse(self.sender.calls)
        part = (await self.store.delivery_detail(run['id']))['parts'][0]
        self.assertEqual(part['state'], 'failed')

    async def test_preview_image_is_read_through_its_run_and_hash_checked(self):
        run = await self.make_run('preview')
        self.assertEqual((await self.renderer.preview(run['id']))['src'], 'data:image/png;base64,'+base64.b64encode(PNG).decode())
        self.assertFalse((await self.store.delivery_detail(run['id']))['parts'])
        (self.renderer.directory / run['result']['delivery']['file']).write_bytes(PNG+b'changed')
        with self.assertRaises(RenderError):
            await self.renderer.preview(run['id'])
        with self.assertRaises(RenderError):
            await self.renderer.read('../test.db', '')

    async def test_cleanup_keeps_pending_images_and_reclaims_unreferenced_files(self):
        run = await self.make_run()
        await self.runtime.freeze(run['id'])
        path = self.renderer.directory / run['result']['delivery']['file']
        os.utime(path, (1, 1))
        orphan = self.renderer.directory / ('a'*32+'.png')
        orphan.write_bytes(PNG); os.utime(orphan, (1, 1))
        await self.renderer.cleanup()
        self.assertTrue(path.exists())
        self.assertFalse(orphan.exists())

    async def test_closing_is_frozen_and_history_survives_restart(self):
        pool = [{'id':'a','title':'A','note':'a'},{'id':'b','title':'B','note':'b'}]
        run = await self.make_run()
        original = run['result']['delivery']['closing']
        # Same run always keeps its original choice, even if the library changes.
        self.assertEqual(await self.store.choose_closing(run, 'shorekeeper', pool), original)
        await self.store.initialize()
        run2_id = await self.store.enqueue(self.task['id'], utcnow(), 'scheduled')
        run2 = await self.store.get_run(run2_id)
        first = await self.store.choose_closing(run2, 'alternate', pool)
        run3 = await self.store.get_run(await self.store.enqueue(self.task['id'], utcnow(), 'scheduled'))
        second = await self.store.choose_closing(run3, 'alternate', pool)
        self.assertNotEqual(first['id'], second['id'])
        preview = await self.store.get_run(await self.store.enqueue(self.task['id'], utcnow(), 'preview'))
        await self.store.choose_closing(preview, 'alternate', pool)
        self.assertEqual(await self.store.call(lambda db: db.execute('SELECT count(*) FROM render_history').fetchone()[0]), 3)


class ImageAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_uses_base64_not_a_path_in_the_other_container(self):
        calls = []
        async def call_action(action, **kwargs):
            calls.append((action, kwargs))
            return {'message_id': 123}
        platform = SimpleNamespace(meta=lambda:SimpleNamespace(id='bot',name='aiocqhttp'), bot=SimpleNamespace(call_action=call_action))
        sender = OneBot(SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[platform])))
        result = await sender.send_image('bot:FriendMessage:456', PNG)
        self.assertEqual(result.state, 'acknowledged')
        self.assertEqual(calls[0][1]['message'][0], {'type':'image','data':{'file':'base64://'+base64.b64encode(PNG).decode()}})
