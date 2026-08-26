"""子号本机 OAuth 会话。ticket 一次性，给 Windows 回调窗口用。"""
from __future__ import annotations

import base64
import json
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlparse

from app.utils.proxy import mask_proxy_url, normalize_proxy_url
from app.utils.time_utils import get_now

REDIRECT_URI = "http://localhost:1455/auth/callback"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TTL = timedelta(minutes=20)

_LOCK = threading.Lock()
_SESSIONS: Dict[str, Dict[str, Any]] = {}


def _purge(now: Optional[datetime] = None) -> None:
    current = now or get_now()
    expired = [
        key
        for key, item in _SESSIONS.items()
        if item.get("expires_at") and item["expires_at"] < current
    ]
    for key in expired:
        _SESSIONS.pop(key, None)


def chrome_proxy_parts(proxy: str) -> Dict[str, str]:
    try:
        normalized = normalize_proxy_url(proxy) or ""
    except ValueError:
        normalized = ""
    if not normalized:
        return {}
    parsed = urlparse(normalized)
    host = parsed.hostname or ""
    port = parsed.port
    if not host or not port:
        return {}
    scheme = "socks5" if str(parsed.scheme or "").startswith("socks5") else "http"
    return {
        "server": f"{scheme}://{host}:{port}",
        "username": unquote(parsed.username) if parsed.username is not None else "",
        "password": unquote(parsed.password) if parsed.password is not None else "",
        "label": mask_proxy_url(normalized),
    }


def create_session(
    *,
    team_id: int,
    email: str,
    authorize: Dict[str, str],
    role: str = "child",
    mode: str = "manual",
    proxy: str = "",
    password: str = "",
    team_name: str = "",
) -> Dict[str, Any]:
    ticket = secrets.token_urlsafe(24)
    now = get_now()
    session = {
        "ticket": ticket,
        "team_id": int(team_id),
        "email": (email or "").strip().lower(),
        "role": "owner" if role == "owner" else "child",
        "mode": "auto" if mode == "auto" else "manual",
        "job_id": "",
        "proxy": (proxy or "").strip(),
        "login_password": password or "",
        "team_name": (team_name or "").strip(),
        "authorize_url": authorize.get("authorize_url") or "",
        "code_verifier": authorize.get("code_verifier") or "",
        "state": authorize.get("state") or "",
        "client_id": authorize.get("client_id") or CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "status": "waiting",
        "message": "等待本机回调",
        "error": "",
        "result": None,
        "created_at": now,
        "expires_at": now + TTL,
        "updated_at": now,
    }
    with _LOCK:
        _purge(now)
        _SESSIONS[ticket] = session
        return public_session(session)


