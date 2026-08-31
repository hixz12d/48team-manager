"""
数据库自动迁移模块
在应用启动时自动检测并执行必要的数据库迁移
"""
import logging
import sqlite3
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)


def get_db_path():
    """获取数据库文件路径"""
    from app.config import settings
    db_file = settings.database_url.split("///")[-1]
    return Path(db_file)


def column_exists(cursor, table_name, column_name):
    """检查表中是否存在指定列。"""
    if not table_exists(cursor, table_name):
        return False
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [row[1] for row in cursor.fetchall()]
    return column_name in columns


def table_exists(cursor, table_name):
    """检查表是否存在"""
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    )
    return cursor.fetchone() is not None


def ensure_identity_tables(cursor, migrations_applied):
    """Phase 1：只新增 identity 表，不改旧表语义。"""
    if not table_exists(cursor, "accounts"):
        logger.info("创建 accounts 表")
        cursor.execute("""
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email VARCHAR(255) NOT NULL UNIQUE,
                official_plan VARCHAR(20) NOT NULL DEFAULT 'unknown',
                official_user_id VARCHAR(100),
                official_account_id VARCHAR(100),
                auth_state VARCHAR(30) NOT NULL DEFAULT 'unknown',
                operational_state VARCHAR(20) NOT NULL DEFAULT 'available',
                local_purpose VARCHAR(20) NOT NULL,
                proxy VARCHAR(500),
                access_token_encrypted TEXT,
                refresh_token_encrypted TEXT,
                session_token_encrypted TEXT,
                id_token_encrypted TEXT,
                client_id VARCHAR(100),
                next_eligible_at DATETIME,
                quota_slot_minute INTEGER,
                next_quota_probe_at DATETIME,
                quota_probe_fail_count INTEGER NOT NULL DEFAULT 0,
                source_team_id INTEGER,
                source_child_account_id INTEGER,
                created_at DATETIME,
                updated_at DATETIME,
                version INTEGER NOT NULL DEFAULT 1
            )
        """)
        migrations_applied.append("accounts")

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_accounts_purpose_state ON accounts (local_purpose, operational_state)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_accounts_source_team ON accounts (source_team_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_accounts_source_child ON accounts (source_child_account_id)"
    )

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_accounts_next_quota_probe ON accounts (next_quota_probe_at)"
    )

    if not table_exists(cursor, "workspaces"):
        logger.info("创建 workspaces 表")
        cursor.execute("""
            CREATE TABLE workspaces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                official_workspace_id VARCHAR(100),
                name VARCHAR(255),
                subscription_plan VARCHAR(100),
                owner_account_id INTEGER,
                status VARCHAR(20) NOT NULL DEFAULT 'active',
                seat_limit INTEGER,
                last_official_sync_at DATETIME,
                source_team_id INTEGER UNIQUE,
                created_at DATETIME,
                updated_at DATETIME,
                version INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY(owner_account_id) REFERENCES accounts(id)
            )
        """)
        migrations_applied.append("workspaces")

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_workspaces_official_id ON workspaces (official_workspace_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_workspaces_owner ON workspaces (owner_account_id)"
    )

    if not table_exists(cursor, "workspace_memberships"):
        logger.info("创建 workspace_memberships 表")
        cursor.execute("""
            CREATE TABLE workspace_memberships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                official_user_id VARCHAR(100),
                official_role VARCHAR(20) NOT NULL DEFAULT 'unknown',
                membership_state VARCHAR(20) NOT NULL DEFAULT 'unknown',
                local_purpose VARCHAR(20) NOT NULL,
                joined_at DATETIME,
                removed_at DATETIME,
                source_mapping_id INTEGER,
                created_at DATETIME,
                updated_at DATETIME,
                FOREIGN KEY(workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE,
                FOREIGN KEY(account_id) REFERENCES accounts(id),
                UNIQUE(workspace_id, account_id)
            )
        """)
        migrations_applied.append("workspace_memberships")

    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_membership_account ON workspace_memberships (workspace_id, account_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_membership_workspace_state ON workspace_memberships (workspace_id, membership_state)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_membership_account ON workspace_memberships (account_id)"
    )

    if not table_exists(cursor, "external_bindings"):
        logger.info("创建 external_bindings 表")
        cursor.execute("""
            CREATE TABLE external_bindings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider VARCHAR(40) NOT NULL,
                local_account_id INTEGER NOT NULL,
                remote_account_id VARCHAR(100) NOT NULL,
                binding_state VARCHAR(20) NOT NULL DEFAULT 'pending',
                verified_email VARCHAR(255),
                verified_official_account_id VARCHAR(100),
                verified_workspace_id VARCHAR(100),
                last_observed_at DATETIME,
                last_error TEXT,
                created_at DATETIME,
                updated_at DATETIME,
                FOREIGN KEY(local_account_id) REFERENCES accounts(id),
                UNIQUE(provider, remote_account_id),
                UNIQUE(provider, local_account_id)
            )
        """)
        migrations_applied.append("external_bindings")

    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_external_binding_remote ON external_bindings (provider, remote_account_id)"
    )
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_external_binding_local ON external_bindings (provider, local_account_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_external_binding_state ON external_bindings (provider, binding_state)"
    )


