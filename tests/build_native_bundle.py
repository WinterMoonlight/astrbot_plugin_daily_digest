"""Build a stdin script containing only source/test files, no credentials."""
import base64
import io
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
buffer = io.BytesIO()
with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as bundle:
    for path in (root / 'digest').glob('*.py'):
        bundle.write(path, path.relative_to(root).as_posix())
    bundle.write(root / 'tests/native_probe.py', 'tests/native_probe.py')
    bundle.write(root / 'main.py', 'main.py')
payload = base64.b64encode(buffer.getvalue()).decode('ascii')
script = f'''import base64,io,os,runpy,sys,tempfile,zipfile
with tempfile.TemporaryDirectory(prefix="digest_code_") as root:
    with zipfile.ZipFile(io.BytesIO(base64.b64decode({payload!r}))) as source:
        source.extractall(root)
    sys.path.insert(0,root)
    runpy.run_path(os.path.join(root,"tests/native_probe.py"),run_name="__main__")
'''
target = root / '.local/run_native_probe.py'
target.parent.mkdir(exist_ok=True)
target.write_text(script, encoding='ascii')
print('Prepared isolated native probe bundle (source/test only).')
