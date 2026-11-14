"""Start the loopback workbench with the current Python interpreter."""
import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
URL = 'http://127.0.0.1:' + os.environ.get('VIDEO_STUDIO_PORT', '8788')


def ready():
    try:
        with urllib.request.urlopen(URL + '/api/agent', timeout=2) as response:
            body = json.load(response)
            return 'runs' in body and 'config' in body
    except (OSError, ValueError):
        return False


def main():
    if not ready():
        with (ROOT / 'launcher.log').open('ab') as log:
            process = subprocess.Popen(
                [sys.executable, str(ROOT / 'server.py')], cwd=ROOT,
                stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW,
            )
        for _ in range(80):
            if ready():
                break
            if process.poll() is not None:
                raise RuntimeError('服务启动失败，请检查 launcher.log 和端口占用。')
            time.sleep(0.5)
        else:
            raise RuntimeError('服务启动超时，请检查 launcher.log。')
    webbrowser.open(URL)


if __name__ == '__main__':
    main()
