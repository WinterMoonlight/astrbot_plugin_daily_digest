"""Real Typst worker tests: no browser or network is needed."""
import json
import asyncio
import os
import shutil
import unittest
from pathlib import Path

from digest.domain import TaskConfig, utcnow
from digest.rendering import ImageRenderer, TemplateCatalog
from digest.presentation import report_text
import test_images as fixtures

TEMPLATES = fixtures.TEMPLATES


class TypstTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Reuse only setup, not the fixture-renderer tests themselves.
        await fixtures.ImageWorkflowTests.asyncSetUp(self)
        self.renderer = ImageRenderer(self.store, TEMPLATES)

    async def asyncTearDown(self):
        await fixtures.ImageWorkflowTests.asyncTearDown(self)

    async def render(self, report, config=None):
        run = await self.store.get_run(await self.store.enqueue(self.task['id'], utcnow(), 'preview'))
        return await self.renderer.present(run, config or TaskConfig.from_dict(self.task['config']),
            {'summary': report_text(report), 'report': report, 'warnings': []})

    async def test_real_template_short_long_and_extreme_reports(self):
        for name in ('standard', 'long', 'extended'):
            sample = json.loads((TEMPLATES / f'shorekeeper/samples/{name}.json').read_text(encoding='utf-8'))
            report = {'intro': sample['intro'], 'sections': sample['sections']}
            result = await self.render(report)
            self.assertEqual(result['delivery']['format'], 'image', result['warnings'])
            self.assertEqual(result['delivery']['width'], 1080)
            self.assertGreater(result['delivery']['height'], 1000)
            self.assertEqual(result['summary'], report_text(report))
        report = {'intro':'超长正文', 'sections':[{'label':'安排','title':'请完整保留','body':'一段长通知。'*3000}]}
        result = await self.render(report)
        self.assertEqual(result['delivery']['format'], 'text')
        self.assertIn('高度限制', result['delivery']['reason'])
        self.assertEqual(result['summary'], report_text(report))

    async def test_second_template_without_character_fonts_or_stickers(self):
        templates = Path(self.tmp.name) / 'templates'
        root = templates / 'minimal'
        root.mkdir(parents=True)
        shutil.copyfile(TEMPLATES / 'template.schema.json', templates / 'template.schema.json')
        manifest = {'schemaVersion':2, 'engine':'typst', 'id':'minimal','name':'Minimal','version':'1.0.0','width':800,
            'entry':'report.typ'}
        (root / 'template.json').write_text(json.dumps(manifest),encoding='utf-8')
        (root / 'report.typ').write_text('#let data = json(bytes(sys.inputs.data))\n#set page(width:400pt,height:auto,margin:10pt)\n#for item in data.sections { text(item.body) }',encoding='utf-8')
        self.renderer.catalog = TemplateCatalog(templates)
        report = {'intro':'另一种模板','sections':[{'label':'提示','title':'标题','body':'<script>alert(1)</script> #panic(42) is literal text.'}]}
        cfg = TaskConfig.from_dict({**self.task['config'],'template_id':'minimal'})
        result = await self.render(report, cfg)
        self.assertEqual(result['delivery']['format'], 'image', result['warnings'])
        self.assertEqual(result['delivery']['template_id'], 'minimal')
        self.assertIsNone(result['delivery']['closing'])
        self.assertEqual(result['delivery']['width'], 800)

    async def test_worker_exits_after_render_and_next_render_works(self):
        report = {'intro':'测试', 'sections':[{'label':'事项','title':'休息一下','body':'正文保持完整。'}]}
        for _ in range(2):
            result = await self.render(report)
            self.assertEqual(result['delivery']['format'], 'image', result['warnings'])
            self.assertFalse(self.renderer.workers)
            self.assertFalse(list(self.renderer.directory.glob('.typst-*')))

    async def test_timeout_reaps_worker_and_preserves_text(self):
        self.renderer.worker_timeout = 0.001
        report = {'intro':'测试', 'sections':[{'label':'事项','title':'超时','body':'正文不会丢失。'}]}
        result = await self.render(report)
        self.assertEqual(result['delivery']['format'], 'text')
        self.assertIn('超时', result['delivery']['reason'])
        self.assertEqual(result['summary'], report_text(report))
        self.assertFalse(self.renderer.workers)
        self.assertFalse(list(self.renderer.directory.glob('.typst-*')))
