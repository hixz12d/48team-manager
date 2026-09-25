"""Build the personal extension using the existing VPS Cloudflare configuration.

Reads only three Team48 settings. Never prints credentials or modifies the VPS.
"""
import argparse
import base64
import json
import re
import shutil
from pathlib import Path
import shlex
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
REMOTE = '''
import json, sqlite3
from pathlib import Path
from sqlalchemy.engine.url import make_url
from app.core.config import load_settings
url = make_url(load_settings().database_url)
if url.get_backend_name() != "sqlite":
    raise RuntimeError("Expected the Team48 SQLite database")
path = Path(url.database).resolve()
with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
    values = dict(db.execute("SELECT key, value FROM system_settings WHERE key IN (?, ?, ?)",
                  ("cf_mail_base_url", "cf_mail_address", "cf_mail_admin_password")))
config = {
    "baseUrl": values.get("cf_mail_base_url") or "https://apimail.xiaozhudf2026.foo",
    "address": values.get("cf_mail_address") or "icloud@xiaozhudf2026.foo",
    "adminPassword": values.get("cf_mail_admin_password") or "",
}
print("TEAM48_CF_CONFIG=" + json.dumps(config))
'''


def remote_config():
    from team48_target_probe import connect

    payload = base64.b64encode(REMOTE.encode()).decode()
    command = "docker exec -w /app team48-manager python -c " + shlex.quote(
        "import base64; exec(base64.b64decode(" + repr(payload) + "))")
    with connect() as client:
        _, stdout, stderr = client.exec_command(command, timeout=30)
        output = stdout.read().decode("utf-8")
        stderr.read()  # remote diagnostic bodies may contain configuration; do not echo them
        status = stdout.channel.recv_exit_status()
    lines = [line.removeprefix("TEAM48_CF_CONFIG=") for line in output.splitlines() if line.startswith("TEAM48_CF_CONFIG=")]
    if status or len(lines) != 1:
        raise RuntimeError("Could not read the existing Cloudflare configuration")
    return json.loads(lines[0])


TEAM48_URL = "https://48team.xiaozhudf2026.foo"


def embedded(source, name):
    match = re.search(r'export const ' + name + r' = Object\.freeze\((\{.*?\})\);', source, re.S)
    return json.loads(match.group(1)) if match else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--local-config', action='store_true', help='Reuse the existing local private-config.mjs; no SSH or server access')
    parser.add_argument('--unpack', action='store_true', help='Also update dist/chatgpt-signup for loading unpacked')
    parser.add_argument('--team48-token', help='EXTENSION_API_TOKEN of the Team48 server; enables the automatic handoff')
    parser.add_argument('--no-team48', action='store_true', help='Remove the embedded Team48 handoff configuration')
    args = parser.parse_args()
    extension = ROOT / 'extensions' / 'chatgpt-signup'
    private = extension / 'private-config.mjs'
    source = private.read_text(encoding='utf-8') if private.exists() else ''
    if args.local_config:
        config = embedded(source, 'MAILBOX')
        if not config:
            raise RuntimeError('Local mailbox configuration has an unsupported format')
    else:
        config = remote_config()
    if not all(isinstance(config.get(key), str) and config[key].strip() for key in ("baseUrl", "address", "adminPassword")):
        raise RuntimeError("The existing Cloudflare configuration is incomplete")
    if config["baseUrl"].rstrip("/") != "https://apimail.xiaozhudf2026.foo":
        raise RuntimeError("Mailbox origin differs from the extension's fixed host permission")
    # The Team48 token is kept across rebuilds unless replaced or explicitly removed.
    team48 = None if args.no_team48 else embedded(source, 'TEAM48')
    if args.team48_token:
        if len(args.team48_token.strip()) < 24:
            raise RuntimeError("The Team48 token must have at least 24 characters")
        team48 = {"baseUrl": TEAM48_URL, "token": args.team48_token.strip()}
    if team48 and team48.get("baseUrl", "").rstrip("/") != TEAM48_URL:
        raise RuntimeError("Team48 origin differs from the extension's fixed host permission")
    text = ("// Personal package configuration. Generated locally; excluded from Git.\n"
            "export const MAILBOX = Object.freeze(" + json.dumps(config, ensure_ascii=True) + ");\n")
    if team48:
        text += "export const TEAM48 = Object.freeze(" + json.dumps(team48, ensure_ascii=True) + ");\n"
    private.write_text(text, encoding="utf-8")
    version = json.loads((extension / "manifest.json").read_text(encoding="utf-8"))["version"]
    destination = ROOT / "dist" / f"team48-chatgpt-signup-{version}-personal.zip"
    destination.parent.mkdir(exist_ok=True)
    with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
        for path in sorted(extension.rglob("*")):
            if path.is_file():
                archive.write(path, Path("chatgpt-signup") / path.relative_to(extension))
    with ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("Package integrity check failed")
    if args.unpack:
        shutil.copytree(extension, ROOT / 'dist' / 'chatgpt-signup', dirs_exist_ok=True)
    handoff = "Team48 handoff enabled" if team48 else "Team48 handoff not configured"
    print(f"Built {destination.name}; existing mailbox settings embedded; {handoff}; no server changes.")


if __name__ == "__main__":
    main()
