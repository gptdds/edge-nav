#!/usr/bin/env python
"""Run the fictional-data UI checks and refresh the public screenshots."""
import os
from pathlib import Path
import shutil
import subprocess

root = Path(__file__).resolve().parent.parent
env = os.environ.copy()
if os.name == 'nt' and not env.get('BROWSER_PATH'):
    for candidate in (
        r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
        r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    ):
        if Path(candidate).exists():
            env['BROWSER_PATH'] = candidate
            break
npm = shutil.which('npm')
if not npm:
    raise SystemExit('Install Node.js, then run npm ci and npx playwright install chromium.')
raise SystemExit(subprocess.call([npm, 'test'], cwd=root, env=env))
