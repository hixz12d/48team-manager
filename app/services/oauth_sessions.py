"""子号本机 OAuth 会话。ticket 一次性，给 Windows 回调窗口用。"""
from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timedelta
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
    }


def protocol_url(ticket: str, origin: str) -> str:
    from urllib.parse import quote

    base = (origin or "").strip().rstrip("/")
    return f"{PROTOCOL_NAME}://launch?ticket={quote(ticket or '', safe='')}&origin={quote(base, safe='')}"


def _chrome_oauth_ps1() -> str:
    return r"""if (-not $cfg.proxyServer) {
    [System.Windows.Forms.MessageBox]::Show('这个号没有静态 ISP 代理，不能弹出授权页。', 'Team48 重新授权')
    exit 1
}
$prefix = 'http://127.0.0.1:1455/'
$listener = [System.Net.HttpListener]::new()
$listener.Prefixes.Add($prefix)
try {
    $listener.Start()
} catch {
    [System.Windows.Forms.MessageBox]::Show('无法监听 localhost:1455，请先关掉占用这个端口的程序。', 'Team48 重新授权')
    exit 1
}
$work = Join-Path $env:TEMP ('team48-oauth-' + $cfg.ticket.Substring(0, [Math]::Min(8, $cfg.ticket.Length)))
New-Item -ItemType Directory -Force -Path $work | Out-Null
$chrome = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LocalAppData\Google\Chrome\Application\chrome.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) {
    $listener.Stop(); $listener.Close()
    [System.Windows.Forms.MessageBox]::Show('找不到 Chrome / Edge，无法弹出授权页。', 'Team48 重新授权')
    exit 1
}
$args = @(
    ('--user-data-dir=' + (Join-Path $work 'chrome-profile')),
    '--no-first-run',
    '--no-default-browser-check',
    '--new-window',
    ('--proxy-server=' + $cfg.proxyServer),
    '--proxy-bypass-list=localhost;127.0.0.1;<-loopback>',
    $cfg.authorizeUrl
)
if ($cfg.proxyUser) {
    $ext = Join-Path $work 'proxy-auth'
    New-Item -ItemType Directory -Force -Path $ext | Out-Null
    $manifest = '{ "manifest_version": 2, "name": "Team48 Proxy Auth", "version": "1.0", "permissions": ["webRequest", "webRequestBlocking", "<all_urls>"], "background": { "scripts": ["background.js"], "persistent": true } }'
    $bg = 'chrome.webRequest.onAuthRequired.addListener(function(){return {authCredentials:{username:' + (ConvertTo-Json $cfg.proxyUser -Compress) + ',password:' + (ConvertTo-Json $cfg.proxyPass -Compress) + '}};},{urls:["<all_urls>"]},["blocking"]);'
    Set-Content -LiteralPath (Join-Path $ext 'manifest.json') -Value $manifest -Encoding utf8
    Set-Content -LiteralPath (Join-Path $ext 'background.js') -Value $bg -Encoding utf8
    $args = @($args[0], $args[1], $args[2], $args[3], $args[4], $args[5], ('--load-extension=' + $ext), $args[6])
}
Start-Process -FilePath $chrome -ArgumentList $args | Out-Null
$task = $listener.GetContextAsync()
while (-not $task.AsyncWaitHandle.WaitOne(200)) {
    [System.Windows.Forms.Application]::DoEvents()
}
if (-not $task.IsCompleted) {
    $listener.Stop()
    $listener.Close()
    exit 1
}
$context = $task.Result
$callback = $context.Request.Url.AbsoluteUri
$html = '<html><body style="font-family:sans-serif;padding:24px">认证回调已收到，可以关闭这个窗口。</body></html>'
$buffer = [System.Text.Encoding]::UTF8.GetBytes($html)
$context.Response.StatusCode = 200
$context.Response.ContentType = 'text/html; charset=utf-8'
$context.Response.OutputStream.Write($buffer, 0, $buffer.Length)
$context.Response.Close()
$listener.Stop()
$listener.Close()
$body = @{ ticket = $cfg.ticket; callback_text = $callback } | ConvertTo-Json
try {
    $resp = Invoke-RestMethod -Method Post -Uri $cfg.completeUrl -ContentType 'application/json; charset=utf-8' -Body $body
    $msg = if ($resp.message) { [string]$resp.message } else { '重新授权完成' }
    [System.Windows.Forms.MessageBox]::Show($msg, 'Team48 重新授权')
} catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Team48 重新授权失败')
    exit 1
}
"""


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
$cfg = Invoke-RestMethod -Uri ($origin + '/admin/seats/oauth/' + $ticket + '/launch.json') -TimeoutSec 20
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
Set-Content -LiteralPath $handlerPath -Value @'
__HANDLER__
'@ -Encoding utf8
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
[System.Windows.Forms.MessageBox]::Show('本机弹出已装好。回到网页再点一次弹出授权窗口，浏览器问是否打开时选允许。', 'Team48 重新授权')
"""
    return template.replace("__HANDLER__", handler)
