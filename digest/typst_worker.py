"""One-shot native renderer. No AstrBot imports, no model or delivery calls."""
import io
import json
import math
from pathlib import Path
import struct
import sys
import time
from xml.etree import ElementTree


def render(request, destination):
    import typst
    started = time.perf_counter()
    root = Path(request['root']).resolve()
    entry = (root / request['entry']).resolve()
    if not entry.is_relative_to(root):
        raise ValueError('模板入口超出目录')
    files = request['font_files']
    if any(not Path(p).resolve().is_relative_to(root) for p in files):
        raise ValueError('字体超出模板目录')
    # typst-py scans directories on Linux; individual file paths are not portable.
    directories = sorted({str(Path(p).parent) for p in files})
    fonts = typst.Fonts(font_paths=directories, include_system_fonts=False)
    families = set(fonts.families())
    roles = request['font_roles']
    # Only scan system fonts when a declared role cannot be served by bundled fonts.
    system_fonts_used = any(role['family'] not in families for role in roles.values())
    if system_fonts_used:
        fonts = typst.Fonts(font_paths=directories, include_system_fonts=True)
        families = set(fonts.families())
    resolved = {}
    for name, role in roles.items():
        choices = [role['family']] + role['fallback']
        match = next((candidate for candidate in choices if candidate in families), None)
        if match is None:
            raise ValueError(f'缺少 {name} 可用字体，请补齐模板字体')
        resolved[name] = match
    data = request['data']
    data['fonts'] = resolved
    compiler = typst.Compiler(str(entry), root=str(root), font_paths=fonts,
        ignore_system_fonts=True, sys_inputs={'data': json.dumps(data, ensure_ascii=False)})
    svg = compiler.compile(format='svg')
    if not isinstance(svg, bytes):
        raise ValueError('模板必须输出单张连续长图')
    with io.BytesIO(svg) as stream:
        _, node = next(ElementTree.iterparse(stream, events=('start',)))
        bounds = [float(n) for n in node.attrib['viewBox'].split()]
    width, height = bounds[2] * 2, bounds[3] * 2
    if not all(math.isfinite(n) and n > 0 for n in (width, height)):
        raise ValueError('模板画布尺寸无效')
    if abs(width - request['width']) > 1:
        raise ValueError('模板画布宽度与清单不一致')
    if height > request['max_height']:
        raise ValueError(f"图片超过 {request['max_height']} 像素高度限制")
    png = compiler.compile(format='png', ppi=144)
    if not isinstance(png, bytes) or len(png) > request['max_bytes']:
        raise ValueError('图片超过文件大小限制')
    actual_width, actual_height = struct.unpack('>II', png[16:24])
    if abs(actual_width - width) > 1 or abs(actual_height - height) > 1:
        raise ValueError('PNG 尺寸与排版不一致')
    (destination / 'report.png').write_bytes(png)
    peak = None
    status = Path('/proc/self/status')
    if status.exists():
        for line in status.read_text().splitlines():
            if line.startswith('VmHWM:'):
                peak = round(int(line.split()[1]) / 1024, 2)
    return {'ok': True, 'width': actual_width, 'height': actual_height, 'bytes': len(png),
            'seconds': round(time.perf_counter() - started, 3), 'worker_peak_rss_mib': peak,
            'fonts': resolved, 'system_fonts_used': system_fonts_used}


if __name__ == '__main__':
    try:
        value = render(json.load(sys.stdin), Path(sys.argv[1]))
    except Exception as error:
        value = {'ok': False, 'error': str(error)[:1200]}
    sys.stdout.buffer.write(json.dumps(value, ensure_ascii=False).encode('utf-8'))
    raise SystemExit(0 if value['ok'] else 1)
