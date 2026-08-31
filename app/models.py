"""
数据库模型定义
定义所有数据库表的 SQLAlchemy 模型
"""
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, Float, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base
from app.utils.time_utils import get_now


class Team(Base):
    """Team 信息表"""
    __tablename__ = "teams"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), nullable=False, comment="Team 管理员邮箱")
    access_token_encrypted = Column(Text, nullable=False, comment="加密存储的 AT")
    id_token_encrypted = Column(Text, comment="加密存储的 ID Token")
    refresh_token_encrypted = Column(Text, comment="加密存储的 RT")
    session_token_encrypted = Column(Text, comment="加密存储的 Session Token")
    client_id = Column(String(100), comment="OAuth Client ID")
    encryption_key_id = Column(String(50), comment="加密密钥 ID")
    account_id = Column(String(100), comment="当前使用的 account-id")
    sub2api_account_id = Column(Integer, comment="Sub2API 账号 ID")
    team_name = Column(String(255), comment="Team 名称")
    plan_type = Column(String(50), comment="计划类型")
    subscription_plan = Column(String(100), comment="订阅计划")
    expires_at = Column(DateTime, comment="订阅到期时间")
    current_members = Column(Integer, default=0, comment="本地占用：已加入 + 待接受邀请")
    max_members = Column(Integer, default=6, comment="本地操作上限，不是上游订阅容量")
    status = Column(String(20), default="active", comment="状态: active/full/expired/error/banned")
    account_role = Column(String(50), comment="账号角色: account-owner/standard-user 等")
    device_code_auth_enabled = Column(Boolean, default=False, comment="是否开启设备代码身份验证")
    warranty_seat_enabled = Column(Boolean, default=False, comment="是否作为质保兑换码分流目标 Team")
    error_count = Column(Integer, default=0, comment="连续报错次数")
    last_sync = Column(DateTime, comment="最后同步时间")
    created_at = Column(DateTime, default=get_now, comment="创建时间")
    pool_type = Column(String(20), default="normal", comment="池类型: normal/welfare")
    proxy = Column(String(500), comment="母号专属静态 ISP 代理")
    seat_cycle_days = Column(Integer, default=7, comment="子号轮转天数")
    rotation_manual_count = Column(Integer, comment="今日轮转次数手调值")
    rotation_manual_on = Column(String(10), comment="手调轮转次数的日期 YYYY-MM-DD")

    # 关系
    team_accounts = relationship("TeamAccount", back_populates="team", cascade="all, delete-orphan")
    redemption_records = relationship("RedemptionRecord", back_populates="team", cascade="all, delete-orphan")
    email_mappings = relationship("TeamEmailMapping", back_populates="team", cascade="all, delete-orphan")
    child_accounts = relationship("ChildAccount", back_populates="current_team")

    # 索引
    __table_args__ = (
        Index("idx_status", "status"),
    )


class TeamAccount(Base):
    """Team Account 关联表"""
    __tablename__ = "team_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    account_id = Column(String(100), nullable=False, comment="Account ID")
    account_name = Column(String(255), comment="Account 名称")
    is_primary = Column(Boolean, default=False, comment="是否为主 Account")
    created_at = Column(DateTime, default=get_now, comment="创建时间")

    # 关系
    team = relationship("Team", back_populates="team_accounts")

    # 唯一约束
    __table_args__ = (
        Index("idx_team_account", "team_id", "account_id", unique=True),
    )


