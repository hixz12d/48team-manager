# Sub2API plan contract gate

## Current status

Deployed version, commit, branch and shadow-account rules: **not verified**. No production access or write was performed for this increment. The blueprint's references to upstream main do not establish a contract for the deployed instance.

Existing Team48 `_build_credentials` in `app/application/sub2api_publish.py` supplies credentials but no `plan_type`. `account_sub2api_push` uses POST/PUT `/api/v1/admin/accounts` through the existing client and verifies identity after write. `push_refreshed_tokens_to_bound_sub2api` checks the existing binding before and after token updates. These existing behaviors are not expanded by this increment.

## Gate decision

- New plan-only and `extra.team48.subscription` writes are **not implemented/enabled** without a verified transport contract.
- Read models report `plan_sync.status=skipped`, `reason=contract_unverified`.
- No use of `unknown`, `Business Premium`, `business_standard` or `business_premium` as credential plan values.
- No inference of credential plan from workspace role, account local purpose, email, quota or subscription display labels.
- Existing `seat_type` snapshots remain legacy raw member metadata, not verified Standard/Premium evidence. New subscription DTOs report `seat_tier=unknown`, `status=unverified`, with no fabricated observation timestamp.

## Evidence needed before enabling writes

1. Deployed Sub2API version/commit, API request schema, single-account credentials and extra update semantics, supported atomic/conflict handling, and shadow restrictions.
2. Sanitized official Standard and Premium responses with exact field paths, current/pending values and stable member/workspace identity. No tokens, cookies, passwords or complete HAR in the repository.
3. Controlled contract tests for retained non-sensitive credential fields, masked-secret rejection, retained extra/configuration, verified scoped binding, write/readback, and one Sub2API OAuth refresh cycle.

Until then, the safe deliverable is unknown tier and skipped metadata push, not a plausible-looking mapping. Subscription snapshot persistence, tier mapping, pending seat changes and mixed-tier production acceptance remain deferred.
