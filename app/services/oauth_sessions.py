"""子号本机 OAuth 会话。ticket 一次性，给 Windows 回调窗口用。"""
from __future__ import annotations

import secrets
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

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


def create_session(*, team_id: int, email: str, authorize: Dict[str, str]) -> Dict[str, Any]:
    ticket = secrets.token_urlsafe(24)
    now = get_now()
    session = {
        "ticket": ticket,
        "team_id": int(team_id),
        "email": (email or "").strip().lower(),
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


def launcher_script(session: Dict[str, Any], complete_url: str) -> str:
    authorize_url = str(session.get("authorize_url") or "").replace("'", "''")
    ticket = str(session.get("ticket") or "").replace("'", "''")
    complete = str(complete_url or "").replace("'", "''")
    return f"""$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$authorizeUrl = '{authorize_url}'
$ticket = '{ticket}'
$completeUrl = '{complete}'
$prefix = 'http://127.0.0.1:1455/'
$listener = [System.Net.HttpListener]::new()
$listener.Prefixes.Add($prefix)
try {{
    $listener.Start()
}} catch {{
    [System.Windows.Forms.MessageBox]::Show('无法监听 localhost:1455，请先关掉占用这个端口的程序。', 'Team48 认证')
    exit 1
}}
$form = New-Object System.Windows.Forms.Form
$form.Text = 'Team48 本机认证'
$form.Width = 460
$form.Height = 180
$form.StartPosition = 'CenterScreen'
$label = New-Object System.Windows.Forms.Label
$label.AutoSize = $false
$label.Dock = 'Fill'
$label.Padding = New-Object System.Windows.Forms.Padding(16)
$label.Text = '已打开 ChatGPT 授权页。请在浏览器里跑完登录/接码，跳到 localhost 后会自动回收回调并推送到 Sub2API。'
$form.Controls.Add($label)
Start-Process $authorizeUrl | Out-Null
$form.Add_Shown({{ $form.Activate() }})
$task = $listener.GetContextAsync()
while (-not $task.AsyncWaitHandle.WaitOne(200)) {{
    [System.Windows.Forms.Application]::DoEvents()
    if (-not $form.Visible) {{ break }}
}}
if (-not $task.IsCompleted) {{
    $listener.Stop()
    $listener.Close()
    exit 1
}}
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
$body = @{{ ticket = $ticket; callback_text = $callback }} | ConvertTo-Json
try {{
    $resp = Invoke-RestMethod -Method Post -Uri $completeUrl -ContentType 'application/json; charset=utf-8' -Body $body
    $msg = if ($resp.message) {{ [string]$resp.message }} else {{ '认证完成' }}
    [System.Windows.Forms.MessageBox]::Show($msg, 'Team48 认证')
}} catch {{
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Team48 认证失败')
    exit 1
}}
"""