class TeamEmailMapping(Base):
    """Team 与邮箱关系映射表"""
    __tablename__ = "team_email_mappings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    email = Column(String(255), nullable=False, comment="成员邮箱(统一存小写)")
    status = Column(String(20), default="invited", nullable=False, comment="状态: invited/joined/removed")
    source = Column(String(20), default="sync", nullable=False, comment="来源: redeem/admin_add/sync/api")
    is_admin_invited = Column(
        Boolean,
        default=False,
        nullable=False,
        comment="是否由后台管理员手工邀请（永久标记，自动同步流程不会覆盖）",
    )
    last_seen_at = Column(DateTime, default=get_now, comment="最后一次确认该状态的时间")
    missing_sync_count = Column(Integer, default=0, nullable=False, comment="连续同步缺失次数")
    created_at = Column(DateTime, default=get_now, comment="创建时间")
    updated_at = Column(DateTime, default=get_now, onupdate=get_now, comment="更新时间")
    child_account_id = Column(Integer, ForeignKey("child_accounts.id"), comment="关联子号 ID")
    joined_at = Column(DateTime, comment="本轮入组时间")
    kicked_at = Column(DateTime, comment="本轮踢出时间")
    cycle_days = Column(Integer, default=7, comment="本轮轮转天数")

    # 关系
    team = relationship("Team", back_populates="email_mappings")
    child_account = relationship("ChildAccount", back_populates="team_mappings")

    # 索引
    __table_args__ = (
        Index("idx_team_email_unique", "team_id", "email", unique=True),
        Index("idx_team_email_email", "email"),
        Index("idx_team_email_status", "team_id", "status"),
    )


class RedemptionCode(Base):
    """兑换码表"""
    __tablename__ = "redemption_codes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(32), unique=True, nullable=False, comment="兑换码")
    status = Column(String(20), default="unused", comment="状态: unused/used/expired/warranty_active")
    created_at = Column(DateTime, default=get_now, comment="创建时间")
    expires_at = Column(DateTime, comment="过期时间")
    used_by_email = Column(String(255), comment="使用者邮箱")
    used_team_id = Column(Integer, ForeignKey("teams.id"), comment="使用的 Team ID")
    used_at = Column(DateTime, comment="使用时间")
    has_warranty = Column(Boolean, default=False, comment="是否为质保兑换码")
    warranty_days = Column(Integer, default=30, comment="质保时长(天)")
    extension_days = Column(Integer, default=0, comment="人工续期累计天数")
    warranty_expires_at = Column(DateTime, comment="质保到期时间(首次使用后根据质保时长计算)")
    pool_type = Column(String(20), default="normal", comment="兑换池类型: normal/welfare")
    reusable_by_seat = Column(Boolean, default=False, comment="是否可按席位重复使用")

    # 关系
    redemption_records = relationship("RedemptionRecord", back_populates="redemption_code")
    renewal_requests = relationship("RenewalRequest", back_populates="redemption_code")

    # 索引
    __table_args__ = (
        Index("idx_code_status", "code", "status"),
    )


class RedemptionRecord(Base):
    """使用记录表"""
    __tablename__ = "redemption_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), nullable=False, comment="用户邮箱")
    code = Column(String(32), ForeignKey("redemption_codes.code"), nullable=False, comment="兑换码")
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=False, comment="Team ID")
    account_id = Column(String(100), nullable=False, comment="Account ID")
    redeemed_at = Column(DateTime, default=get_now, comment="兑换时间")
    is_warranty_redemption = Column(Boolean, default=False, comment="是否为质保兑换")

    # 关系
    team = relationship("Team", back_populates="redemption_records")
    redemption_code = relationship("RedemptionCode", back_populates="redemption_records")

    # 索引
    __table_args__ = (
        Index("idx_email", "email"),
    )


class RenewalRequest(Base):
    """兑换码续期请求表"""
    __tablename__ = "renewal_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), nullable=False, comment="申请续期的用户邮箱")
    # code 允许为空：当兑换码被销毁时，extended/ignored 历史记录保留作为审计证据，
    # 此时 FK 指向已不存在的码会失败，因此销毁兑换码会把这些行的 code 置 NULL，
    # 原始码值会同步追加到 admin_note 中保证可追溯。
    code = Column(String(32), ForeignKey("redemption_codes.code"), nullable=True, comment="兑换码")
    team_id = Column(Integer, ForeignKey("teams.id"), comment="申请时关联的 Team ID")
    status = Column(String(20), default="pending", nullable=False, comment="状态: pending/extended/ignored")
    requested_at = Column(DateTime, default=get_now, comment="申请时间")
    handled_at = Column(DateTime, comment="处理时间")
    extension_days = Column(Integer, comment="管理员批准的续期天数")
    admin_note = Column(Text, comment="管理员备注")

    # 关系
    redemption_code = relationship("RedemptionCode", back_populates="renewal_requests")

    __table_args__ = (
        Index("idx_renewal_request_status", "status"),
        Index("idx_renewal_request_email", "email"),
        Index("idx_renewal_request_code", "code"),
    )


