"""Template discovery and isolated, bounded Typst rendering."""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from .domain import parse_time

MAX_HEIGHT = 12000
MAX_PNG_BYTES = 8 * 1024 * 1024
FILE_NAME = re.compile(r'^[a-f0-9]{32}\.png$')


class RenderError(ValueError):
    pass


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
            if list(Draft202012Validator(schema).iter_errors(manifest)):
                raise RenderError('模板清单不符合 Typst 规范')
            if manifest['id'] != template_id:
                raise RenderError('模板标识与目录不一致')
            self.file(root, manifest['entry']).read_text(encoding='utf-8')
            for name in manifest.get('assets', {}).values():
                if not self.file(root, 'assets/' + name).is_file():
                    raise RenderError('模板图片素材缺失')
            stickers = manifest.get('stickers', {})
            for name in stickers.values():
                if not self.file(root, 'assets/' + name).is_file():
                    raise RenderError('模板表情素材缺失')
            if stickers and (not manifest.get('stickerCycle') or any(n not in stickers for n in manifest['stickerCycle'])):
                raise RenderError('模板表情轮换配置无效')
            warnings = []
            for face in manifest.get('fonts', {}).get('faces', []):
                if face['distribution'] == 'private' and not face['file'].startswith('assets/fonts/private/'):
                    raise RenderError('私有字体必须放在模板的 private 目录')
                if not self.file(root, face['file']).is_file():
                    warnings.append(f"字体缺失：{face['file']}，将尝试回退字体")
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

    def payload(self, loaded, data):
        root, manifest = loaded['root'], loaded['manifest']
        data = copy.deepcopy(data)
        data['assets'] = {key: '/' + 'assets/' + name for key, name in manifest.get('assets', {}).items()}
        cycle = manifest.get('stickerCycle', [])
        for i, item in enumerate(data['sections']):
            item['sticker'] = '/assets/' + manifest['stickers'][cycle[i % len(cycle)]] if cycle else None
        faces = manifest.get('fonts', {}).get('faces', [])
        files = [str(self.file(root, face['file'])) for face in faces if self.file(root, face['file']).is_file()]
        return {'root': str(root), 'entry': manifest['entry'], 'data': data,
                'font_files': files, 'font_roles': manifest.get('fonts', {}).get('roles', {}),
                'width': manifest['width'], 'max_height': MAX_HEIGHT, 'max_bytes': MAX_PNG_BYTES}


class ImageRenderer:
    def __init__(self, store, templates):
        self.store = store
        self.catalog = TemplateCatalog(templates)
        self.directory = store.path.parent / 'rendered'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()
        self.workers = set()
        self.last_metrics = None
        self.worker_timeout = 75

    async def close(self):
        for process in tuple(self.workers):
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        await asyncio.gather(*(p.wait() for p in tuple(self.workers)), return_exceptions=True)

    async def capture(self, loaded, data):
        payload = self.catalog.payload(loaded, data)
        # A fresh process returns all native compiler memory on exit.
        with tempfile.TemporaryDirectory(prefix='.typst-', dir=self.directory) as job:
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).with_name('typst_worker.py')), job,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                **({'creationflags': 0x08000000} if os.name == 'nt' else {}))
            self.workers.add(process)
            communication = asyncio.create_task(process.communicate(json.dumps(payload, ensure_ascii=False).encode('utf-8')))
            try:
                try:
                    async with asyncio.timeout(self.worker_timeout):
                        stdout, _ = await asyncio.shield(communication)
                except BaseException as exc:
                    if process.returncode is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                    await asyncio.shield(asyncio.gather(communication, return_exceptions=True))
                    if isinstance(exc, TimeoutError):
                        raise RenderError('Typst 编译超时，已停止子进程') from exc
                    raise
                result = json.loads(stdout)
                if process.returncode or not result.get('ok'):
                    raise RenderError(result.get('error', 'Typst 编译失败'))
                png = await asyncio.to_thread((Path(job) / 'report.png').read_bytes)
                self.last_metrics = result
                return png, {'width': result['width'], 'height': result['height']}
            finally:
                self.workers.discard(process)

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
            reason = str(exc) if isinstance(exc, RenderError) else '本地 Typst 渲染不可用，请检查模板及依赖'
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
