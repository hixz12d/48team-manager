# Single-account invited OAuth signup

The one-seat replenish action and `scripts.signup_one` now use an invitation-first flow. No scheduler, automatic member removal, SMS purchase or Sub2API push is enabled by this change. The legacy registration-only paths remain separate.

Flow: validate workspace/proxy/mailbox -> resume an unfinished child or reuse eligible standby -> claim one HME only when neither exists -> send and verify the requested invitation -> read its HTTPS invitation link -> register through that link -> confirm official membership/role/seat -> OAuth login (signup disabled) -> conditionally verify SMS -> validate callback and token email -> save access/refresh tokens.

Registration does not consume SMS. During OAuth, an explicitly supplied number and HTTPS receipt URL are used only if a phone page appears. Missing SMS input stops with `phone_verification_required`. The unified flow permits at most one SMS send and one code submission, with a 90-second polling timeout; it never changes numbers automatically. An OAuth failure after joining returns `partial`, preserving the same account for retry. No successful registration is inferred merely from sending an invitation or receiving an OAuth callback.

## Prerequisites

- Use the intended Team48 runtime and database. Local backup data is not the production HME occupation store.
- Configure the Cloudflare forwarding mailbox (or an account-specific pickup URL) and the workspace owner's proxy. HME is needed only when claiming a new alias, not when resuming an existing account. Playwright and its browser must already be available.
- Explicitly select the workspace ID, role and seat intent. The command does not remove members, increase capacity, or select a different workspace.
- For Chromix, download and verify a trusted platform build separately, then pass its actual browser executable via `--browser-executable`. Do not pass the Windows `.cmd` launcher. This integration uses Playwright's executable path, not the Chromix SDK or fingerprint flags. Linux x64 startup, DOM interaction and OAuth entry were verified in the isolated trial below; full signup was blocked by phone verification.
- Existing `browser_headless` settings still apply. No dependencies or browser binaries are installed by this command.

## Run

From the repository root, with the project's Python environment:

```powershell
# No writes or network requests: displays the execution guard only.
.\.venv\Scripts\python.exe -m scripts.signup_one --workspace-id 123 --role member

# Real external effects. Replace 123 with the explicitly approved workspace ID.
.\.venv\Scripts\python.exe -m scripts.signup_one --workspace-id 123 --role member --confirm
```

Use `--seat-intent premium` only when that seat has been approved. Standard/default uses the existing seat contract. Use `--role owner` only when owner privileges have been approved.

For conditional SMS, add `--sms-stdin` and provide exactly one `+number----https://receipt-url` line on standard input. Do not place the receipt credential in command-line arguments, shell history or committed files. In the one-seat replenish API, the existing `phone_line` is the explicit per-operation SMS authorization; leaving it empty does not use the number pool or saved SMS credentials. Receipt URLs are not stored in the operation's phone field or the new account's SMS URL. Only a successfully verified phone is retained.

Invite registration and OAuth run in cancellable child processes. Stage updates and 30-second heartbeats keep the operation lease live; cancellation stops the browser worker. The existing alias lease is retained when registration has already started. Successful invitation registration finalizes the HME workspace label before attempting OAuth, so a later authorization failure cannot release that identity.

An interrupted attempt may already have created an invitation/account. Empty email first resumes a single unfinished local child in this workspace; multiple candidates require explicit selection. `--email-line alias@icloud.com` selects the account deterministically. Already-joined accounts go directly to the membership gate and OAuth, without a new invite/registration. Explicit emails do not acquire/relabel HME. A healthy account with a refresh token does not repeat OAuth. Missing invitation links never fall back to a generic signup URL.

