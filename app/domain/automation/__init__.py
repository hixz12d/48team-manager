"""Persistent operations and automation policies."""

ACTIVE_STATES = ("queued", "running", "waiting")
TERMINAL_STATES = ("success", "partial", "failed", "cancelled", "manual_required")
OPERATION_TYPES = (
    "quota_probe",
    "auth_probe",
    "reauth",
    "onboard",
    "rotate",
    "kick_member",
    "purge_child",
    "invite_child",
    "revoke_invite",
    "hme_reconcile",
    "hme_label_retry",
    "workspace_sync",
    "sub2api_sync",
    "sub2api_reconcile",
    "sub2api_push",
    "proxy_check",
    "free_register",
    "reregister",
)
SENSITIVE_INPUT_KEYS = (
    "password",
    "login_password",
    "code_verifier",
    "email_line",
    "phone_line",
    "pickup_url",
    "sms_url",
    "proxy",
    "resolved_proxy",
    "oauth_secret",
    "client_secret",
)
REDACT_VALUE_MARKERS = ("password", "passwd", "secret", "token", "verifier")
DEFAULT_LEASE_SECONDS = 180
MAX_LOG_ITEMS = 40
BROWSER_ACTIONS = ("reauth", "onboard", "rotate", "free_register", "reregister", "free")
WORKSPACE_LOCK_ACTIONS = ("rotate", "onboard", "reregister", "kick_member", "purge_child", "invite_child", "revoke_invite")
DEFAULT_AUTO_REAUTH_ENABLED = False
DEFAULT_AUTO_REAUTH_INTERVAL_MINUTES = 30
MIN_AUTO_REAUTH_INTERVAL_MINUTES = 5
MAX_AUTO_REAUTH_INTERVAL_MINUTES = 24 * 60
DEFAULT_AUTH_PROBE_ENABLED = True
DEFAULT_AUTH_PROBE_INTERVAL_MINUTES = 30
DEFAULT_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_REDIRECT_URI = "http://localhost:1455/auth/callback"
TOKEN_REFRESH_WINDOW_HOURS = 2
