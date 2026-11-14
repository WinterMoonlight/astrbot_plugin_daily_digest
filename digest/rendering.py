"""Local, bounded HTML rendering. Templates supply presentation, never message delivery."""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from .domain import parse_time

MAX_HEIGHT = 12000
MAX_PNG_BYTES = 8 * 1024 * 1024
FILE_NAME = re.compile(r'^[a-f0-9]{32}\.png$')


class RenderError(ValueError):
    pass


def safe_json(value):
    return json.dumps(value, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e')


class TemplateCatalog:
    def __init__(self, root):
        self.root = Path(root).resolve()

    @staticmethod
    def file(root, relative):
        if not isinstance(relative, str) or '\\' in relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
            raise RenderError('模板资源路径无效')
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise RenderError('模板资源不能位于模板目录以外')
        return path

    def load(self, template_id):
        if not isinstance(template_id, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', template_id):
            raise RenderError('无效的模板标识')
        root = (self.root / template_id).resolve()
        if not root.is_relative_to(self.root) or not root.is_dir():
            raise RenderError('所选图片模板不存在')
        try:
            from jsonschema import Draft202012Validator
            schema = json.loads((self.root / 'template.schema.json').read_text(encoding='utf-8'))
            manifest = json.loads((root / 'template.json').read_text(encoding='utf-8'))
            errors = list(Draft202012Validator(schema).iter_errors(manifest))
            if errors:
                raise RenderError('模板清单不符合规范')
            if manifest['id'] != template_id or not 640 <= manifest['width'] <= 1600:
                raise RenderError('模板标识或画布宽度无效')
            components = manifest['components']
            if not {'header', 'content-panel', 'card'}.issubset(components):
                raise RenderError('模板缺少必要组件')
            if not all(re.fullmatch(r'[a-z][a-z0-9_-]*', name) for name in components):
                raise RenderError('模板组件名称无效')
            for name in manifest['styles']:
                self.file(root, 'styles/' + name).read_text(encoding='utf-8')
            for name in components:
                self.file(root, 'components/' + name + '.html').read_text(encoding='utf-8')
            self.file(root, manifest['entry']).read_text(encoding='utf-8')
            if not {'header', 'content-panel'}.issubset(manifest['layout']) or not set(manifest['layout']).issubset(components):
                raise RenderError('模板布局缺少必要组件')
            for name in manifest.get('assets', {}).values():
                if not self.file(root, 'assets/' + name).is_file():
                    raise RenderError('模板图片素材缺失')
            stickers = manifest.get('stickers', {})
            for name in stickers.values():
                if not self.file(root, 'assets/' + name).is_file():
                    raise RenderError('模板表情素材缺失')
            if stickers and (not manifest.get('stickerCycle') or any(n not in stickers for n in manifest['stickerCycle'])):
                raise RenderError('模板表情轮换配置无效')
            fonts = manifest.get('fonts', {})
            families = {face['family'] for face in fonts.get('faces', [])}
            warnings = []
            for face in fonts.get('faces', []):
                if face['distribution'] == 'private' and not face['file'].startswith('assets/fonts/private/'):
                    raise RenderError('私有字体必须放在模板的 private 目录')
                if not self.file(root, face['file']).is_file():
                    warnings.append(f"字体缺失：{face['file']}，将使用回退字体")
            for role in fonts.get('roles', {}).values():
                if role['family'] not in families or any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9 -]*', n) for n in role['fallback']):
                    raise RenderError('字体角色配置无效')
            choices = []
            if manifest.get('closingCopy'):
                choices = json.loads(self.file(root, manifest['closingCopy']).read_text(encoding='utf-8'))
                if not isinstance(choices, list) or not choices or len(choices) > 100:
                    raise RenderError('收尾短句库无效')
                seen = set()
                for item in choices:
                    if not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k].strip() or len(item[k]) > 300 for k in ('id', 'title', 'note')) or item['id'] in seen:
                        raise RenderError('收尾短句库格式无效')
                    seen.add(item['id'])
            return {'root': root, 'manifest': manifest, 'choices': choices, 'warnings': warnings}
        except (OSError, json.JSONDecodeError) as exc:
            raise RenderError('模板文件缺失或无法读取') from exc

    def list(self):
        items, warnings = [], []
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or not (directory / 'template.json').is_file():
                continue
            try:
                loaded = self.load(directory.name)
                m = loaded['manifest']
                items.append({'id': m['id'], 'name': m['name'], 'version': m['version'], 'warnings': loaded['warnings']})
            except (ValueError, ImportError):
                warnings.append(f'模板 {directory.name} 不可用，请检查清单、素材与依赖')
        return items, warnings

    def document(self, loaded, data):
        root, manifest = loaded['root'], loaded['manifest']
        resources = {}

        def asset(relative):
            path = self.file(root, relative)
            url = f'https://digest.invalid/resource/{len(resources)}'
            mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
            resources[url] = (path, mime)
            return url

        replacements = {name.upper(): asset('assets/' + name_path) for name, name_path in manifest.get('assets', {}).items()}
        styles = [self.file(root, 'styles/' + name).read_text(encoding='utf-8') for name in manifest['styles']]
        styles.append(f":root{{--page-width:{manifest['width']}px;}}")
        fonts = manifest.get('fonts', {})
        for face in fonts.get('faces', []):
            if self.file(root, face['file']).is_file():
                styles.append("@font-face{font-family:'%s';src:url('%s');font-weight:%s;font-style:normal;font-display:block;}" % (face['family'], asset(face['file']), face['weight']))
        for role, config in fonts.get('roles', {}).items():
            names = [config['family']] + config['fallback']
            family = ','.join(n if n in ('serif', 'sans-serif', 'monospace') else "'" + n + "'" for n in names)
            styles.append(f':root{{--{role}-font:{family};}}')
        theme = {'layout': manifest['layout'], 'stickers': {}}
        stickers = manifest.get('stickers', {})
        cycle = manifest.get('stickerCycle', [])
        for i, item in enumerate(data['sections']):
            if cycle:
                name = cycle[i % len(cycle)]
                item['expression'] = name
                if name not in theme['stickers']:
                    theme['stickers'][name] = asset('assets/' + stickers[name])
        components = []
        for name in manifest['components']:
            html = self.file(root, 'components/' + name + '.html').read_text(encoding='utf-8')
            html = re.sub(r'\{\{([A-Z]+)\}\}', lambda m: replacements[m[1]], html)
            components.append(f'<template id="component-{name}">{html}</template>')
        values = {'STYLES': '\n'.join(styles), 'COMPONENTS': '\n'.join(components),
                  'DATA': safe_json(data), 'THEME': safe_json(theme),
                  'RUNTIME': (self.root / 'common' / 'layout.js').read_text(encoding='utf-8')}
        html = self.file(root, manifest['entry']).read_text(encoding='utf-8')
        html = re.sub(r'\{\{([A-Z]+)\}\}', lambda m: values[m[1]], html)
        return html, resources