def get_session(ticket: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        _purge()
        session = _SESSIONS.get(ticket or "")
        return dict(session) if session else None


def public_session(session: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ticket": session.get("ticket"),
        "team_id": session.get("team_id"),
        "email": session.get("email"),
        "role": session.get("role") or "child",
        "mode": session.get("mode") or "manual",
        "job_id": session.get("job_id") or "",
        "proxy_label": chrome_proxy_parts(str(session.get("proxy") or "")).get("label") or "",
        "authorize_url": session.get("authorize_url"),
        "redirect_uri": session.get("redirect_uri"),
        "client_id": session.get("client_id"),
        "status": session.get("status"),
        "message": session.get("message"),
        "error": session.get("error"),
        "result": session.get("result"),
    }


def mark_session(ticket: str, **fields: Any) -> Optional[Dict[str, Any]]:
    with _LOCK:
        session = _SESSIONS.get(ticket or "")
        if not session:
            return None
        session.update(fields)
        session["updated_at"] = get_now()
        return dict(session)


def consume_verifier(ticket: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        session = _SESSIONS.get(ticket or "")
        if not session:
            return None
        return dict(session)


PROTOCOL_NAME = "team48-oauth"


def launch_payload(session: Dict[str, Any], complete_url: str) -> Dict[str, Any]:
    parts = chrome_proxy_parts(str(session.get("proxy") or ""))
    return {
        "authorizeUrl": session.get("authorize_url") or "",
        "ticket": session.get("ticket") or "",
        "completeUrl": complete_url or "",
        "proxyServer": parts.get("server") or "",
        "proxyUser": parts.get("username") or "",
        "proxyPass": parts.get("password") or "",
        "proxyLabel": parts.get("label") or "",
        "loginEmail": session.get("email") or "",
        "loginPassword": session.get("login_password") or "",
        "teamName": session.get("team_name") or "",
    }


def protocol_url(ticket: str, origin: str) -> str:
    from urllib.parse import quote

    base = (origin or "").strip().rstrip("/")
    return f"{PROTOCOL_NAME}://launch?ticket={quote(ticket or '', safe='')}&origin={quote(base, safe='')}"


_SOCKS_BRIDGE_CS = Path(__file__).with_name("team48_socks_bridge.cs")


def socks_bridge_source() -> str:
    return _SOCKS_BRIDGE_CS.read_text(encoding="utf-8")


_OAUTH_FILL_JS = Path(__file__).with_name("team48_oauth_fill.js")
_OAUTH_BG_JS = Path(__file__).with_name("team48_oauth_bg.js")


def oauth_fill_source() -> str:
    return _OAUTH_FILL_JS.read_text(encoding="utf-8")


def oauth_bg_source() -> str:
    return _OAUTH_BG_JS.read_text(encoding="utf-8")


def _oauth_fill_src_ps1() -> str:
    fill = base64.b64encode(oauth_fill_source().encode("utf-8")).decode("ascii")
    bg = base64.b64encode(oauth_bg_source().encode("utf-8")).decode("ascii")
    return (
        f"$fillSrc = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{fill}'))\n"
        f"$bgSrc = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{bg}'))\n"
    )


def _socks_bridge_setup_ps1() -> str:
    payload = base64.b64encode(socks_bridge_source().encode("utf-8")).decode("ascii")
    return (
        "$script:team48Bridge = $null\n"
        "if ($cfg.proxyUser -and ([string]$cfg.proxyServer).ToLower().StartsWith('socks5')) {\n"
        "    try {\n"
        f"        $src = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'))\n"
        "        Add-Type -TypeDefinition $src -Language CSharp\n"
        "        $u = [Uri]$cfg.proxyServer\n"
        "        $script:team48Bridge = New-Object Team48SocksBridge($u.Host, [int]$u.Port, [string]$cfg.proxyUser, [string]$cfg.proxyPass)\n"
        "        $cfg.proxyServer = 'socks5://127.0.0.1:' + $script:team48Bridge.Port\n"
        "        $cfg.proxyUser = ''\n"
        "        $cfg.proxyPass = ''\n"
        "    } catch {\n"
        "        throw\n"
        "    }\n"
        "}\n"
    )


def _chrome_oauth_ps1() -> str:
    return (
        r"""if (-not ('Team48Native.Win' -as [type])) {
    Add-Type -Namespace Team48Native -Name Win -MemberDefinition '[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow); [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();'
}
[Team48Native.Win]::ShowWindow([Team48Native.Win]::GetConsoleWindow(), 0)
$script:listener = $null
$script:workDir = $null
function Release-Team48Listen {
    if ($script:listener) {
        try { if ($script:listener.IsListening) { $script:listener.Stop() } } catch {}
        try { $script:listener.Close() } catch {}
        $script:listener = $null
    }
}
function Stop-Team48Oauth {
    Release-Team48Listen
    if ($script:team48Bridge) {
        try { $script:team48Bridge.Dispose() } catch {}
        $script:team48Bridge = $null
    }
    if ($script:workDir) {
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.CommandLine -and $_.CommandLine.Contains($script:workDir) -and $_.Name -match 'chrome|msedge'
        } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Milliseconds 200
        Remove-Item -LiteralPath $script:workDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
function Clear-Team48Port {
    Release-Team48Listen
    try {
        Get-NetTCPConnection -LocalPort 1455 -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
            if ($_.OwningProcess -and $_.OwningProcess -ne $PID) {
                $proc = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
                if ($proc -and ($proc.ProcessName -match '^(powershell|pwsh)$')) {
                    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
                }
            }
        }
        Start-Sleep -Milliseconds 400
    } catch {}
}
if (-not $cfg.proxyServer) {
    [System.Windows.Forms.MessageBox]::Show('这个号没有静态 ISP 代理，不能弹出授权页。', 'Team48 重新授权')
    exit 1
}
"""
        + r"""try {
"""
        + _socks_bridge_setup_ps1()
        + _oauth_fill_src_ps1()
        + r"""$script:listener = [System.Net.HttpListener]::new()
foreach ($prefix in @('http://127.0.0.1:1455/','http://localhost:1455/','http://[::1]:1455/')) {
    try { $script:listener.Prefixes.Add($prefix) } catch {}
}
try {
    $script:listener.Start()
} catch {
    Clear-Team48Port
    $script:listener = [System.Net.HttpListener]::new()
    foreach ($prefix in @('http://127.0.0.1:1455/','http://localhost:1455/','http://[::1]:1455/')) {
        try { $script:listener.Prefixes.Add($prefix) } catch {}
    }
    $script:listener.Start()
}
$script:workDir = Join-Path $env:TEMP ('team48-oauth-' + $cfg.ticket.Substring(0, [Math]::Min(8, $cfg.ticket.Length)))
if (Test-Path -LiteralPath $script:workDir) { Remove-Item -LiteralPath $script:workDir -Recurse -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force -Path $script:workDir | Out-Null
$chrome = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LocalAppData\Google\Chrome\Application\chrome.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) { throw '找不到 Chrome / Edge，无法弹出授权页。' }
$ext = Join-Path $script:workDir 'ext'
New-Item -ItemType Directory -Force -Path $ext | Out-Null
$manifest = '{ "manifest_version": 2, "name": "Team48 OAuth", "version": "1.0", "permissions": ["webRequest", "webRequestBlocking", "<all_urls>"], "background": { "scripts": ["background.js"], "persistent": true }, "content_scripts": [{ "matches": ["https://auth.openai.com/*", "https://auth0.openai.com/*", "https://chatgpt.com/*"], "js": ["fill.js"], "run_at": "document_idle", "all_frames": true }] }'
if ($cfg.proxyUser) {
    $auth = 'chrome.webRequest.onAuthRequired.addListener(function(){return {authCredentials:{username:' + (ConvertTo-Json $cfg.proxyUser -Compress) + ',password:' + (ConvertTo-Json $cfg.proxyPass -Compress) + '}};},{urls:["<all_urls>"]},["blocking"]);'
    $bgSrc = $bgSrc + "`n" + $auth
}
Set-Content -LiteralPath (Join-Path $ext 'background.js') -Value $bgSrc -Encoding utf8
Set-Content -LiteralPath (Join-Path $ext 'manifest.json') -Value $manifest -Encoding utf8
$head = 'window.TEAM48_EMAIL = ' + (ConvertTo-Json ([string]$cfg.loginEmail) -Compress) + ";`nwindow.TEAM48_PASSWORD = " + (ConvertTo-Json ([string]$cfg.loginPassword) -Compress) + ";`nwindow.TEAM48_TEAM = " + (ConvertTo-Json ([string]$cfg.teamName) -Compress) + ";`n"
Set-Content -LiteralPath (Join-Path $ext 'fill.js') -Value ($head + $fillSrc) -Encoding utf8
$args = @(
    ('--user-data-dir=' + (Join-Path $script:workDir 'chrome-profile')),
    '--no-first-run',
    '--no-default-browser-check',
    '--new-window',
    '--disable-background-networking',
    '--disable-sync',
    '--disable-component-update',
    '--disable-client-side-phishing-detection',
    '--disable-features=Translate,MediaRouter,OptimizationHints',
    ('--proxy-server=' + $cfg.proxyServer),
    '--proxy-bypass-list=<-loopback>;localhost;127.0.0.1;::1;[::1]',
    ('--disable-extensions-except=' + $ext),
    ('--load-extension=' + $ext),
    $cfg.authorizeUrl
)
Start-Process -FilePath $chrome -ArgumentList $args | Out-Null
$task = $script:listener.GetContextAsync()
$deadline = [DateTime]::UtcNow.AddMinutes(18)
while (-not $task.AsyncWaitHandle.WaitOne(200)) {
    [System.Windows.Forms.Application]::DoEvents()
    if ([DateTime]::UtcNow -gt $deadline) { throw '授权超时，已释放 1455 端口。' }
}
$context = $task.Result
$callback = $context.Request.Url.AbsoluteUri
$html = '<html><body style="font-family:sans-serif;padding:24px">认证回调已收到，可以关闭这个窗口。</body></html>'
$buffer = [System.Text.Encoding]::UTF8.GetBytes($html)
$context.Response.StatusCode = 200
$context.Response.ContentType = 'text/html; charset=utf-8'
$context.Response.ContentLength64 = $buffer.Length
$context.Response.OutputStream.Write($buffer, 0, $buffer.Length)
$context.Response.Close()
Release-Team48Listen
$body = @{ ticket = $cfg.ticket; callback_text = $callback } | ConvertTo-Json
$resp = Invoke-RestMethod -Method Post -Uri $cfg.completeUrl -ContentType 'application/json; charset=utf-8' -Body $body
$msg = if ($resp.message) { [string]$resp.message } else { '重新授权完成' }
[System.Windows.Forms.MessageBox]::Show($msg, 'Team48 重新授权')
} catch {
    [System.Windows.Forms.MessageBox]::Show([string]$_.Exception.Message, 'Team48 重新授权')
    exit 1
} finally {
    Stop-Team48Oauth
}
"""
    )


def launcher_script(session: Dict[str, Any], complete_url: str) -> str:
    cfg_json = json.dumps(launch_payload(session, complete_url), ensure_ascii=True, separators=(",", ":"))
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "Add-Type -AssemblyName System.Windows.Forms\n"
        "$cfg = @'\n"
        f"{cfg_json}\n"
        "'@ | ConvertFrom-Json\n"
        + _chrome_oauth_ps1()
    )


def protocol_handler_script() -> str:
    return (
        r"""param(
    [Parameter(Position = 0)]
    [string]$Uri = ''
)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.Windows.Forms
if (-not $Uri) { $Uri = [string]$args[0] }
$raw = [string]$Uri
if ($raw.StartsWith('"') -and $raw.EndsWith('"')) { $raw = $raw.Substring(1, $raw.Length - 2) }
$query = $raw
if ($raw.Contains('?')) { $query = $raw.Split('?', 2)[1] }
$map = @{}
foreach ($pair in $query.Split('&')) {
    if (-not $pair) { continue }
    $kv = $pair.Split('=', 2)
    $k = [Uri]::UnescapeDataString($kv[0])
    $v = if ($kv.Count -gt 1) { [Uri]::UnescapeDataString($kv[1]) } else { '' }
    $map[$k] = $v
}
$ticket = [string]$map['ticket']
$origin = ([string]$map['origin']).TrimEnd('/')
if (-not $ticket -or -not $origin) {
    [System.Windows.Forms.MessageBox]::Show('授权参数不完整。', 'Team48 重新授权')
    exit 1
}
try {
    $cfg = Invoke-RestMethod -Uri ($origin + '/admin/seats/oauth/' + $ticket + '/launch.json') -TimeoutSec 20
} catch {
    [System.Windows.Forms.MessageBox]::Show([string]$_.Exception.Message, 'Team48 重新授权')
    exit 1
}
try {
    Invoke-RestMethod -Method Post -Uri ($origin + '/admin/seats/oauth/' + $ticket + '/ack') -ContentType 'application/json; charset=utf-8' -Body '{}' -TimeoutSec 10 | Out-Null
} catch {}
"""
        + _chrome_oauth_ps1()
    )


def install_protocol_script() -> str:
    handler = protocol_handler_script()
    if "'@" in handler:
        raise ValueError("handler contains powershell here-string terminator")
    template = r"""$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$dir = Join-Path $env:LOCALAPPDATA 'team48-oauth'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$handlerPath = Join-Path $dir 'handler.ps1'
$utf8 = New-Object System.Text.UTF8Encoding $true
[System.IO.File]::WriteAllText($handlerPath, @'
__HANDLER__
'@, $utf8)
$ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$cmd = '"' + $ps + '" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $handlerPath + '" "%1"'
$base = 'HKCU:\Software\Classes\team48-oauth'
New-Item -Path $base -Force | Out-Null
Set-ItemProperty -Path $base -Name '(Default)' -Value 'URL:Team48 OAuth'
Set-ItemProperty -Path $base -Name 'URL Protocol' -Value ''
$icon = Join-Path $base 'DefaultIcon'
New-Item -Path $icon -Force | Out-Null
Set-ItemProperty -Path $icon -Name '(Default)' -Value 'powershell.exe,0'
$shell = Join-Path $base 'shell\open\command'
New-Item -Path $shell -Force | Out-Null
Set-ItemProperty -Path $shell -Name '(Default)' -Value $cmd
[System.Windows.Forms.MessageBox]::Show('本机弹出已装好。回到网页再点弹出授权窗口。', 'Team48 重新授权')
"""
    return template.replace("__HANDLER__", handler)