class Setting(Base):
    """系统设置表"""
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(100), unique=True, nullable=False, comment="配置项名称")
    value = Column(Text, comment="配置项值")
    description = Column(String(255), comment="配置项描述")
    created_at = Column(DateTime, default=get_now, comment="创建时间")
    updated_at = Column(DateTime, default=get_now, onupdate=get_now, comment="更新时间")

    # 索引
    __table_args__ = (
        Index("idx_key", "key"),
    )


class ChildAccount(Base):
    """子号资产库。踢人只改状态，不删除记录。"""
    __tablename__ = "child_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=False, comment="子号邮箱(统一存小写)")
    password_encrypted = Column(Text, comment="加密存储的登录密码")
    mail_raw = Column(Text, comment="原始邮箱行，含 pickup / Graph 凭证")
    phone = Column(String(50), comment="最近使用的手机号")
    sms_url = Column(Text, comment="最近使用的接码 URL")
    proxy = Column(String(500), comment="子号专属静态 ISP 代理")
    status = Column(
        String(20),
        default="unused",
        nullable=False,
        comment="状态: unused/invited/active/standby/disabled/deleted",
    )
    current_team_id = Column(Integer, ForeignKey("teams.id"), comment="当前所在 Team")
    last_team_id = Column(Integer, comment="上一轮 Team")
    joined_at = Column(DateTime, comment="当前轮入组时间")
    kicked_at = Column(DateTime, comment="最近踢出时间")
    next_eligible_at = Column(DateTime, comment="周限满下架后，官方7日额度允许再拉回的时间")
    cycle_days = Column(Integer, default=7, comment="当前轮转天数")
    access_token_encrypted = Column(Text, comment="加密存储的 AT")
    refresh_token_encrypted = Column(Text, comment="加密存储的 RT")
    session_token_encrypted = Column(Text, comment="加密存储的 Session Token")
    id_token_encrypted = Column(Text, comment="加密存储的 ID Token")
    client_id = Column(String(100), comment="OAuth Client ID")
    account_id = Column(String(100), comment="ChatGPT account-id")
    sub2api_account_id = Column(Integer, comment="Sub2API 账号 ID")
    last_error = Column(Text, comment="最近一次拉人/踢人错误")
    last_stage = Column(String(40), comment="最近一次拉人阶段")
    last_job_id = Column(String(32), comment="最近一次拉人任务 ID")
    probe_status = Column(String(20), comment="最近探测: 200/401/403/phone/none")
    probe_label = Column(String(40), comment="最近探测展示")
    probed_at = Column(DateTime, comment="最近探测时间")
    created_at = Column(DateTime, default=get_now, comment="创建时间")
    updated_at = Column(DateTime, default=get_now, onupdate=get_now, comment="更新时间")

    current_team = relationship("Team", back_populates="child_accounts")
    team_mappings = relationship("TeamEmailMapping", back_populates="child_account")
    events = relationship("SeatEvent", back_populates="child_account", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_child_status", "status"),
        Index("idx_child_team", "current_team_id", "status"),
    )


class SeatEvent(Base):
    """席位动作审计。"""
    __tablename__ = "seat_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    child_account_id = Column(Integer, ForeignKey("child_accounts.id", ondelete="SET NULL"))
    team_id = Column(Integer, ForeignKey("teams.id", ondelete="SET NULL"))
    email = Column(String(255), nullable=False, comment="当时邮箱")
    action = Column(String(40), nullable=False, comment="invite/register/reinvite/kick/rotate/push/delete")
    success = Column(Boolean, default=True, nullable=False)
    detail = Column(Text, comment="结果或错误")
    created_at = Column(DateTime, default=get_now, comment="创建时间")

    child_account = relationship("ChildAccount", back_populates="events")

    __table_args__ = (
        Index("idx_seat_event_email", "email"),
        Index("idx_seat_event_team", "team_id", "created_at"),
    )