class ImageRenderer:
    def __init__(self, store, templates, executable=None):
        self.store = store
        self.catalog = TemplateCatalog(templates)
        self.directory = store.path.parent / 'rendered'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.executable = executable or os.environ.get('DAILY_DIGEST_BROWSER')
        self.lock = asyncio.Lock()
        self.browser = None
        self.playwright = None

    async def _browser(self):
        if self.browser and self.browser.is_connected():
            return self.browser
        if not self.playwright:
            from playwright.async_api import async_playwright
            self.playwright = await async_playwright().start()
        executable = self.executable
        if not executable:
            for name in ('chromium', 'chromium-browser', 'google-chrome'):
                executable = shutil.which(name)
                if executable:
                    break
        if not executable and os.name == 'nt':
            for candidate in ('C:/Program Files/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'):
                if Path(candidate).is_file():
                    executable = candidate
                    break
        self.browser = await self.playwright.chromium.launch(headless=True, executable_path=executable, args=['--disable-dev-shm-usage'])
        return self.browser

    async def close(self):
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    async def capture(self, loaded, data):
        html, resources = await asyncio.to_thread(self.catalog.document, loaded, data)
        browser = await self._browser()
        context = await browser.new_context(viewport={'width': loaded['manifest']['width'], 'height': 900}, device_scale_factor=1, service_workers='block')
        failed_resources = []
        try:
            async def route(request):
                found = resources.get(request.request.url)
                if not found:
                    await request.abort()
                    return
                file, mime = found
                try:
                    body = await asyncio.to_thread(file.read_bytes)
                    await request.fulfill(body=body, content_type=mime, headers={'Access-Control-Allow-Origin': '*'})
                except OSError:
                    failed_resources.append(file.name)
                    await request.abort()
            await context.route('**/*', route)
            page = await context.new_page()
            await page.set_content(html, wait_until='load', timeout=30000)
            await page.wait_for_function('window.__RENDER_READY__ || window.__RENDER_ERROR__', timeout=30000)
            metrics = await page.evaluate('''() => {
                const canvas = document.querySelector('.report-page');
                const rect = canvas.getBoundingClientRect();
                const visible = [...document.querySelectorAll('.card-body')].every(node => {
                  if(node.scrollHeight>node.clientHeight+1 || node.scrollWidth>node.clientWidth+1) return false;
                  const range=document.createRange(); range.selectNodeContents(node);
                  const lines=[...range.getClientRects()];
                  return lines.some(r=>r.width>0) && lines.every(r=>r.left>=rect.left-1 && r.right<=rect.right+1 && r.top>=rect.top && r.bottom<=rect.bottom+1);
                });
                return {error:window.__RENDER_ERROR__, width:rect.width, height:rect.height,
                  overflow:document.documentElement.scrollWidth>rect.width,
                  visible, fontError:[...document.fonts].some(f=>f.status==='error'),
                  text:[...document.querySelectorAll('.card-body')].map(n=>n.textContent),
                  images:[...document.images].every(i=>i.complete&&i.naturalWidth>0)};
            }''')
            if metrics['error'] or metrics['overflow'] or not metrics['images'] or not metrics['visible'] or metrics['fontError'] or failed_resources:
                raise RenderError('模板排版或资源加载失败')
            if metrics['text'] != [s['body'] for s in data['sections']]:
                raise RenderError('模板没有完整呈现所有正文')
            if not 1 <= metrics['height'] <= MAX_HEIGHT:
                raise RenderError(f'图片超过 {MAX_HEIGHT} 像素高度限制')
            png = await page.locator('.report-page').screenshot(type='png', timeout=30000)
            if len(png) > MAX_PNG_BYTES:
                raise RenderError('图片超过 8 MiB 大小限制')
            return png, {'width': int(metrics['width']), 'height': math.ceil(metrics['height'])}
        finally:
            await context.close()

    async def present(self, run, task, result):
        result['delivery'] = {'format': 'text', 'requested': task.output_mode}
        if task.output_mode != 'image' or not result.get('summary'):
            return result
        if not result.get('report'):
            result['delivery']['reason'] = '图片结构不可用，本次使用文字'
            return result
        try:
            async with asyncio.timeout(90):
                async with self.lock:
                    loaded = await asyncio.to_thread(self.catalog.load, task.template_id)
                    result.setdefault('warnings', []).extend(loaded['warnings'])
                    closing = await self.store.choose_closing(run, task.template_id, loaded['choices'])
                    data = copy.deepcopy(result['report'])
                    data.update(title='日常简报', date=parse_time(run['snapshot']['planned_at']).astimezone(ZoneInfo(task.timezone)).strftime('%Y.%m.%d'), closing=closing)
                    png, size = await self.capture(loaded, data)
                    name = run['id'] + '.png'
                    if not FILE_NAME.fullmatch(name):
                        raise RenderError('图片文件名无效')
                    path = self.directory / name
                    def save():
                        temp = path.with_suffix('.tmp')
                        temp.write_bytes(png)
                        temp.replace(path)
                    await asyncio.to_thread(save)
                    result['delivery'] = {'format': 'image', 'requested': 'image', 'file': name,
                        'sha256': hashlib.sha256(png).hexdigest(), **size,
                        'template_id': task.template_id, 'template_version': loaded['manifest']['version'], 'closing': closing}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = str(exc) if isinstance(exc, RenderError) else '本地图片渲染不可用，请检查浏览器和渲染依赖'
            result['delivery']['reason'] = reason
            result.setdefault('warnings', []).append(reason + '；已回退文字，内容未截断')
        return result

    async def read(self, name, digest):
        if not isinstance(name, str) or not FILE_NAME.fullmatch(name):
            raise RenderError('图片标识无效')
        path = (self.directory / name).resolve()
        if not path.is_relative_to(self.directory.resolve()):
            raise RenderError('图片路径无效')
        def read():
            if path.stat().st_size > MAX_PNG_BYTES:
                raise RenderError('图片文件过大')
            raw = path.read_bytes()
            if not raw.startswith(b'\x89PNG\r\n\x1a\n') or hashlib.sha256(raw).hexdigest() != digest:
                raise RenderError('图片校验失败')
            return raw
        try:
            return await asyncio.to_thread(read)
        except OSError as exc:
            raise RenderError('图片文件缺失，不能发送或预览') from exc

    async def preview(self, run_id):
        run = await self.store.get_run(run_id)
        delivery = (run.get('result') or {}).get('delivery', {})
        if delivery.get('format') != 'image':
            raise RenderError('此记录没有图片')
        raw = await self.read(delivery['file'], delivery['sha256'])
        return {'src': 'data:image/png;base64,' + base64.b64encode(raw).decode('ascii')}

    async def cleanup(self):
        def references(db):
            files = set()
            for row in db.execute('SELECT result FROM runs WHERE result IS NOT NULL'):
                name = (json.loads(row[0]).get('delivery') or {}).get('file')
                if name:
                    files.add(name)
            files.update(row[0] for row in db.execute("SELECT body FROM outbox WHERE kind='image' AND body IS NOT NULL"))
            return files
        keep = await self.store.call(references)
        def clean():
            for path in self.directory.iterdir():
                if path.is_file() and path.name not in keep and path.suffix in ('.png', '.tmp') and path.stat().st_mtime < time.time() - 3600:
                    path.unlink()
        await asyncio.to_thread(clean)
