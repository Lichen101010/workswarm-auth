"""Install a durable Windows copy; no credentials are printed or packaged."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


FILES = ('pyproject.toml', 'src', 'README.md', 'CLOUD.md', 'publisher')


def main():
    if os.name != 'nt':
        raise SystemExit('Run this installer on Windows after Extract All.')
    os.environ['PYTHONUTF8'] = '1'
    source = Path(__file__).resolve().parent
    if not (source / 'pyproject.toml').is_file() or not (source / 'src/workswarm_auth/cli.py').is_file():
        raise SystemExit('Extract All files from the ZIP, then rerun Install-WorkSwarm-Auth.cmd.')
    profile = source / 'publisher/cloud.json'
    if not profile.is_file():
        raise SystemExit('Publisher cloud profile is missing. Use the publisher-built client ZIP.')
    version = re.search(r'__version__ = "([^"]+)"', (source / 'src/workswarm_auth/__init__.py').read_text(encoding='utf-8')).group(1)
    target = Path(os.environ['LOCALAPPDATA']) / 'WorkSwarmAuth' / version
    target.mkdir(parents=True, exist_ok=True)
    if source != target:
        for name in FILES:
            path = source / name
            if path.is_dir():
                shutil.copytree(path, target / name, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.egg-info'))
            elif path.is_file():
                shutil.copy2(path, target / name)
    python = target / '.venv/Scripts/python.exe'
    if not python.exists():
        subprocess.run([sys.executable, '-m', 'venv', str(target / '.venv')], check=True)
    subprocess.run([str(python), '-m', 'pip', 'install', '-e', str(target)], check=True)
    cli = [str(python), '-m', 'workswarm_auth.cli']
    subprocess.run([*cli, 'cloud-init', '--profile', str(target / 'publisher/cloud.json')], check=True)
    config = subprocess.run([*cli, 'cloud-config'], check=True, capture_output=True, text=True, encoding='utf-8').stdout
    (target / 'workswarm-cloud-mcp.json').write_text(config, encoding='utf-8')
    report = {'installed': True, 'version': version, 'mode': 'cloud', 'installation': str(target),
              'mcp_config': str(target / 'workswarm-cloud-mcp.json'),
              'provider_credentials_required': False, 'real_oauth_and_workswarm_gui': 'pending'}
    (target / 'install-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('Installed cloud bridge at: ' + str(target))
    print('Import workswarm-cloud-mcp.json in WorkSwarm, then call auth_connect. No provider credentials are needed.')


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError:
        raise SystemExit('Installation step failed. See the preceding command output; no success is recorded.')