class SeatVacancyEvent(Base):
    """踢人响应里的 policy_notice / billing_notice 历史。只当证据，不驱动自动补位。"""
    __tablename__ = "seat_vacancy_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    account_id = Column(String(100), comment="ChatGPT account-id")
    user_id = Column(String(100), comment="被踢成员 user-xxx")
    email = Column(String(255), comment="被踢成员邮箱")
    policy_notice_null = Column(Boolean, default=False, nullable=False, comment="policy_notice 是否为 null")
    vacancy_ordinal = Column(Integer, comment="vacancy_ordinal")
    free_vacancy_threshold = Column(Integer, comment="free_vacancy_threshold")
    billing_starts_at = Column(DateTime, comment="席位开始时间(本地)")
    expires_at = Column(DateTime, comment="席位释放时间(本地)")
    is_free = Column(Boolean, comment="ordinal < threshold；缺失视为未知")
    has_billing_notice = Column(Boolean, default=False, nullable=False, comment="是否带回执账单")
    policy_kind = Column(String(80), comment="policy_notice.kind")
    billed_seat_delta = Column(Integer, comment="policy_notice.billed_seat_delta")
    replacement_required = Column(Boolean, comment="policy_notice.replacement_required")
    policy_notice_json = Column(Text, comment="policy_notice 原文")
    billing_notice_json = Column(Text, comment="billing_notice 原文")
    captured_at = Column(DateTime, default=get_now, nullable=False, comment="记录时间")

    __table_args__ = (
        Index("idx_vacancy_team_captured", "team_id", "captured_at"),
    )


class Sub2ApiUsageLedger(Base):
    """Sub2API 7日窗口消费的本地累计账本。窗口回零或下滑时保留历史。"""
    __tablename__ = "sub2api_usage_ledgers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ledger_key = Column(String(255), unique=True, nullable=False, comment="email:xxx 或 id:123")
    email = Column(String(255), comment="账号邮箱")
    sub2api_account_id = Column(Integer, comment="最近一次 Sub2API 账号 ID")
    family = Column(String(255), comment="分组名")
    last_account_cost = Column(Float, comment="上次见到的 7日账号消费")
    last_user_cost = Column(Float, comment="上次见到的 7日倍率消费")
    lifetime_account_cost = Column(Float, default=0, comment="累计账号消费")
    lifetime_user_cost = Column(Float, default=0, comment="累计倍率消费")
    created_at = Column(DateTime, default=get_now, comment="创建时间")
    updated_at = Column(DateTime, default=get_now, onupdate=get_now, comment="更新时间")

    __table_args__ = (
        Index("idx_sub2api_ledger_email", "email"),
    )


class Sub2ApiUsageProbe(Base):
    """Sub2API 错峰 usage 探测进度。母号和子号都记这里，不把母号塞进 child 表。"""
    __tablename__ = "sub2api_usage_probes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sub2api_account_id = Column(Integer, unique=True, nullable=False, comment="Sub2API 账号 ID")
    email = Column(String(255), comment="账号邮箱")
    next_probe_at = Column(DateTime, nullable=False, comment="下次探测时间")
    fail_count = Column(Integer, default=0, nullable=False, comment="连续失败次数")
    last_kind = Column(String(20), comment="最近一次健康标签")
    last_label = Column(String(40), comment="最近一次展示标签")
    last_error = Column(Text, comment="最近一次失败说明")
    last_probed_at = Column(DateTime, comment="最近一次探测时间")
    next_reauth_at = Column(DateTime, comment="下次自动重授权时间")
    reauth_fail_count = Column(Integer, default=0, nullable=False, comment="连续重授权失败次数")
    last_reauth_code = Column(String(40), comment="最近一次重授权错误码")
    next_rotate_at = Column(DateTime, comment="下次封禁/周限踢拉时间")
    rotate_fail_count = Column(Integer, default=0, nullable=False, comment="连续踢拉失败次数")
    last_rotate_code = Column(String(40), comment="最近一次踢拉错误码")
    created_at = Column(DateTime, default=get_now, comment="创建时间")
    updated_at = Column(DateTime, default=get_now, onupdate=get_now, comment="更新时间")

    __table_args__ = (
        Index("idx_usage_probe_next", "next_probe_at"),
        Index("idx_usage_probe_email", "email"),
    )


