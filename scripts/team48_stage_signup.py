"""Stage the approved isolated signup experiment; never replace live application files."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import shlex

from scripts.team48_target_probe import connect

NAME = "oauth-signup-hixz2611-20260910"
HOST_ROOT = "/opt/team48/data/experiments/" + NAME
CONTAINER_ROOT = "/app/data/experiments/" + NAME
BROWSER_SHA256 = "85593ab5bedbdceee2196f2d1e9c603a09cf18f5dfb9bcc949c4232e7b35295f"

SETUP = r'''
import hashlib, json, os, shutil, urllib.request, zipfile
from pathlib import Path
root = Path('/app/data/experiments/oauth-signup-hixz2611-20260910')
expected = {
'app/application/onboard.py': 'b2c5d69ff7c6487c30e6d9e5295962597a29a46d39ac01d511b620727957ede5',
'app/application/console_actions.py': '9e77c8e26862e87dc66ac59ddcf65042326d8b69bde07cad7d02e35b8db3d6e9',
'app/integrations/openai/browser/reauth.py': '37d9b8aa002968306740af6a2bc924935297fb57f0e65e9d278684169478a55e'}
for relative, digest in expected.items():
    if hashlib.sha256(Path('/app', relative).read_text().encode()).hexdigest() != digest:
        raise RuntimeError('Live code changed; staging refused')
root.mkdir(parents=True, exist_ok=False, mode=0o700)
shutil.copytree('/app/app', root / 'app', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
(root / 'scripts').mkdir(mode=0o700)
url = 'https://github.com/xiaozhou26/Chromix/releases/download/v152.0.7977.82/chromix-linux-x64.zip'
archive = root / 'chromix-linux-x64.zip'
request = urllib.request.Request(url, headers={'User-Agent': 'Team48-single-account-test'})
with urllib.request.urlopen(request, timeout=60) as response, archive.open('xb') as target:
    shutil.copyfileobj(response, target)
with archive.open('rb') as stream:
    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
if digest != '85593ab5bedbdceee2196f2d1e9c603a09cf18f5dfb9bcc949c4232e7b35295f':
    raise RuntimeError('Chromix SHA256 mismatch; will not extract or execute')
destination = root / 'browser'
destination.mkdir(mode=0o700)
with zipfile.ZipFile(archive) as bundle:
    for entry in bundle.infolist():
        path = Path(entry.filename)
        if path.is_absolute() or '..' in path.parts or '\\' in entry.filename:
            raise RuntimeError('Unsafe ZIP path')
        mode = entry.external_attr >> 16
        if mode & 0o170000 == 0o120000:
            raise RuntimeError('ZIP symlink refused')
    bundle.extractall(destination)
    for entry in bundle.infolist():
        path = destination / entry.filename
        if path.is_file():
            path.chmod(0o700 if (entry.external_attr >> 16) & 0o111 else 0o600)
executables = [str(p) for p in destination.rglob('chrome') if p.is_file()]
print(json.dumps({'staged': str(root), 'browser_sha256': digest, 'executables': executables}))
'''


def run_remote(client, source, *, timeout=600):
    encoded = base64.b64encode(source.encode()).decode()
    command = "docker exec -w /app team48-manager python -c " + shlex.quote(
        "import base64; exec(base64.b64decode(" + repr(encoded) + "))")
    _, out, err = client.exec_command(command, timeout=timeout)
    text = out.read().decode("utf-8", errors="replace")
    error = err.read().decode("utf-8", errors="replace")
    code = out.channel.recv_exit_status()
    print(text)
    if code:
        # These scripts have no credential-bearing requests or command arguments.
        print("remote_failure:", error[-3000:])
        raise RuntimeError("Isolated staging/preflight failed")


def main():
    project = Path(__file__).resolve().parents[1]
    files = (
        "app/application/onboard.py", "app/application/console_actions.py",
        "app/application/oauth_signup.py", "app/integrations/openai/browser/reauth.py",
        "scripts/signup_one.py",
    )
    with connect() as client:
        run_remote(client, SETUP)
        with client.open_sftp() as sftp:
            for relative in files:
                sftp.put(str(project / relative), HOST_ROOT + "/" + relative)
        print(json.dumps({'isolated_code_uploaded': list(files), 'live_files_changed': False}))


if __name__ == "__main__":
    main()
