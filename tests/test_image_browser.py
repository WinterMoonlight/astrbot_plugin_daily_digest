"""Opt-in real browser tests: DIGEST_BROWSER_TESTS=1 python -m unittest discover -s tests."""
import json
import os
import shutil
import unittest
from pathlib import Path

from digest.domain import TaskConfig, utcnow
from digest.rendering import ImageRenderer, TemplateCatalog
from digest.presentation import report_text
import test_images as fixtures

TEMPLATES = fixtures.TEMPLATES


@unittest.skipUnless(os.environ.get('DIGEST_BROWSER_TESTS') == '1', 'real browser tests are opt-in')
class BrowserTests(unittest.IsolatedAsyncioTestCase):
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
        (root / 'components').mkdir(parents=True)
        (root / 'styles').mkdir()
        (templates / 'common').mkdir()
        shutil.copyfile(TEMPLATES / 'template.schema.json', templates / 'template.schema.json')
        shutil.copyfile(TEMPLATES / 'common/layout.js', templates / 'common/layout.js')
        shutil.copyfile(TEMPLATES / 'shorekeeper/template.html', root / 'template.html')
        manifest = {'schemaVersion':1,'id':'minimal','name':'Minimal','version':'1.0.0','width':800,'viewportHeight':900,
            'entry':'template.html','layout':['header','content-panel'],'styles':['page.css'], 'components':['header','content-panel','card']}
        (root / 'template.json').write_text(json.dumps(manifest),encoding='utf-8')
        (root / 'components/header.html').write_text('<header><h1 data-field="report-title"></h1><p data-field="intro"></p></header>',encoding='utf-8')
        (root / 'components/content-panel.html').write_text('<section class="card-stack"></section>',encoding='utf-8')
        (root / 'components/card.html').write_text('<article><h2 data-field="title"></h2><p class="card-body" data-field="body"></p></article>',encoding='utf-8')
        (root / 'styles/page.css').write_text('body{margin:0} .report-page{width:800px} .card-body{white-space:pre-wrap;overflow-wrap:anywhere}',encoding='utf-8')
        self.renderer.catalog = TemplateCatalog(templates)
        report = {'intro':'另一种模板','sections':[{'label':'提示','title':'标题','body':'<script>alert(1)</script> 作为普通文字显示。'}]}
        cfg = TaskConfig.from_dict({**self.task['config'],'template_id':'minimal'})
        result = await self.render(report, cfg)
        self.assertEqual(result['delivery']['format'], 'image', result['warnings'])
        self.assertEqual(result['delivery']['template_id'], 'minimal')
        self.assertIsNone(result['delivery']['closing'])
        self.assertEqual(result['delivery']['width'], 800)