class HmeAliasLease(Base):
    """HME 别名领取租约。未过期或 signup_started/consumed 都视为占用。"""
    __tablename__ = "hme_alias_leases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=False, comment="领取到的别名")
    anonymous_id = Column(String(255), nullable=False, comment="HME anonymousId")
    account_id = Column(String(100), nullable=False, comment="HME 账号 ID")
    job_id = Column(String(32), comment="拉人任务 public_id")
    operation_id = Column(Integer, comment="operations.id")
    purpose = Column(String(40), comment="onboard/free/rotate")
    team_id = Column(Integer, comment="拉人时的 Team ID")
    local_state = Column(String(20), default="reserved", nullable=False, comment="reserved/signup_started/consumed/quarantined/manual_review")
    label_desired = Column(String(255), comment="打算写入 HME 的本地标签")
    label_sync_pending = Column(Boolean, default=False, nullable=False, comment="打标失败，待后台重试")
    last_error = Column(Text, comment="最近一次打标/对账错误")
    heartbeat_at = Column(DateTime, comment="任务心跳，signup_started 后忽略过期")
    expires_at = Column(DateTime, nullable=False, comment="reserved 状态的租约过期时间")
    created_at = Column(DateTime, default=get_now, comment="创建时间")
    updated_at = Column(DateTime, default=get_now, onupdate=get_now, comment="更新时间")

    __table_args__ = (
        Index("idx_hme_lease_expires", "expires_at"),
        Index("idx_hme_lease_job", "job_id"),
        Index("idx_hme_lease_state", "local_state"),
        Index("idx_hme_lease_operation", "operation_id"),
    )


class PhonePool(Base):
    """本地接码号码池。成功绑定才计数，领取只写租约。"""
    __tablename__ = "phone_pool"

    id = Column(Integer, primary_key=True, autoincrement=True)
    number = Column(String(32), unique=True, nullable=False, comment="E.164 号码")
    sms_url = Column(Text, nullable=False, comment="接码 URL")
    status = Column(String(20), default="active", nullable=False, comment="active/maxed/disabled/risk")
    used_count = Column(Integer, default=0, nullable=False, comment="成功或作废次数")
    max_uses = Column(Integer, comment="单号次数上限，空则用系统默认")
    last_used_at = Column(DateTime, comment="最近一次占用/成功/冷却起点")
    last_success_at = Column(DateTime, comment="最近一次 OpenAI 接受短信")
    last_error = Column(Text, comment="最近一次失败说明")
    last_error_type = Column(String(40), comment="invalid/recently_used/risk/no_sms")
    reserved_by = Column(String(64), comment="占用该号的 operation public_id")
    reserved_at = Column(DateTime, comment="租约开始时间")
    lease_heartbeat_at = Column(DateTime, comment="租约心跳，任务运行中续租")
    operation_id = Column(Integer, comment="当前占用的 operations.id")
    risk_count = Column(Integer, default=0, nullable=False, comment="累计 risk 次数")
    no_sms_streak = Column(Integer, default=0, nullable=False, comment="连续收不到短信次数")
    note = Column(Text, comment="备注")
    created_at = Column(DateTime, default=get_now, comment="创建时间")
    updated_at = Column(DateTime, default=get_now, onupdate=get_now, comment="更新时间")

    __table_args__ = (
        Index("idx_phone_pool_status", "status"),
        Index("idx_phone_pool_reserved", "reserved_by"),
        Index("idx_phone_pool_heartbeat", "lease_heartbeat_at"),
    )


