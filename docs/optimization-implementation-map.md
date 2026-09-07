# Optimization implementation map

## Baseline and evidence gates

- Team48 baseline: `c5f52cbdbcf253be0712e6f3545d1935c6dc825c`; initial worktree clean.
- Deployed Sub2API version/branch: **unverified**. No SSH, production credentials, remote writes or deployment authorized/performed.
- Standard/Premium official response fixtures: **not supplied**. Existing member adapter accepts `seat_type`, `seatType`, `seat`, including legacy `member`; these are not verified Business tier evidence.
- Therefore this increment implements scoped durable sync and local runtime status. Tier normalization and Sub2API plan writes remain gated, not inferred from roles, names, email or local purpose.

## Verified entry points

| Surface | Actual path and symbols | Change / tests |
| --- | --- | --- |
| Member sync UI | `app/web/static/js/app.js`: `entityActions.workspace`, `bootWorkspaces`; `app/web/static/js/accounts-view.js`: `boot`, team rendering; `app/web/templates/console.html` | Independent team controls and explicit batch endpoint; persistent queued/running state, retained partial errors and shared visibility-aware polling implemented. `tests/management_ui.test.cjs`, browser checks |
| HTTP sync | `app/web/routes/api.py`: `build_api_router.sync_workspace` | Now returns durable enqueue with 202 instead of awaiting provider work in the request. `tests/test_workspace_sync_scope.py` |
| Execution | `app/application/workspace_sync.py`: `WorkspaceSyncService.sync_workspace`, `_counts_from_fetch` | Reuse complete member+invite validation and snapshot commit; accept existing Operation rather than create nested jobs. Scope/concurrency/failure tests |
| Official read | `app/application/workspaces.py`: `WorkspaceService.get_members`, `get_invites`, `ensure_access_token`, `_workspace_account_id`; `app/integrations/openai/chatgpt.py`: `get_members`, `get_invites` | Owner credential, explicit official workspace ID, existing collection pagination; avoid token refresh side effects in new read-only sync path. `tests/test_workspace_sync.py`, `tests/test_official_member_logic.py` |
| Membership | `app/persistence/models/identity.py`: `WorkspaceMembership`, `WorkspaceOfficialMemberSnapshot`; `app/application/workspace_sync.py`: `sync_workspace` | Local account/workspace relationship separate from local purpose. Existing official snapshot is workspace/email keyed; do not promote it to verified subscription identity. Existing sync tests |
| Operations | `app/application/operations.py`: `OperationStore.create`, `finish`, `recover_stale`, `active_for_workspace`, `claim_next_reauth`; `app/application/jobs/dispatcher.py`: `ReauthDispatcher` | Persistent operations and leases exist; no generic runner. Use existing scheduler for HTTP sync dispatch, persistent active key for dedup; no second scheduler/browser runner. Scope/recovery tests |
| Scheduler | `app/application/jobs/scheduler.py`: `configure_jobs`, `start_scheduler`, `dispatch_quota_queue`; `app/main.py`: `create_app.lifespan` | Actual jobs: quota scan/dispatch, token refresh, auto reauth, auto rotate, Sub2API usage. No periodic member sync. Add queue dispatch only; report real next_run_time. Runtime tests |
| Token vs reauth | `app/application/tokens.py`: `AuthService.load_settings`, `refresh_account`, `run_probe_once`; `app/application/reauth.py`: `ReauthService.load_settings`, `run_once`, `run_job`; `app/application/jobs/browser.py`: `lock`, `run_reauth_isolated` | Token refresh HTTP vs interactive browser authorization; policies queried without executing either. Runtime tests |
| Sub2API | `app/application/sub2api_publish.py`: `_build_credentials`, `account_sub2api_push`, `push_refreshed_tokens_to_bound_sub2api`; `app/integrations/sub2api/client.py`: `create_account`, `update_account`, `read_after_write` | Existing credentials builder omits plan_type; existing push verifies identity after write. No new plan/extra writes without deployed merge/concurrency/shadow contract. `tests/test_sub2api_management.py` |
| Read models | `app/application/queries/console.py`: `overview`; `app/application/queries/portfolio.py`: `portfolio_query`; `app/application/queries/identity.py`: `workspaces_query` | New local-only runtime DTO; portfolio carries scoped active sync. Unknown tier remains unknown. Runtime/security tests |
| UI files | `app/web/templates/console.html`, `app/web/static/js/app.js`, `accounts-view.js`, `polling.js`, `runtime-status.js`, `app/web/static/css/management.css`, `runtime.css` | Existing Jinja/vanilla JS retained. Failed UI handoff produced no writes; parent implemented and browser-verified UI. |
| Schema | `app/persistence/migrations/bootstrap.py`: `bootstrap_schema`, `_ensure_sqlite_columns`; `app/persistence/models/operations.py`: `Operation` | Two idempotent SQLite index additions, no new subscription tables; tests use isolated SQLite and `Base.metadata.create_all` / `bootstrap_schema`. Migration tests |

## Delivery boundaries

No production database, HME/Apple writes, Sub2API containers, seat purchases, member removals, automatic reauthorization/rotation enablement, deployment or Git commits. Final verification: 269 Python tests, 28 Node tests and both isolated browser scripts passed. See `runtime-and-member-lifecycle.md` for contracts, screenshots, limitations and rollback.
