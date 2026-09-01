"""Persistent operations and automation policies."""

ACTIVE_STATES = ("queued", "running", "waiting")
TERMINAL_STATES = ("success", "failed", "cancelled", "manual_required")
OPERATION_TYPES = (
    "quota_probe",
    "auth_probe",
    "reauth",
    "onboard",
    "rotate",
    "reconcile",
    "sub2api_sync",
    "proxy_check",
    "free_register",
    "reregister",
)
SENSITIVE_INPUT_KEYS = ("password", "login_password", "code_verifier")
DEFAULT_LEASE_SECONDS = 180
MAX_LOG_ITEMS = 40