class PhoneAttempt(Base):
    """号码使用历史。成功才计入 used_count，本表只记账。"""
    __tablename__ = "phone_attempts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    phone_id = Column(Integer, ForeignKey("phone_pool.id"), nullable=False)
    operation_id = Column(Integer, comment="operations.id")
    operation_public_id = Column(String(32), comment="operations.public_id")
    account_id = Column(Integer, comment="本地 accounts.id")
    purpose = Column(String(20), default="signup", nullable=False, comment="signup/reauth")
    result = Column(String(40), nullable=False, comment="success/recently_used/invalid/risk/no_sms/cancelled/provider_error")
    provider_message = Column(Text, comment="OpenAI / 接码回执")
    started_at = Column(DateTime, comment="领取时间")
    finished_at = Column(DateTime, comment="记结果时间")
    created_at = Column(DateTime, default=get_now, comment="创建时间")

    __table_args__ = (
        Index("idx_phone_attempts_phone", "phone_id", "finished_at"),
        Index("idx_phone_attempts_operation", "operation_public_id"),
    )


class ProxyProfile(Base):
    """静态 ISP 档案。任务创建时 freeze，不跟页面字符串走。"""
    __tablename__ = "proxy_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), comment="展示名")
    scheme = Column(String(20), nullable=False, comment="http/https/socks5/socks5h")
    host = Column(String(255), nullable=False)
    port = Column(Integer, nullable=False)
    username_encrypted = Column(Text)
    password_encrypted = Column(Text)
    url_fingerprint = Column(String(64), unique=True, nullable=False, comment="规范化 URL 的 sha256")
    region = Column(String(80))
    status = Column(String(20), default="active", nullable=False, comment="active/disabled")
    last_exit_ip = Column(String(64))
    last_checked_at = Column(DateTime)
    failure_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=get_now)
    updated_at = Column(DateTime, default=get_now, onupdate=get_now)

    __table_args__ = (
        Index("idx_proxy_profiles_status", "status"),
        Index("idx_proxy_profiles_host", "host", "port"),
    )


class Account(Base):
    """本地登录账号。官方计划 / 本地用途 / 工作区角色必须拆开，互不推断。"""
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=False, comment="登录邮箱(统一存小写)")
    official_plan = Column(String(20), default="unknown", nullable=False, comment="free/plus/pro/team/business/unknown")
    official_user_id = Column(String(100), comment="官方 user-xxx，不是 Workspace UUID")
    official_account_id = Column(String(100), comment="官方个人 account id，不是 Workspace UUID")
    auth_state = Column(String(30), default="unknown", nullable=False, comment="healthy/refresh_due/oauth_required/manual_required/unknown")
    operational_state = Column(String(20), default="available", nullable=False, comment="available/active/standby/disabled/archived/unused/free")
    local_purpose = Column(String(20), nullable=False, comment="mother/child/standby/free/disabled")
    proxy = Column(String(500), comment="账号专属静态 ISP 字符串，兼容旧数据")
    proxy_profile_id = Column(Integer, ForeignKey("proxy_profiles.id"), comment="规范化代理档案")
    access_token_encrypted = Column(Text, comment="加密存储的 AT")
    refresh_token_encrypted = Column(Text, comment="加密存储的 RT")
    session_token_encrypted = Column(Text, comment="加密存储的 Session Token")
    id_token_encrypted = Column(Text, comment="加密存储的 ID Token")
    client_id = Column(String(100), comment="OAuth Client ID")
    next_eligible_at = Column(DateTime, comment="周限满后允许再拉回的时间")
    quota_slot_minute = Column(Integer, comment="官方额度错峰分钟槽 0-59")
    next_quota_probe_at = Column(DateTime, comment="下次官方额度探测时间")
    quota_probe_fail_count = Column(Integer, default=0, nullable=False, comment="官方额度连续失败次数")
    source_team_id = Column(Integer, comment="回填来源 teams.id")
    source_child_account_id = Column(Integer, comment="回填来源 child_accounts.id")
    created_at = Column(DateTime, default=get_now, comment="创建时间")
    updated_at = Column(DateTime, default=get_now, onupdate=get_now, comment="更新时间")
    version = Column(Integer, default=1, nullable=False, comment="乐观锁版本")

    owned_workspaces = relationship("Workspace", back_populates="owner_account")
    memberships = relationship("WorkspaceMembership", back_populates="account")
    external_bindings = relationship("ExternalBinding", back_populates="account")
    quota_snapshots = relationship("QuotaSnapshot", back_populates="account")

    __table_args__ = (
        Index("idx_accounts_purpose_state", "local_purpose", "operational_state"),
        Index("idx_accounts_source_team", "source_team_id"),
        Index("idx_accounts_source_child", "source_child_account_id"),
        Index("idx_accounts_next_quota_probe", "next_quota_probe_at"),
        Index("idx_accounts_proxy_profile", "proxy_profile_id"),
    )


