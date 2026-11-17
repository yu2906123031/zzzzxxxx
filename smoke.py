import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

with tempfile.TemporaryDirectory() as tmp:
    proc=subprocess.Popen([sys.executable,'arb.py','--port','18787','--database',str(Path(tmp)/'smoke.sqlite')],stdout=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                with urllib.request.urlopen('http://127.0.0.1:18787/',timeout=2) as r:
                    assert r.status==200 and 'PERP RADAR' in r.read().decode('utf-8-sig')
                break
            except OSError:
                time.sleep(.2)
        else:
            raise AssertionError('server did not start')
        with urllib.request.urlopen('http://127.0.0.1:18787/api/state',timeout=2) as r:
            data=json.load(r)
            assert data['mode']=='PAPER ONLY'
        print('HTTP page and JSON endpoint OK')
        html=Path('web/index.html').read_text(encoding='utf-8-sig')
        js=html.split('<script>')[1].split('</script>')[0]
        js_path=Path(tmp)/'ui.js'
        js_path.write_text(js,encoding='utf-8')
        subprocess.run(['node','--check',str(js_path)],check=True)
        print('Browser JavaScript syntax OK')
    finally:
        proc.terminate()
        proc.wait(timeout=10)