def ensure_quota_tables(cursor, migrations_applied):
    """Phase 3：官方额度快照与 Account 探测调度字段。不改旧 usage_probe 语义。"""
    if table_exists(cursor, "accounts"):
        if not column_exists(cursor, "accounts", "quota_slot_minute"):
            logger.info("添加 accounts.quota_slot_minute 字段")
            cursor.execute("ALTER TABLE accounts ADD COLUMN quota_slot_minute INTEGER")
            migrations_applied.append("accounts.quota_slot_minute")
        if not column_exists(cursor, "accounts", "next_quota_probe_at"):
            logger.info("添加 accounts.next_quota_probe_at 字段")
            cursor.execute("ALTER TABLE accounts ADD COLUMN next_quota_probe_at DATETIME")
            migrations_applied.append("accounts.next_quota_probe_at")
        if not column_exists(cursor, "accounts", "quota_probe_fail_count"):
            logger.info("添加 accounts.quota_probe_fail_count 字段")
            cursor.execute(
                "ALTER TABLE accounts ADD COLUMN quota_probe_fail_count INTEGER NOT NULL DEFAULT 0"
            )
            migrations_applied.append("accounts.quota_probe_fail_count")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_accounts_next_quota_probe ON accounts (next_quota_probe_at)"
        )

    if not table_exists(cursor, "quota_snapshots"):
        logger.info("创建 quota_snapshots 表")
        cursor.execute("""
            CREATE TABLE quota_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                five_hour_used_percent INTEGER,
                five_hour_reset_at DATETIME,
                seven_day_used_percent INTEGER,
                seven_day_reset_at DATETIME,
                source VARCHAR(20) NOT NULL DEFAULT 'official',
                queried_at DATETIME NOT NULL,
                success BOOLEAN NOT NULL DEFAULT 0,
                error_code VARCHAR(40),
                error_message TEXT,
                created_at DATETIME,
                FOREIGN KEY(account_id) REFERENCES accounts(id)
            )
        """)
        migrations_applied.append("quota_snapshots")

    if table_exists(cursor, "quota_snapshots"):
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_quota_snapshots_account_queried ON quota_snapshots (account_id, queried_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_quota_snapshots_success ON quota_snapshots (account_id, success, queried_at)"
        )


def ensure_operation_tables(cursor, migrations_applied):
    """Phase 4：长任务落库。不改踢拉业务语义，不删 _JOBS 兼容 API。"""
    if not table_exists(cursor, "operations"):
        logger.info("创建 operations 表")
        cursor.execute("""
            CREATE TABLE operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_id VARCHAR(32) NOT NULL UNIQUE,
                type VARCHAR(40) NOT NULL,
                entity_type VARCHAR(40),
                entity_id INTEGER,
                workspace_id INTEGER,
                account_id INTEGER,
                email VARCHAR(255),
                phone VARCHAR(64),
                state VARCHAR(20) NOT NULL DEFAULT 'queued',
                current_step VARCHAR(40),
                idempotency_key VARCHAR(120),
                locked_by VARCHAR(80),
                lease_expires_at DATETIME,
                cancel_requested BOOLEAN NOT NULL DEFAULT 0,
                input_json TEXT,
                result_json TEXT,
                error_code VARCHAR(40),
                error_message TEXT,
                log_json TEXT,
                created_at DATETIME,
                started_at DATETIME,
                finished_at DATETIME,
                updated_at DATETIME
            )
        """)
        migrations_applied.append("operations")

    if table_exists(cursor, "operations"):
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_operations_state_lease ON operations (state, lease_expires_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_operations_email_created ON operations (email, created_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_operations_type_state ON operations (type, state)"
        )

    if not table_exists(cursor, "operation_steps"):
        logger.info("创建 operation_steps 表")
        cursor.execute("""
            CREATE TABLE operation_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id INTEGER NOT NULL,
                step_name VARCHAR(40) NOT NULL,
                state VARCHAR(20) NOT NULL DEFAULT 'queued',
                attempt INTEGER NOT NULL DEFAULT 1,
                input_snapshot TEXT,
                result_snapshot TEXT,
                started_at DATETIME,
                finished_at DATETIME,
                error_code VARCHAR(40),
                error_message TEXT,
                FOREIGN KEY(operation_id) REFERENCES operations(id) ON DELETE CASCADE
            )
        """)
        migrations_applied.append("operation_steps")

    if table_exists(cursor, "operation_steps"):
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_operation_step_name ON operation_steps (operation_id, step_name)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_operation_steps_op ON operation_steps (operation_id, state)"
        )