class Workspace(Base):
    """真实 Team / Workspace。official_workspace_id 只能是 UUID，不能是 user-xxx。"""
    __tablename__ = "workspaces"

    id = Column(Integer, primary_key=True, autoincrement=True)
    official_workspace_id = Column(String(100), comment="官方 Workspace UUID")
    name = Column(String(255), comment="Workspace 名称")
    subscription_plan = Column(String(100), comment="官方订阅缓存，不是本地用途")
    owner_account_id = Column(Integer, ForeignKey("accounts.id"), comment="本地母号 Account")
    status = Column(String(20), default="active", nullable=False, comment="active/expired/error/unknown")
    seat_limit = Column(Integer, comment="本地操作上限缓存")
    last_official_sync_at = Column(DateTime, comment="最近一次官方成员同步")
    source_team_id = Column(Integer, unique=True, comment="回填来源 teams.id")
    created_at = Column(DateTime, default=get_now, comment="创建时间")
    updated_at = Column(DateTime, default=get_now, onupdate=get_now, comment="更新时间")
    version = Column(Integer, default=1, nullable=False, comment="乐观锁版本")

    owner_account = relationship("Account", back_populates="owned_workspaces")
    memberships = relationship("WorkspaceMembership", back_populates="workspace")

    __table_args__ = (
        Index("idx_workspaces_official_id", "official_workspace_id"),
        Index("idx_workspaces_owner", "owner_account_id"),
    )


class WorkspaceMembership(Base):
    """Account 与 Workspace 的关系。owner 行来自 Workspace.owner，不从 mapping 猜。"""
    __tablename__ = "workspace_memberships"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    official_user_id = Column(String(100), comment="官方 user-xxx")
    official_role = Column(String(20), default="unknown", nullable=False, comment="owner/admin/member/unknown")
    membership_state = Column(String(20), default="unknown", nullable=False, comment="invited/joined/removed/unknown")
    local_purpose = Column(String(20), nullable=False, comment="mother/child")
    joined_at = Column(DateTime, comment="本轮入组时间")
    removed_at = Column(DateTime, comment="本轮踢出时间")
    source_mapping_id = Column(Integer, comment="回填来源 team_email_mappings.id")
    created_at = Column(DateTime, default=get_now, comment="创建时间")
    updated_at = Column(DateTime, default=get_now, onupdate=get_now, comment="更新时间")

    workspace = relationship("Workspace", back_populates="memberships")
    account = relationship("Account", back_populates="memberships")

    __table_args__ = (
        UniqueConstraint("workspace_id", "account_id", name="uq_workspace_membership_account"),
        Index("idx_membership_workspace_state", "workspace_id", "membership_state"),
        Index("idx_membership_account", "account_id"),
    )


class ExternalBinding(Base):
    """本地账号与远端系统的显式绑定。一个 remote id 只能绑一个本地账号。"""
    __tablename__ = "external_bindings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String(40), nullable=False, comment="第一阶段仅 sub2api")
    local_account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    remote_account_id = Column(String(100), nullable=False, comment="远端账号 ID")
    binding_state = Column(String(20), default="pending", nullable=False, comment="pending/verified/conflict/missing/orphaned")
    verified_email = Column(String(255), comment="交叉验证通过的邮箱")
    verified_official_account_id = Column(String(100), comment="交叉验证通过的官方 account id")
    verified_workspace_id = Column(String(100), comment="交叉验证通过的 Workspace UUID")
    last_observed_at = Column(DateTime, comment="最近一次观察到远端")
    last_error = Column(Text, comment="最近一次绑定错误")
    created_at = Column(DateTime, default=get_now, comment="创建时间")
    updated_at = Column(DateTime, default=get_now, onupdate=get_now, comment="更新时间")

    account = relationship("Account", back_populates="external_bindings")

    __table_args__ = (
        UniqueConstraint("provider", "remote_account_id", name="uq_external_binding_remote"),
        UniqueConstraint("provider", "local_account_id", name="uq_external_binding_local"),
        Index("idx_external_binding_state", "provider", "binding_state"),
    )