Output contains status, operation ID, account ID and email, not tokens, proxy URLs, PKCE secrets or callback URLs. Check the operation's error code for failures. HME local-label synchronization failures retain the existing lease protection; Apple labels are never modified.

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_invitation_flow tests.test_oauth_signup tests.test_onboard tests.test_replenish tests.test_oauth_security tests.test_reauth -q
```

Tests use an in-memory database and mocked external APIs/browser. They do not establish that OpenAI will omit phone verification for a particular account, proxy or browser build. A live one-account run requires separate approval; do not deploy from this command or touch `/opt/sub2api`.

## Approved Live Trial: 2026-09-10

- Workspace: `11`, `Jupiter 1`; primary mother `hixz2611@gmail.com` was preserved.
- The user approved replacing only `adagio.funding1i@icloud.com` with one new Owner + Premium child, using an isolated Chromix installation and no SMS.
- Staging: `/opt/team48/data/experiments/oauth-signup-hixz2611-20260910`, visible in the existing container as `/app/data/experiments/oauth-signup-hixz2611-20260910`. Live application files, Compose and service processes were not replaced/restarted.
- Browser: Chromix `152.0.7977.82` Linux x64; SHA256 `85593ab5bedbdceee2196f2d1e9c603a09cf18f5dfb9bcc949c4232e7b35295f`. Playwright launched it, DOM clicks worked and the proxied OAuth login page returned HTTP 200. Screenshot attempts timed out; page HTML and metadata were available for failure diagnosis.
- Operation: `c32371f57801`. The exact old official member ID was checked against a two-person roster before removal. Removal was confirmed by an official readback showing only the mother. Old local account was retained as standby; the generic rotate path was not used because it can also pause Sub2API scheduling.
- New HME: `4-acetic.glyph@icloud.com`, local account `44`. The official pending invitation was verified as `account-owner` + `prolite` (Owner + Premium).
- Registration reached email OTP, then stopped at `https://auth.openai.com/add-phone`, title `Phone number required - OpenAI`. The page explicitly required a phone number and a one-time verification code. No SMS number was obtained or submitted.
- Result: `phone_verification_required`; OAuth session failed, no access/refresh tokens were stored for the new account, and official membership remained one mother plus one pending invitation. Do not report this as successful registration/authorization or retry the replacement runner.
- HME local label readback: `GPT已使用`; label sync is not pending and no lease remains. Apple labels were not edited. The label prevents this partially registered identity from being allocated again.
- No Sub2API writes/pushes were made. Trial browser/runner processes were confirmed stopped. The isolated directory is retained for diagnosis; no automatic cleanup or restoration was performed.
- Any continuation should reuse this same email and explicitly decide how to handle the required phone verification or the pending invitation; do not allocate more aliases to retry blindly.

## Follow-up: Invited Registration and Conditional SMS OAuth

- The user subsequently registered and joined via the invitation flow. Their screenshot showed `4-acetic.glyph@icloud.com` as Owner + Premium. This was a user-completed registration step, not a successful automatic signup from the first trial.
- The user then approved SMS only when OpenAI requires it, and supplied one specific number/receipt endpoint for this attempt. No replacement numbers or paid SMS purchases were authorized.
- `scripts/team48_resume_sms.py` ran against the same isolated Chromix installation, with signup disabled and the existing account email fixed. It verified official Owner + Premium membership before OAuth. The supplied SMS endpoint was used only at the phone verification stage.
- Operation `2184934d156f` completed successfully. Strict callback/state and token-email checks passed; access and refresh tokens were stored for account `44`. The new OAuth session is consumed, account state is `active`, auth state is `healthy`, and local membership is joined/owner.
- Final official readback: exactly two members, mother `hixz2611@gmail.com` (Owner + Standard) and child `4-acetic.glyph@icloud.com` (Owner + Premium), with zero pending invitations.
- SMS receipt credentials were supplied over SSH stdin, briefly stored encrypted in the private experiment directory, and removed on worker startup. The receipt URL was not stored in account data, source files or logs. Only the verified phone was retained on the account; do not include receipt credentials or OTPs in reports.
- No Sub2API push/write, mother removal, new alias allocation or invitation resend occurred in this follow-up. The one-time secret file is absent and all experiment processes exited.
- Agreed future behavior: invitation registration/join first, then OAuth; acquire/use SMS only when a phone verification page requires it. This experiment proves that explicit-number continuation works. It does not enable a background SMS pool or change the existing no-SMS signup CLI default.
