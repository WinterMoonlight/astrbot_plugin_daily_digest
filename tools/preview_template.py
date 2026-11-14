"""Render local sample data with the production renderer; no model or QQ calls."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from digest.domain import TaskConfig, utcnow
from digest.presentation import report_text
from digest.rendering import ImageRenderer
from digest.storage import Store


async def main(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    store = Store(output / 'preview.sqlite3')
    await store.initialize()
    renderer = ImageRenderer(store, ROOT / 'templates')
    try:
        sample = json.loads(Path(args.sample).read_text(encoding='utf-8'))
        report = {'intro': sample['intro'], 'sections': sample['sections']}
        config = TaskConfig(name='Local preview', groups=['local:GroupMessage:1'], recipient='local:FriendMessage:1',
                            provider_id='unused', output_mode='image', template_id=args.template)
        task = await store.save_task(config)
        run = await store.get_run(await store.enqueue(task['id'], utcnow(), 'preview'))
        result = await renderer.present(run, config, {'summary': report_text(report), 'report': report, 'warnings': []})
        await store.finish(run['id'], 'ready', result)
        (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        if result['delivery']['format'] != 'image':
            raise RuntimeError(result['delivery'].get('reason', 'No image generated'))
        png = await renderer.read(result['delivery']['file'], result['delivery']['sha256'])
        (output / 'preview.png').write_bytes(png)
        print(output / 'preview.png')
        for warning in result['warnings']:
            print(warning)
    finally:
        await renderer.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template', default='shorekeeper')
    parser.add_argument('--sample', default=str(ROOT / 'templates/shorekeeper/samples/standard.json'))
    parser.add_argument('--output', default=str(ROOT / '.local/template-preview'))
    asyncio.run(main(parser.parse_args()))
