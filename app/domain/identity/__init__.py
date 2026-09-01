"""Identity policy. Plan, role, and local purpose stay independent."""

IDENTITY_GMAIL_POLICY_OWNER_ONLY = "owner_only"
IDENTITY_GMAIL_POLICY_WARN = "warn"
IDENTITY_GMAIL_POLICY_UNRESTRICTED = "unrestricted"

OFFICIAL_PLANS = ("free", "plus", "pro", "team", "business", "unknown")
WORKSPACE_ROLES = ("owner", "admin", "member", "unknown")
LOCAL_PURPOSES = ("mother", "child", "standby", "free", "disabled")
AUTH_STATES = (
    "healthy",
    "refresh_due",
    "refreshing",
    "oauth_required",
    "phone_required",
    "manual_required",
    "deactivated",
    "unknown",
)
OPERATIONAL_STATES = (
    "available",
    "active",
    "standby",
    "disabled",
    "archived",
    "unused",
    "free",
)
MEMBERSHIP_STATES = ("invited", "joined", "removed", "unknown")
BINDING_STATES = ("pending", "verified", "conflict", "missing", "orphaned")
WORKSPACE_STATUSES = ("active", "expired", "error", "unknown")

PROVIDER_SUB2API = "sub2api"

AUDIT_VERIFIED = "verified"
AUDIT_CONFLICT = "conflict"
AUDIT_UNBOUND = "unbound"
AUDIT_SUSPICIOUS = "suspicious"

LOCAL_PURPOSE_MOTHER = "mother"
LOCAL_PURPOSE_CHILD = "child"
LOCAL_PURPOSE_STANDBY = "standby"
LOCAL_PURPOSE_FREE = "free"
LOCAL_PURPOSE_DISABLED = "disabled"

OFFICIAL_PLAN_UNKNOWN = "unknown"
AUTH_STATE_UNKNOWN = "unknown"
OFFICIAL_ROLE_OWNER = "owner"
OFFICIAL_ROLE_UNKNOWN = "unknown"
MEMBERSHIP_STATE_INVITED = "invited"
MEMBERSHIP_STATE_JOINED = "joined"
MEMBERSHIP_STATE_REMOVED = "removed"
MEMBERSHIP_STATE_UNKNOWN = "unknown"

BINDING_PENDING = "pending"
BINDING_VERIFIED = "verified"
BINDING_CONFLICT = "conflict"
BINDING_MISSING = "missing"
BINDING_ORPHANED = "orphaned"

GMAIL_DOMAINS = ("gmail.com", "googlemail.com")
