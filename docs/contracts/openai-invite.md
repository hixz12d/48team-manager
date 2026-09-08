# OpenAI invite seat contract

## Verified mapping (2026-09-08)

| UI label | Local intent | Wire `seat_type` | Evidence |
|---|---|---|---|
| Standard | `standard` / `workspace_default` | **`default`** (or omit) | Dual World + Jupiter invite create / response |
| Premium | `premium` | **`prolite`** | Jupiter 1 UI invite → pending invite API `seat_type=prolite` |

| UI role | Wire `role` |
|---|---|
| Owner | `account-owner` |
| Member | `standard-user` |

### Critical corrections

- UI **Premium ≠** JSON `"premium"` (invite create returns 422 `not a valid SeatType`).
- UI **Premium =** JSON **`"prolite"`**.
- UI **Standard ≠** JSON `"standard"`; Standard = **`"default"`**.
- Old Team48 default `seat_type=premium` was wrong on both axes.

## Capture sources

1. **Standard**: HME probe invite create on Dual World / Jupiter → response `seat_type=default`.
2. **Premium**: Operator UI invite on Jupiter 1 (Owner + Premium) → Team48 `GET .../invites` observed:
   ```json
   {
     "role": "account-owner",
     "seat_type": "prolite",
     "status": 2
   }
   ```

## Operator settings (VPS)

- `invite_seat_wire_standard` = `default`
- `invite_seat_wire_premium` = `prolite`

Code: `VERIFIED_INVITE_SEAT_WIRE_VALUES` seeds both values.

## Fixtures

- `tests/fixtures/openai/invite_standard_default_success.json`
- `tests/fixtures/openai/invite_premium_prolite_success.json`
- `tests/fixtures/openai/invite_seat_type_rejected.json` (`premium` string rejected)
- `tests/fixtures/openai/invite_premium_user_patch_rate_limited.json` (historical blind-guess notes)

## Invite payload examples

Standard member:
```json
{
  "email_addresses": ["<email>"],
  "role": "standard-user",
  "resend_emails": true,
  "seat_type": "default"
}
```

Premium owner (matches Jupiter UI capture):
```json
{
  "email_addresses": ["<email>"],
  "role": "account-owner",
  "resend_emails": true,
  "seat_type": "prolite"
}
```
