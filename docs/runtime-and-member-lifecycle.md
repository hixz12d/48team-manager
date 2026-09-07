# Runtime and member lifecycle increment

## Interfaces

- `POST /api/workspaces/{id}/sync`: authenticated, 202, durable Operation only. Response includes `operation_id`, `status`, `workspace_id`, `target`, `reused`. No access token: 400 `credentials_missing`; missing team: 404; disabled team/owner: 409.
- `POST /api/workspaces/sync`: authenticated, 202, queues each active team through the same function. Returns `items` (each team's operation/error), `queued`, `failed`, `reused`. No parent occupies a worker waiting for children. Repeated submissions reuse active jobs.
- `GET /api/runtime/status`: authenticated, local-only white-list DTO. Active items capped at 8; recent results at 5. Counts are database-wide, independent of display limits and homepage attention items.
- `GET /api/workspaces` and `/api/accounts/portfolio`: each team includes `sync_operation` and its latest unarchived terminal `last_sync_operation`. Subscription fields explicitly remain unverified. Teams also expose `former_members`; entries include account ID, email, previous role, departure time and `can_reinvite`.
- `POST /api/workspaces/{id}/members/add`: existing lightweight invitation endpoint, body `{email, role}`. It reuses a local account and membership, does not launch browser onboarding. Existing invitations are not duplicated; joined members are not silently assigned a different role. Unknown remote lookup or disabled account/team blocks the write.

## Queue and recovery

The existing APScheduler now dispatches workspace-sync Operations every 2 seconds. This is a queue dispatcher, **not periodic member synchronization**. Official member and invitation reads reuse the existing paginated client with the selected workspace ID and owner access token. The new queue path does not refresh tokens, start OAuth/browser flows, push Sub2API, or fetch unrelated team-name metadata. A 120-second execution bound is shorter than the 180-second lease. Network waits occur after committing task progress, not while retaining a SQLite write transaction.

A partial unique index enforces one active idempotency key per queued/running/waiting workspace sync. Existing workspace mutation guards include workspace sync. Cancelled queued jobs do not execute; an in-progress read checks cancellation before snapshot writes. Incomplete member/invitation reads retain the old complete snapshot. New read-only jobs can return to queued after restart; legacy sync jobs lacking an active key require manual resubmission instead of replay. No browser job is automatically resumed.

All-team enqueue does not create a synthetic parent; each team's real Operation holds the final result. The initial batch response reports accepted/rejected queue submissions, not finished member synchronization.

## Runtime semantics

`counts.running`, `queued`, `waiting`, `waiting_user` are mutually exclusive persisted-state buckets. `waiting_user` includes unarchived `manual_required` Operations, including older unresolved work. Item `status` can further explain `waiting_retry` and `waiting_browser`; these are refinements, not additional totals. Recent terminal history may include a manual-required operation also present in the active attention list, but it is counted only once in the state totals.

Policy `next_run_at` is the actual scheduler **scan** time, not a promise that every account will run at that instant. Disabled policies have no next time. Runtime status does not invent a periodic member-sync job. Token refresh and browser reauthorization are separate policies. A small 5-second scheduler heartbeat reports unknown/unavailable/stale rather than healthy when there is no current evidence. Browser occupancy comes from the actual process lock; a browser owner is only reported when known.

## Member lifecycle

Official removal preserves the local account and credentials. After confirmed remote absence it marks only the selected workspace relationship removed and deletes only that member's old official snapshot. Team capacity is not decremented from guessed purchased-seat data; the full snapshot is marked stale. Reinviting reuses the same account ID and transitions the relationship to invited; authentication remains a separate action.

An unverified delete/revoke does not erase local membership or snapshots. Revoking an invitation cannot silently kick a member who already accepted it. An account with another current team relationship keeps its global operational state. Manual removal does not newly pause/delete Sub2API. Existing explicitly destructive purge and automatic rotation policies remain separate.

Local detach (`members/remove`) is still a local-only unlink, not remote departure or permanent deletion. `former_members` supplies the missing path for a preserved account to be invited back. The invitation Operation now records `success` correctly instead of incorrectly treating an `ok: true` command result as failed.

## Migration and rollback

Only two compatible indexes are added: `uq_workspace_sync_active_key` and `idx_operations_archived_finished`. `bootstrap_schema` creates them idempotently on existing SQLite; no new subscription columns/tables, no networking during migration. No production migration was run.

Before deployment obtain approval and an SQLite-consistent backup. To roll back, stop accepting new sync requests, let queued/running sync tasks drain or cancel them, then restore the previous application code. Keep the added indexes. Do not delete account/membership records or undo official member actions automatically. Old code does not dispatch new queued workspace sync jobs, so do not leave a queue pending across rollback.

## Frontend

- Team headers provide independent sync controls; the toolbar explicitly says sync all teams and calls the batch endpoint. Accepted tasks show queued/running and are recovered from the server after reload. Completion/failure links to the corresponding Operation; failures preserve the previous member snapshot.
- `polling.js` owns one in-flight read and one timer per surface, coalesces explicit refreshes, aborts/pauses on hidden/pagehide, resumes on visibility/pageshow and backs off after read failures. Active work polls at 2 seconds; idle surfaces at 15 seconds. Runtime polling never invokes provider writes.
- Account refresh retains unchanged team DOM nodes, filters, collapse and scroll state. Batch submission failures remain visible across status refreshes. Runtime read failures retain the last rendered data and its timestamp.
- The homepage runtime band sits below business statistics and above existing workspace/attention sections; those attention totals are not reused as task counts. It shows current work, trigger/stage, scheduler/browser evidence, automation policies and recent results.
- Team details expose preserved departed accounts and a dedicated reinvitation form with the prior role. It uses `members/add`, preserves the selection on failure, and shows pending invitation after success. Browser onboarding, credential authorization, local unlink and permanent purge remain separate workflows.
- Business tiers are shown as unidentified without verified observations; conflicting/multiple team contexts are not merged into an account-wide tier. Subscription details show observation/source and the unverified Sub2API plan state.

## Final verification

- Full Python suite: **269 passed**, `python -m unittest discover -s tests -v` (271.751 seconds).
- JavaScript: **28 passed**, `node --test tests/*.test.cjs`, including polling lifecycle/backoff/coalescing and subscription evidence presentation.
- Python compilation and JavaScript syntax checks passed. `git diff --check` passed.
- `tests/browser_management.py`: management summary, billing, filters, navigation, menus, account detail, collapse/refresh, deep links and overflow checks passed at 1600, 1440, 1280, 1024, 768, 390px.
- `tests/browser_runtime_lifecycle.py`: selected-team endpoint only, reload restores queued state, unrelated team DOM identity retained, missing-credential errors, explicit batch endpoint and retained partial-submit errors, removal/reinvitation/retry, no onboard/reauth/Sub2API writes, runtime failure/recovery and responsive checks passed. All mutations are intercepted by Playwright; no real member changed. Runtime task screenshots use synthetic DTOs, not claims about live worker activity.
- Both browser scripts report zero page errors. A parallel rerun was interrupted when the preview process exited; restarting the isolated server and rerunning serially passed.
- Screenshots are in the local temporary `team48-browser` directory: `accounts-{width}.png`, `runtime-{width}.png`, `sync-failed-1440.png`, `reinvite-{width}.png`.
- Isolated read-only preview: `http://127.0.0.1:8019`, login `preview` / `preview-only`. Uses example identities, an example departed account, unknown Business tiers and a separate temporary database. Real account writes are blocked.
- Real Standard/Premium fixtures, deployed Sub2API contract and OAuth-cycle tests remain unexecuted. See `sub2api-plan-contract.md`. No production deployment/migration, scheduler policy enablement or Git commit was performed.