def run_auto_migration(db_path=None):
    """
    自动运行数据库迁移
    检测缺失的列并自动添加。新 identity 表只新增，不改旧表语义。
    """
    db_path = Path(db_path) if db_path is not None else get_db_path()
    
    if not db_path.exists():
        logger.info("数据库文件不存在，跳过迁移")
        return
    
    logger.info("开始检查数据库迁移...")
    
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        
        migrations_applied = []
        
        # 检查并添加质保相关字段
        if table_exists(cursor, "redemption_codes") and not column_exists(cursor, "redemption_codes", "has_warranty"):
            logger.info("添加 redemption_codes.has_warranty 字段")
            cursor.execute("""
                ALTER TABLE redemption_codes 
                ADD COLUMN has_warranty BOOLEAN DEFAULT 0
            """)
            migrations_applied.append("redemption_codes.has_warranty")
        
        if table_exists(cursor, "redemption_codes") and not column_exists(cursor, "redemption_codes", "warranty_expires_at"):
            logger.info("添加 redemption_codes.warranty_expires_at 字段")
            cursor.execute("""
                ALTER TABLE redemption_codes 
                ADD COLUMN warranty_expires_at DATETIME
            """)
            migrations_applied.append("redemption_codes.warranty_expires_at")
        
        if table_exists(cursor, "redemption_codes") and not column_exists(cursor, "redemption_codes", "warranty_days"):
            logger.info("添加 redemption_codes.warranty_days 字段")
            cursor.execute("""
                ALTER TABLE redemption_codes 
                ADD COLUMN warranty_days INTEGER DEFAULT 30
            """)
            migrations_applied.append("redemption_codes.warranty_days")
        
        if table_exists(cursor, "redemption_records") and not column_exists(cursor, "redemption_records", "is_warranty_redemption"):
            logger.info("添加 redemption_records.is_warranty_redemption 字段")
            cursor.execute("""
                ALTER TABLE redemption_records 
                ADD COLUMN is_warranty_redemption BOOLEAN DEFAULT 0
            """)
            migrations_applied.append("redemption_records.is_warranty_redemption")

        # 检查并添加 Token 刷新相关字段
        if table_exists(cursor, "teams") and not column_exists(cursor, "teams", "refresh_token_encrypted"):
            logger.info("添加 teams.refresh_token_encrypted 字段")
            cursor.execute("ALTER TABLE teams ADD COLUMN refresh_token_encrypted TEXT")
            migrations_applied.append("teams.refresh_token_encrypted")

        if table_exists(cursor, "teams") and not column_exists(cursor, "teams", "id_token_encrypted"):
            logger.info("添加 teams.id_token_encrypted 字段")
            cursor.execute("ALTER TABLE teams ADD COLUMN id_token_encrypted TEXT")
            migrations_applied.append("teams.id_token_encrypted")

        if table_exists(cursor, "teams") and not column_exists(cursor, "teams", "session_token_encrypted"):
            logger.info("添加 teams.session_token_encrypted 字段")
            cursor.execute("ALTER TABLE teams ADD COLUMN session_token_encrypted TEXT")
            migrations_applied.append("teams.session_token_encrypted")

        if table_exists(cursor, "teams") and not column_exists(cursor, "teams", "client_id"):
            logger.info("添加 teams.client_id 字段")
            cursor.execute("ALTER TABLE teams ADD COLUMN client_id VARCHAR(100)")
            migrations_applied.append("teams.client_id")

        if table_exists(cursor, "teams") and not column_exists(cursor, "teams", "error_count"):
            logger.info("添加 teams.error_count 字段")
            cursor.execute("ALTER TABLE teams ADD COLUMN error_count INTEGER DEFAULT 0")
            migrations_applied.append("teams.error_count")

        if table_exists(cursor, "teams") and not column_exists(cursor, "teams", "account_role"):
            logger.info("添加 teams.account_role 字段")
            cursor.execute("ALTER TABLE teams ADD COLUMN account_role VARCHAR(50)")
            migrations_applied.append("teams.account_role")

        if table_exists(cursor, "teams") and not column_exists(cursor, "teams", "device_code_auth_enabled"):
            logger.info("添加 teams.device_code_auth_enabled 字段")
            cursor.execute("ALTER TABLE teams ADD COLUMN device_code_auth_enabled BOOLEAN DEFAULT 0")
            migrations_applied.append("teams.device_code_auth_enabled")
        

        if table_exists(cursor, "teams") and not column_exists(cursor, "teams", "pool_type"):
            logger.info("添加 teams.pool_type 字段")
            cursor.execute("ALTER TABLE teams ADD COLUMN pool_type VARCHAR(20) DEFAULT 'normal'")
            migrations_applied.append("teams.pool_type")

        if table_exists(cursor, "teams") and not column_exists(cursor, "teams", "warranty_seat_enabled"):
            logger.info("添加 teams.warranty_seat_enabled 字段")
            cursor.execute("ALTER TABLE teams ADD COLUMN warranty_seat_enabled BOOLEAN DEFAULT 0")
            migrations_applied.append("teams.warranty_seat_enabled")

        if table_exists(cursor, "redemption_codes") and not column_exists(cursor, "redemption_codes", "pool_type"):
            logger.info("添加 redemption_codes.pool_type 字段")
            cursor.execute("ALTER TABLE redemption_codes ADD COLUMN pool_type VARCHAR(20) DEFAULT 'normal'")
            migrations_applied.append("redemption_codes.pool_type")

        if table_exists(cursor, "redemption_codes") and not column_exists(cursor, "redemption_codes", "reusable_by_seat"):
            logger.info("添加 redemption_codes.reusable_by_seat 字段")
            cursor.execute("ALTER TABLE redemption_codes ADD COLUMN reusable_by_seat BOOLEAN DEFAULT 0")
            migrations_applied.append("redemption_codes.reusable_by_seat")

        if table_exists(cursor, "redemption_codes") and not column_exists(cursor, "redemption_codes", "extension_days"):
            logger.info("添加 redemption_codes.extension_days 字段")
            cursor.execute("ALTER TABLE redemption_codes ADD COLUMN extension_days INTEGER DEFAULT 0")
            migrations_applied.append("redemption_codes.extension_days")

        if not table_exists(cursor, "team_email_mappings"):
            logger.info("创建 team_email_mappings 表")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS team_email_mappings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    team_id INTEGER NOT NULL,
                    email VARCHAR(255) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'invited',
                    source VARCHAR(20) NOT NULL DEFAULT 'sync',
                    last_seen_at DATETIME,
                    missing_sync_count INTEGER NOT NULL DEFAULT 0,
                    is_admin_invited BOOLEAN NOT NULL DEFAULT 0,
                    created_at DATETIME,
                    updated_at DATETIME,
                    FOREIGN KEY(team_id) REFERENCES teams(id) ON DELETE CASCADE
                )
            """)
            migrations_applied.append("team_email_mappings")

        if table_exists(cursor, "team_email_mappings") and not column_exists(cursor, "team_email_mappings", "missing_sync_count"):
            logger.info("添加 team_email_mappings.missing_sync_count 字段")
            cursor.execute("""
                ALTER TABLE team_email_mappings
                ADD COLUMN missing_sync_count INTEGER NOT NULL DEFAULT 0
            """)
            migrations_applied.append("team_email_mappings.missing_sync_count")

        if table_exists(cursor, "team_email_mappings") and not column_exists(cursor, "team_email_mappings", "is_admin_invited"):
            logger.info("添加 team_email_mappings.is_admin_invited 字段")
            cursor.execute("""
                ALTER TABLE team_email_mappings
                ADD COLUMN is_admin_invited BOOLEAN NOT NULL DEFAULT 0
            """)
            migrations_applied.append("team_email_mappings.is_admin_invited")

        if table_exists(cursor, "team_email_mappings"):
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_team_email_unique
                ON team_email_mappings (team_id, email)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_team_email_email
                ON team_email_mappings (email)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_team_email_status
                ON team_email_mappings (team_id, status)
            """)

        if not table_exists(cursor, "renewal_requests"):
            logger.info("创建 renewal_requests 表")
            cursor.execute("""
                CREATE TABLE renewal_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email VARCHAR(255) NOT NULL,
                    code VARCHAR(32) NOT NULL,
                    team_id INTEGER,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    requested_at DATETIME,
                    handled_at DATETIME,
                    extension_days INTEGER,
                    admin_note TEXT,
                    FOREIGN KEY(code) REFERENCES redemption_codes(code),
                    FOREIGN KEY(team_id) REFERENCES teams(id)
                )
            """)
            migrations_applied.append("renewal_requests")

        if table_exists(cursor, "renewal_requests"):
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_renewal_request_status
                ON renewal_requests (status)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_renewal_request_email
                ON renewal_requests (email)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_renewal_request_code
                ON renewal_requests (code)
            """)

        # 把 renewal_requests.code 从 NOT NULL 改为允许 NULL：
        # 兑换码销毁后 extended/ignored 历史保留作为审计证据，需要 code 可空。
        # SQLite 不支持 ALTER COLUMN，必须重建表。
        cursor.execute("PRAGMA table_info(renewal_requests)")
        rr_cols = {row[1]: row for row in cursor.fetchall()}  # name -> full row
        if rr_cols and rr_cols.get("code") and rr_cols["code"][3] == 1:
            # 第四列 (notnull) == 1 表示当前 NOT NULL，需要重建
            logger.info("renewal_requests.code 改为允许 NULL，重建表")
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.execute("""
                CREATE TABLE renewal_requests_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email VARCHAR(255) NOT NULL,
                    code VARCHAR(32),
                    team_id INTEGER,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    requested_at DATETIME,
                    handled_at DATETIME,
                    extension_days INTEGER,
                    admin_note TEXT,
                    FOREIGN KEY(code) REFERENCES redemption_codes(code),
                    FOREIGN KEY(team_id) REFERENCES teams(id)
                )
            """)
            cursor.execute("""
                INSERT INTO renewal_requests_new
                (id, email, code, team_id, status, requested_at, handled_at, extension_days, admin_note)
                SELECT id, email, code, team_id, status, requested_at, handled_at, extension_days, admin_note
                FROM renewal_requests
            """)
            cursor.execute("DROP TABLE renewal_requests")
            cursor.execute("ALTER TABLE renewal_requests_new RENAME TO renewal_requests")
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_renewal_request_status
                ON renewal_requests (status)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_renewal_request_email
                ON renewal_requests (email)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_renewal_request_code
                ON renewal_requests (code)
            """)
            cursor.execute("PRAGMA foreign_keys=ON")
            migrations_applied.append("renewal_requests.code -> NULLABLE")

        if table_exists(cursor, "teams") and not column_exists(cursor, "teams", "proxy"):
            logger.info("添加 teams.proxy 字段")
            cursor.execute("ALTER TABLE teams ADD COLUMN proxy VARCHAR(500)")
            migrations_applied.append("teams.proxy")

        if table_exists(cursor, "teams") and not column_exists(cursor, "teams", "seat_cycle_days"):
            logger.info("添加 teams.seat_cycle_days 字段")
            cursor.execute("ALTER TABLE teams ADD COLUMN seat_cycle_days INTEGER DEFAULT 7")
            migrations_applied.append("teams.seat_cycle_days")

        if table_exists(cursor, "teams") and not column_exists(cursor, "teams", "rotation_manual_count"):
            logger.info("添加 teams.rotation_manual_count 字段")
            cursor.execute("ALTER TABLE teams ADD COLUMN rotation_manual_count INTEGER")
            migrations_applied.append("teams.rotation_manual_count")

        if table_exists(cursor, "teams") and not column_exists(cursor, "teams", "rotation_manual_on"):
            logger.info("添加 teams.rotation_manual_on 字段")
            cursor.execute("ALTER TABLE teams ADD COLUMN rotation_manual_on VARCHAR(10)")
            migrations_applied.append("teams.rotation_manual_on")

        if table_exists(cursor, "teams") and not column_exists(cursor, "teams", "sub2api_account_id"):
            logger.info("添加 teams.sub2api_account_id 字段")
            cursor.execute("ALTER TABLE teams ADD COLUMN sub2api_account_id INTEGER")
            migrations_applied.append("teams.sub2api_account_id")

        if not table_exists(cursor, "child_accounts"):
            logger.info("创建 child_accounts 表")
            cursor.execute("""
                CREATE TABLE child_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email VARCHAR(255) NOT NULL UNIQUE,
                    password_encrypted TEXT,
                    mail_raw TEXT,
                    phone VARCHAR(50),
                    sms_url TEXT,
                    proxy VARCHAR(500),
                    status VARCHAR(20) NOT NULL DEFAULT 'unused',
                    current_team_id INTEGER,
                    last_team_id INTEGER,
                    joined_at DATETIME,
                    kicked_at DATETIME,
                    cycle_days INTEGER DEFAULT 7,
                    access_token_encrypted TEXT,
                    refresh_token_encrypted TEXT,
                    session_token_encrypted TEXT,
                    id_token_encrypted TEXT,
                    client_id VARCHAR(100),
                    account_id VARCHAR(100),
                    sub2api_account_id INTEGER,
                    last_error TEXT,
                    last_stage VARCHAR(40),
                    last_job_id VARCHAR(32),
                    created_at DATETIME,
                    updated_at DATETIME,
                    FOREIGN KEY(current_team_id) REFERENCES teams(id)
                )
            """)
            migrations_applied.append("child_accounts")

        if not table_exists(cursor, "seat_events"):
            logger.info("创建 seat_events 表")
            cursor.execute("""
                CREATE TABLE seat_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    child_account_id INTEGER,
                    team_id INTEGER,
                    email VARCHAR(255) NOT NULL,
                    action VARCHAR(40) NOT NULL,
                    success BOOLEAN NOT NULL DEFAULT 1,
                    detail TEXT,
                    created_at DATETIME,
                    FOREIGN KEY(child_account_id) REFERENCES child_accounts(id) ON DELETE SET NULL,
                    FOREIGN KEY(team_id) REFERENCES teams(id) ON DELETE SET NULL
                )
            """)
            migrations_applied.append("seat_events")

        if table_exists(cursor, "team_email_mappings") and not column_exists(cursor, "team_email_mappings", "child_account_id"):
            logger.info("添加 team_email_mappings.child_account_id 字段")
            cursor.execute("ALTER TABLE team_email_mappings ADD COLUMN child_account_id INTEGER")
            migrations_applied.append("team_email_mappings.child_account_id")

        if table_exists(cursor, "team_email_mappings") and not column_exists(cursor, "team_email_mappings", "joined_at"):
            logger.info("添加 team_email_mappings.joined_at 字段")
            cursor.execute("ALTER TABLE team_email_mappings ADD COLUMN joined_at DATETIME")
            migrations_applied.append("team_email_mappings.joined_at")

        if table_exists(cursor, "team_email_mappings") and not column_exists(cursor, "team_email_mappings", "kicked_at"):
            logger.info("添加 team_email_mappings.kicked_at 字段")
            cursor.execute("ALTER TABLE team_email_mappings ADD COLUMN kicked_at DATETIME")
            migrations_applied.append("team_email_mappings.kicked_at")

        if table_exists(cursor, "team_email_mappings") and not column_exists(cursor, "team_email_mappings", "cycle_days"):
            logger.info("添加 team_email_mappings.cycle_days 字段")
            cursor.execute("ALTER TABLE team_email_mappings ADD COLUMN cycle_days INTEGER DEFAULT 7")
            migrations_applied.append("team_email_mappings.cycle_days")

        if table_exists(cursor, "child_accounts") and not column_exists(cursor, "child_accounts", "last_stage"):
            logger.info("添加 child_accounts.last_stage 字段")
            cursor.execute("ALTER TABLE child_accounts ADD COLUMN last_stage VARCHAR(40)")
            migrations_applied.append("child_accounts.last_stage")

        if table_exists(cursor, "child_accounts") and not column_exists(cursor, "child_accounts", "last_job_id"):
            logger.info("添加 child_accounts.last_job_id 字段")
            cursor.execute("ALTER TABLE child_accounts ADD COLUMN last_job_id VARCHAR(32)")
            migrations_applied.append("child_accounts.last_job_id")

        if table_exists(cursor, "child_accounts") and not column_exists(cursor, "child_accounts", "probe_status"):
            logger.info("添加 child_accounts.probe_status 字段")
            cursor.execute("ALTER TABLE child_accounts ADD COLUMN probe_status VARCHAR(20)")
            migrations_applied.append("child_accounts.probe_status")

        if table_exists(cursor, "child_accounts") and not column_exists(cursor, "child_accounts", "probe_label"):
            logger.info("添加 child_accounts.probe_label 字段")
            cursor.execute("ALTER TABLE child_accounts ADD COLUMN probe_label VARCHAR(40)")
            migrations_applied.append("child_accounts.probe_label")

        if table_exists(cursor, "child_accounts") and not column_exists(cursor, "child_accounts", "probed_at"):
            logger.info("添加 child_accounts.probed_at 字段")
            cursor.execute("ALTER TABLE child_accounts ADD COLUMN probed_at DATETIME")
            migrations_applied.append("child_accounts.probed_at")

        if table_exists(cursor, "child_accounts") and not column_exists(cursor, "child_accounts", "next_eligible_at"):
            logger.info("添加 child_accounts.next_eligible_at 字段")
            cursor.execute("ALTER TABLE child_accounts ADD COLUMN next_eligible_at DATETIME")
            migrations_applied.append("child_accounts.next_eligible_at")

        if table_exists(cursor, "child_accounts"):
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_child_status ON child_accounts (status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_child_team ON child_accounts (current_team_id, status)")
        if table_exists(cursor, "seat_events"):
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_seat_event_email ON seat_events (email)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_seat_event_team ON seat_events (team_id, created_at)")

        if not table_exists(cursor, "seat_vacancy_events"):
            logger.info("创建 seat_vacancy_events 表")
            cursor.execute("""
                CREATE TABLE seat_vacancy_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    team_id INTEGER NOT NULL,
                    account_id VARCHAR(100),
                    user_id VARCHAR(100),
                    email VARCHAR(255),
                    policy_notice_null BOOLEAN NOT NULL DEFAULT 0,
                    vacancy_ordinal INTEGER,
                    free_vacancy_threshold INTEGER,
                    billing_starts_at DATETIME,
                    expires_at DATETIME,
                    is_free BOOLEAN,
                    has_billing_notice BOOLEAN NOT NULL DEFAULT 0,
                    policy_kind VARCHAR(80),
                    billed_seat_delta INTEGER,
                    replacement_required BOOLEAN,
                    policy_notice_json TEXT,
                    billing_notice_json TEXT,
                    captured_at DATETIME NOT NULL,
                    FOREIGN KEY(team_id) REFERENCES teams(id) ON DELETE CASCADE
                )
            """)
            migrations_applied.append("seat_vacancy_events")

        vacancy_columns = (
            ("has_billing_notice", "BOOLEAN NOT NULL DEFAULT 0"),
            ("policy_kind", "VARCHAR(80)"),
            ("billed_seat_delta", "INTEGER"),
            ("replacement_required", "BOOLEAN"),
            ("policy_notice_json", "TEXT"),
            ("billing_notice_json", "TEXT"),
        )
        if table_exists(cursor, "seat_vacancy_events"):
            for column_name, column_sql in vacancy_columns:
                if not column_exists(cursor, "seat_vacancy_events", column_name):
                    logger.info("添加 seat_vacancy_events.%s 字段", column_name)
                    cursor.execute(
                        f"ALTER TABLE seat_vacancy_events ADD COLUMN {column_name} {column_sql}"
                    )
                    migrations_applied.append(f"seat_vacancy_events.{column_name}")

        if table_exists(cursor, "seat_vacancy_events"):
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_vacancy_team_captured ON seat_vacancy_events (team_id, captured_at)"
            )

        if not table_exists(cursor, "sub2api_usage_ledgers"):
            logger.info("创建 sub2api_usage_ledgers 表")
            cursor.execute("""
                CREATE TABLE sub2api_usage_ledgers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ledger_key VARCHAR(255) NOT NULL UNIQUE,
                    email VARCHAR(255),
                    sub2api_account_id INTEGER,
                    family VARCHAR(255),
                    last_account_cost FLOAT,
                    last_user_cost FLOAT,
                    lifetime_account_cost FLOAT DEFAULT 0,
                    lifetime_user_cost FLOAT DEFAULT 0,
                    created_at DATETIME,
                    updated_at DATETIME
                )
            """)
            migrations_applied.append("sub2api_usage_ledgers")

        if table_exists(cursor, "sub2api_usage_ledgers"):
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_sub2api_ledger_email ON sub2api_usage_ledgers (email)"
            )

        if not table_exists(cursor, "sub2api_usage_probes"):
            logger.info("创建 sub2api_usage_probes 表")
            cursor.execute("""
                CREATE TABLE sub2api_usage_probes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sub2api_account_id INTEGER NOT NULL UNIQUE,
                    email VARCHAR(255),
                    next_probe_at DATETIME NOT NULL,
                    fail_count INTEGER NOT NULL DEFAULT 0,
                    last_kind VARCHAR(20),
                    last_label VARCHAR(40),
                    last_error TEXT,
                    last_probed_at DATETIME,
                    created_at DATETIME,
                    updated_at DATETIME
                )
            """)
            migrations_applied.append("sub2api_usage_probes")

        if table_exists(cursor, "sub2api_usage_probes"):
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_probe_next ON sub2api_usage_probes (next_probe_at)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_probe_email ON sub2api_usage_probes (email)"
            )

        if table_exists(cursor, "sub2api_usage_probes"):
            if table_exists(cursor, "sub2api_usage_probes") and not column_exists(cursor, "sub2api_usage_probes", "next_reauth_at"):
                logger.info("添加 sub2api_usage_probes.next_reauth_at 字段")
                cursor.execute("ALTER TABLE sub2api_usage_probes ADD COLUMN next_reauth_at DATETIME")
                migrations_applied.append("sub2api_usage_probes.next_reauth_at")
            if table_exists(cursor, "sub2api_usage_probes") and not column_exists(cursor, "sub2api_usage_probes", "reauth_fail_count"):
                logger.info("添加 sub2api_usage_probes.reauth_fail_count 字段")
                cursor.execute("ALTER TABLE sub2api_usage_probes ADD COLUMN reauth_fail_count INTEGER NOT NULL DEFAULT 0")
                migrations_applied.append("sub2api_usage_probes.reauth_fail_count")
            if table_exists(cursor, "sub2api_usage_probes") and not column_exists(cursor, "sub2api_usage_probes", "last_reauth_code"):
                logger.info("添加 sub2api_usage_probes.last_reauth_code 字段")
                cursor.execute("ALTER TABLE sub2api_usage_probes ADD COLUMN last_reauth_code VARCHAR(40)")
                migrations_applied.append("sub2api_usage_probes.last_reauth_code")
            if table_exists(cursor, "sub2api_usage_probes") and not column_exists(cursor, "sub2api_usage_probes", "next_rotate_at"):
                logger.info("添加 sub2api_usage_probes.next_rotate_at 字段")
                cursor.execute("ALTER TABLE sub2api_usage_probes ADD COLUMN next_rotate_at DATETIME")
                migrations_applied.append("sub2api_usage_probes.next_rotate_at")
            if table_exists(cursor, "sub2api_usage_probes") and not column_exists(cursor, "sub2api_usage_probes", "rotate_fail_count"):
                logger.info("添加 sub2api_usage_probes.rotate_fail_count 字段")
                cursor.execute("ALTER TABLE sub2api_usage_probes ADD COLUMN rotate_fail_count INTEGER NOT NULL DEFAULT 0")
                migrations_applied.append("sub2api_usage_probes.rotate_fail_count")
            if table_exists(cursor, "sub2api_usage_probes") and not column_exists(cursor, "sub2api_usage_probes", "last_rotate_code"):
                logger.info("添加 sub2api_usage_probes.last_rotate_code 字段")
                cursor.execute("ALTER TABLE sub2api_usage_probes ADD COLUMN last_rotate_code VARCHAR(40)")
                migrations_applied.append("sub2api_usage_probes.last_rotate_code")

        if not table_exists(cursor, "hme_alias_leases"):
            logger.info("创建 hme_alias_leases 表")
            cursor.execute("""
                CREATE TABLE hme_alias_leases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email VARCHAR(255) NOT NULL UNIQUE,
                    anonymous_id VARCHAR(255) NOT NULL,
                    account_id VARCHAR(100) NOT NULL,
                    job_id VARCHAR(32),
                    purpose VARCHAR(40),
                    team_id INTEGER,
                    expires_at DATETIME NOT NULL,
                    created_at DATETIME
                )
            """)
            migrations_applied.append("hme_alias_leases")

        if table_exists(cursor, "hme_alias_leases"):
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_hme_lease_expires ON hme_alias_leases (expires_at)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_hme_lease_job ON hme_alias_leases (job_id)"
            )


        if not table_exists(cursor, "phone_pool"):
            logger.info("创建 phone_pool 表")
            cursor.execute("""
                CREATE TABLE phone_pool (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    number VARCHAR(32) NOT NULL UNIQUE,
                    sms_url TEXT NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'active',
                    used_count INTEGER NOT NULL DEFAULT 0,
                    max_uses INTEGER,
                    last_used_at DATETIME,
                    last_success_at DATETIME,
                    last_error TEXT,
                    last_error_type VARCHAR(40),
                    reserved_by VARCHAR(64),
                    reserved_at DATETIME,
                    risk_count INTEGER NOT NULL DEFAULT 0,
                    no_sms_streak INTEGER NOT NULL DEFAULT 0,
                    note TEXT,
                    created_at DATETIME,
                    updated_at DATETIME
                )
            """)
            migrations_applied.append("phone_pool")

        if table_exists(cursor, "phone_pool"):
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_phone_pool_status ON phone_pool (status)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_phone_pool_reserved ON phone_pool (reserved_by)"
            )


        ensure_identity_tables(cursor, migrations_applied)
        ensure_quota_tables(cursor, migrations_applied)
        ensure_operation_tables(cursor, migrations_applied)

        if table_exists(cursor, "settings"):
            cursor.execute("SELECT 1 FROM settings WHERE key = ?", ("free_account_proxy",))
            if cursor.fetchone() is None:
                logger.info("补充 settings.free_account_proxy")
                cursor.execute(
                    "INSERT INTO settings (key, value, description, created_at, updated_at) VALUES (?, ?, ?, datetime('now'), datetime('now'))",
                    ("free_account_proxy", "", "免费号默认静态 ISP"),
                )
                migrations_applied.append("settings.free_account_proxy")

        # 提交更改
        conn.commit()
        
        if migrations_applied:
            logger.info(f"数据库迁移完成，应用了 {len(migrations_applied)} 个迁移: {', '.join(migrations_applied)}")
        else:
            logger.info("数据库已是最新版本，无需迁移")
        
        conn.close()
        
    except Exception as e:
        logger.error(f"数据库迁移失败: {e}")
        raise


if __name__ == "__main__":
    # 允许直接运行此脚本进行迁移
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    run_auto_migration()
    print("迁移完成")