class QuotaSnapshot(Base):
    """官方额度快照。只有 source=official 且 success 才允许驱动以后的踢拉。"""
    __tablename__ = "quota_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    five_hour_used_percent = Column(Integer, comment="5 小时窗口已用百分比")
    five_hour_reset_at = Column(DateTime, comment="5 小时窗口重置时间")
    seven_day_used_percent = Column(Integer, comment="7 日窗口已用百分比")
    seven_day_reset_at = Column(DateTime, comment="7 日窗口重置时间")
    source = Column(String(20), default="official", nullable=False, comment="official/sub2api")
    queried_at = Column(DateTime, nullable=False, comment="查询时间")
    success = Column(Boolean, default=False, nullable=False, comment="本次官方查询是否成功")
    error_code = Column(String(40), comment="失败码")
    error_message = Column(Text, comment="失败说明")
    created_at = Column(DateTime, default=get_now, comment="创建时间")

    account = relationship("Account", back_populates="quota_snapshots")

    __table_args__ = (
        Index("idx_quota_snapshots_account_queried", "account_id", "queried_at"),
        Index("idx_quota_snapshots_success", "account_id", "success", "queried_at"),
    )


class Operation(Base):
    """长任务落库。进程重启后靠 public_id / lease 回收，不活在内存 _JOBS。"""
    __tablename__ = "operations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    public_id = Column(String(32), unique=True, nullable=False, comment="对外 job_id")
    op_type = Column("type", String(40), nullable=False, comment="onboard/reauth/rotate/free_register/reregister")
    entity_type = Column(String(40), comment="team/child/account")
    entity_id = Column(Integer)
    workspace_id = Column(Integer, comment="本地 teams.id / workspaces.source_team_id")
    account_id = Column(Integer)
    email = Column(String(255))
    phone = Column(String(64))
    resolved_proxy = Column(String(500), comment="任务创建时冻结的代理 URL")
    resolved_proxy_profile_id = Column(Integer, comment="任务创建时冻结的 proxy_profiles.id")
    state = Column(String(20), nullable=False, default="queued", comment="queued/running/waiting/success/failed/cancelled/manual_required")
    current_step = Column(String(40))
    idempotency_key = Column(String(120))
    locked_by = Column(String(80))
    lease_expires_at = Column(DateTime)
    cancel_requested = Column(Boolean, default=False, nullable=False)
    input_json = Column(Text, comment="续跑用入参，敏感字段已加密")
    result_json = Column(Text)
    error_code = Column(String(40))
    error_message = Column(Text)
    log_json = Column(Text)
    created_at = Column(DateTime, default=get_now)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)
    updated_at = Column(DateTime, default=get_now, onupdate=get_now)

    steps = relationship("OperationStep", back_populates="operation", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_operations_state_lease", "state", "lease_expires_at"),
        Index("idx_operations_email_created", "email", "created_at"),
        Index("idx_operations_type_state", "type", "state"),
    )


class OperationStep(Base):
    """长任务步骤。rotate 的 kicked 成功后重启不得再踢。"""
    __tablename__ = "operation_steps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    operation_id = Column(Integer, ForeignKey("operations.id", ondelete="CASCADE"), nullable=False)
    step_name = Column(String(40), nullable=False)
    state = Column(String(20), nullable=False, default="queued")
    attempt = Column(Integer, default=1, nullable=False)
    input_snapshot = Column(Text)
    result_snapshot = Column(Text)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)
    error_code = Column(String(40))
    error_message = Column(Text)

    operation = relationship("Operation", back_populates="steps")

    __table_args__ = (
        UniqueConstraint("operation_id", "step_name", name="uq_operation_step_name"),
        Index("idx_operation_steps_op", "operation_id", "state"),
    )
