# Slotly MCP API

Slotly exposes a [Model Context Protocol](https://spec.modelcontextprotocol.io/specification/2024-11-05/) (MCP) endpoint that lets AI assistants and automation tools query your scheduling data on your behalf.

**Spec version:** `2024-11-05`  
**Base URL:** `https://<your-domain>/mcp/v1/`

---

## 1. Overview

The MCP endpoint lets any MCP-compatible client (e.g. Claude Desktop, Claude CLI, custom LLM agents) call 8 tools:

| Tool | Sensitivity | Short description |
|---|---|---|
| `slotly.list_teams` | `read` | List teams the authenticated user belongs to |
| `slotly.list_members` | `read` | List members of a given team |
| `slotly.find_slots` | `read` | Find free meeting slots across a team |
| `slotly.check_time` | `read` | Check whether a set of members is free in a time window |
| `slotly.create_booking` | `write_external` | Create a booking and write an event to the host calendar |
| `slotly.update_booking` | `write_external` | Reschedule or rename an existing booking |
| `slotly.cancel_booking` | `write_external` | Cancel a booking and delete the provider event |
| `slotly.create_event` | `write_external` | Write a bare calendar event (no Booking row) |

Access is controlled by **Personal Access Tokens (PATs)** created at `/settings/api-tokens`. Each token carries a fixed set of scopes that limit which tools it can call.

---

## 2. Authentication

All requests to `/mcp/v1/*` must include a Bearer token in the `Authorization` header:

```
Authorization: Bearer sltly_<token>
```

Tokens are created at [/settings/api-tokens](/settings/api-tokens). The raw token is shown **once** at creation time — copy it immediately.

### Token prefix

Every token starts with `sltly_` followed by a URL-safe random string. The prefix (first 12 characters of the full token) is stored and shown in the UI for identification, but the full token is never stored or displayed again after creation.

### Revoking a token

Tokens can be revoked from `/settings/api-tokens` at any time. Revocation takes effect immediately — all subsequent requests using that token receive `401 invalid_token`.

---

## 3. Rate limits

| Limit | Value |
|---|---|
| Per-token per-minute | 60 requests |
| Per-token per-hour | 1 000 requests |

When the limit is exceeded the API returns:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 14
```

```json
{
  "error": "rate_limited",
  "detail": "Too many requests. Try again in 14 seconds."
}
```

Clients should inspect the `Retry-After` header (seconds) and back off accordingly.

---

## 4. Error shapes

All errors from `/mcp/v1/*` use the same envelope:

```json
{
  "error": "<code>",
  "detail": "<human-readable message>"
}
```

| HTTP status | `error` code | When |
|---|---|---|
| 400 | `invalid_request` | Required field missing or malformed body |
| 400 | `validation_error` | Tool parameter failed validation |
| 401 | `invalid_token` | Missing, malformed, or revoked Bearer token |
| 403 | `scope_denied` | Token lacks the scope for this tool |
| 403 | `forbidden` | Caller is not a member of the referenced team |
| 404 | `unknown_tool` | `name` does not match any registered tool |
| 429 | `rate_limited` | Per-token rate limit exceeded |
| 500 | `server_error` | Unhandled internal error |

---

## 5. Tool catalog

### Endpoints

```
GET  /mcp/v1/tools/list   – list all tools (no scope required)
POST /mcp/v1/tools/call   – call a tool
```

#### POST /mcp/v1/tools/call — request shape

```json
{
  "name": "<tool-name>",
  "params": { ... }
}
```

---

### 5.1 `slotly.list_teams`

Vrátí seznam týmů, ve kterých je uživatel členem.

**Required scope:** `list_teams`

**Input schema:**

```json
{
  "type": "object",
  "properties": {},
  "additionalProperties": false
}
```

**Output:**

```json
{
  "teams": [
    {
      "id": 1,
      "name": "Engineering",
      "description": "Backend + frontend engineers",
      "member_count": 5
    }
  ]
}
```

**Example:**

```bash
curl -s https://example.slotly.app/mcp/v1/tools/call \
  -H "Authorization: Bearer sltly_abc123..." \
  -H "Content-Type: application/json" \
  -d '{"name": "slotly.list_teams", "params": {}}'
```

---

### 5.2 `slotly.list_members`

Vrátí členy daného týmu.

**Required scope:** `list_members`

**Input schema:**

```json
{
  "type": "object",
  "properties": {
    "team_id": { "type": "integer" }
  },
  "required": ["team_id"],
  "additionalProperties": false
}
```

**Output:**

```json
{
  "team_id": 1,
  "members": [
    {
      "id": 42,
      "email": "alice@example.com",
      "display_name": "Alice Novak",
      "timezone": "Europe/Prague",
      "working_hours": { "mon": [["09:00", "17:00"]], "..." : "..." },
      "role": "member"
    }
  ]
}
```

**Example:**

```bash
curl -s https://example.slotly.app/mcp/v1/tools/call \
  -H "Authorization: Bearer sltly_abc123..." \
  -H "Content-Type: application/json" \
  -d '{"name": "slotly.list_members", "params": {"team_id": 1}}'
```

---

### 5.3 `slotly.find_slots`

Najdi společné volné sloty pro členy týmu v zadaném okně.

**Required scope:** `find_slots`

**Input schema:**

```json
{
  "type": "object",
  "properties": {
    "team_id":      { "type": "integer" },
    "member_ids":   { "type": "array", "items": { "type": "integer" } },
    "duration_min": { "type": "integer", "minimum": 15, "maximum": 1440 },
    "window_start": { "type": "string", "format": "date-time" },
    "window_end":   { "type": "string", "format": "date-time" },
    "buffer_min":   { "type": "integer", "minimum": 0, "maximum": 240, "default": 0 },
    "limit":        { "type": "integer", "minimum": 1, "maximum": 500, "default": 100 }
  },
  "required": ["team_id", "duration_min"],
  "additionalProperties": false
}
```

- `member_ids` defaults to all team members if omitted.
- `window_start` defaults to now; `window_end` defaults to now + 14 days.
- Timestamps must include a timezone offset (e.g. `2026-10-28T09:00:00+02:00`).
- Maximum window size is 60 days.

**Output:**

```json
{
  "slots": [
    { "start": "2026-10-29T09:00:00+01:00", "end": "2026-10-29T10:00:00+01:00" }
  ],
  "count": 1,
  "truncated": false
}
```

**Example:**

```bash
curl -s https://example.slotly.app/mcp/v1/tools/call \
  -H "Authorization: Bearer sltly_abc123..." \
  -H "Content-Type: application/json" \
  -d '{
    "name": "slotly.find_slots",
    "params": {
      "team_id": 1,
      "duration_min": 60,
      "window_start": "2026-10-29T00:00:00+01:00",
      "window_end":   "2026-11-05T23:59:59+01:00"
    }
  }'
```

---

### 5.4 `slotly.check_time`

Ověř zda jsou vybraní členové týmu volní v daném okně.

**Required scope:** `check_time`

**Input schema:**

```json
{
  "type": "object",
  "properties": {
    "team_id":    { "type": "integer" },
    "member_ids": { "type": "array", "items": { "type": "integer" }, "minItems": 1 },
    "start":      { "type": "string", "format": "date-time" },
    "end":        { "type": "string", "format": "date-time" }
  },
  "required": ["team_id", "member_ids", "start", "end"],
  "additionalProperties": false
}
```

- Maximum window is 24 hours.
- Timestamps must include a timezone offset.
- Caller must be a member of the team.

**Output:**

```json
{
  "team_id": 1,
  "start": "2026-10-29T14:00:00+01:00",
  "end":   "2026-10-29T15:00:00+01:00",
  "available": true,
  "members": [
    {
      "user_id": 42,
      "display_name": "Alice Novak",
      "email": "alice@example.com",
      "status": "free",
      "conflicts": []
    }
  ]
}
```

`conflicts` contains merged `{start, end}` intervals where the member is busy. Event titles are never exposed.

**Example:**

```bash
curl -s https://example.slotly.app/mcp/v1/tools/call \
  -H "Authorization: Bearer sltly_abc123..." \
  -H "Content-Type: application/json" \
  -d '{
    "name": "slotly.check_time",
    "params": {
      "team_id": 1,
      "member_ids": [42, 43],
      "start": "2026-10-29T14:00:00+01:00",
      "end":   "2026-10-29T15:00:00+01:00"
    }
  }'
```

---

## 6. MCP client configuration

### Claude CLI / Desktop

MCP transports vary by client. Consult your client's documentation
for how to add a remote HTTP server with a custom `Authorization`
header. In general you will configure:

- URL: `https://slotly.app/mcp/v1/tools/call` (or `/tools/list` where applicable)
- Headers: `Authorization: Bearer <your token>`

Example `curl` for `tools/list`:

```bash
curl -H "Authorization: Bearer sltly_..." \
     https://example.slotly.app/mcp/v1/tools/list
```

---

## 7. Scopes reference

| Scope | What it unlocks |
|---|---|
| `list_teams` | `slotly.list_teams` |
| `list_members` | `slotly.list_members` |
| `find_slots` | `slotly.find_slots` |
| `check_time` | `slotly.check_time` |
| `create_booking` | `slotly.create_booking`, `slotly.update_booking`, `slotly.cancel_booking` |
| `write_event` | `slotly.create_event` |

Tokens can be scoped narrowly — e.g. a token with only `find_slots` can call `slotly.find_slots` but will receive `403 scope_denied` for any other tool. Write scopes (`create_booking`, `write_event`) are configured separately in **Settings → API tokens** and are marked with an amber warning.

---

## 8. Write capabilities

Write tools create, update, or cancel calendar entries and bookings on
behalf of the authenticated Slotly user. Every write tool is marked
`write_external` in `tools/list`, and the gateway returns `429` under a
tighter rate limit (20/min, 100/hour per token). Agents SHOULD prompt
the user for confirmation before dispatching a write tool.

### Scopes

Write capabilities require one of two write scopes in addition to any
read scopes:

| Scope | Capabilities covered |
|---|---|
| `create_booking` | `slotly.create_booking`, `slotly.update_booking`, `slotly.cancel_booking` |
| `write_event` | `slotly.create_event` |

Create write-enabled tokens in **Settings → API tokens**. The write section
is marked with an amber warning; read tokens are unaffected.

### Idempotency

Write tools support an optional top-level `idempotency_key` field on the
`/mcp/v1/tools/call` body:

```json
{
  "name": "slotly.create_booking",
  "params": {"attendee_user_ids": [42], "start": "...", "end": "..."},
  "idempotency_key": "<any string, 1-128 chars>"
}
```

If the same `(token, idempotency_key)` is seen again within 24 hours and
the previous call succeeded, the cached response is returned immediately
and no side effect fires. Failed calls do not occupy a key — retrying
with the same key after a failure re-executes the request.

Idempotency keys are scoped per token: two different tokens can use the
same raw key without conflict. Read tools silently ignore
`idempotency_key`.

### Rate limits (write tier)

Writes are capped tighter than reads:

| Window | Reads | Writes |
|---|---|---|
| per minute | 60 | 20 |
| per hour | 1000 | 100 |

On throttle, you get `HTTP 429 {"error": "rate_limited", "retry_after": N}` —
same shape as reads.

### Error codes added by F15

In addition to the F14 error codes:

| HTTP | `error` | `detail.code` | Meaning |
|---|---|---|---|
| 400 | `validation_error` | `slot_conflict` | Someone's calendar now has an overlapping event; refresh and retry with a different time. |
| 400 | `validation_error` | `already_ended` | Cancelling / updating a booking whose end time is in the past. |
| 400 | `validation_error` | `meeting_type_inactive` | The referenced `MeetingType` was deleted (soft-removed, `is_active=False`). |
| 503 | `upstream_oauth_failed` | — | The host's Google / Microsoft OAuth grant is broken; agent should prompt the user to reconnect in **Settings → Calendars**. |

Audit log also records `error_code="idempotency_hit"` for cache-returning
calls (`status="ok"`); these count toward the rate limit normally.

---

### 8.1 `slotly.create_booking`

Creates a meeting between the authenticated host and one or more Slotly
users. Writes an event to the host's connected calendar; attendees are
added as RFC 5545 ATTENDEE entries and receive the standard provider
invite (Google / Outlook email).

**Params:**

| Field | Type | Required | Notes |
|---|---|---|---|
| `attendee_user_ids` | `int[]` | yes | 1-100 user ids from `slotly.list_members`. |
| `start` | ISO 8601 string | yes | Must include timezone offset. |
| `end` | ISO 8601 string | yes | 5 min ≤ end-start ≤ 12 h. |
| `title` | string | no | Max 200 chars. Defaults to the attendee name. |
| `notes` | string | no | Max 2000 chars. |
| `meeting_type_id` | int | no | Reference to a `MeetingType` the host owns. |

**Returns:**

```json
{
  "result": {
    "booking_uuid": "a1b2c3d4-...",
    "status": "ok",
    "event": {
      "id": "google_event_id",
      "html_link": "https://calendar.google.com/...",
      "meet_link": "https://meet.google.com/...",
      "start": "2026-11-04T10:00:00+01:00",
      "end": "2026-11-04T10:30:00+01:00",
      "provider": "google"
    },
    "invited_emails": ["a@x", "b@y"],
    "manage_url": "https://slotly.app/b/..."
  }
}
```

**Example:**

```bash
curl -X POST https://slotly.app/mcp/v1/tools/call \
  -H "Authorization: Bearer sltly_abc123..." \
  -H "Content-Type: application/json" \
  -d '{
    "name": "slotly.create_booking",
    "params": {
      "attendee_user_ids": [42, 56],
      "start": "2026-11-04T10:00:00+01:00",
      "end":   "2026-11-04T10:30:00+01:00",
      "title": "Quick sync"
    },
    "idempotency_key": "my-client-abc123"
  }'
```

---

### 8.2 `slotly.update_booking`

Changes the start/end and/or title of a booking the authenticated user
hosts. On a time change, the old provider event is deleted and a new one
is created with the new time; attendees receive the standard provider
reschedule notification.

**Params:**

| Field | Type | Required |
|---|---|---|
| `booking_uuid` | UUID string | yes |
| `start` | ISO 8601 | if changing time |
| `end` | ISO 8601 | if changing time (must accompany `start`) |
| `title` | string | if changing title |

At least one of `(start+end)` or `title` must be set.

**Returns:**

```json
{
  "result": {
    "booking_uuid": "...",
    "status": "updated",
    "event": {"id": "...", "html_link": "...", "start": "...", "end": "...", "provider": "google"},
    "start": "...",
    "end": "...",
    "title": "..."
  }
}
```

**Example:**

```bash
curl -X POST https://slotly.app/mcp/v1/tools/call \
  -H "Authorization: Bearer sltly_abc123..." \
  -H "Content-Type: application/json" \
  -d '{
    "name": "slotly.update_booking",
    "params": {
      "booking_uuid": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "start": "2026-11-04T11:00:00+01:00",
      "end":   "2026-11-04T11:30:00+01:00"
    }
  }'
```

---

### 8.3 `slotly.cancel_booking`

Idempotent at the server side (second call on a cancelled booking returns
current state without re-sending the cancellation email).

**Params:**

| Field | Type | Required |
|---|---|---|
| `booking_uuid` | UUID string | yes |
| `reason` | string ≤500 chars | no |

**Returns:**

```json
{
  "result": {
    "booking_uuid": "...",
    "status": "cancelled",
    "cancelled_at": "2026-11-04T09:00:00+01:00",
    "reason": "Rescheduled to next week"
  }
}
```

**Example:**

```bash
curl -X POST https://slotly.app/mcp/v1/tools/call \
  -H "Authorization: Bearer sltly_abc123..." \
  -H "Content-Type: application/json" \
  -d '{
    "name": "slotly.cancel_booking",
    "params": {
      "booking_uuid": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "reason": "Rescheduled to next week"
    }
  }'
```

---

### 8.4 `slotly.create_event`

Writes a bare calendar event to the host's write calendar. No `Booking`
row is created — use this for "put a reminder on my calendar" scenarios
rather than scheduled meetings.

**Params:**

| Field | Type | Required | Notes |
|---|---|---|---|
| `start` | ISO 8601 | yes | with TZ offset |
| `end` | ISO 8601 | yes | 5 min ≤ end-start ≤ 24 h |
| `title` | string | yes | 1-200 chars |
| `calendar_id` | string | no | Defaults to host's `write_calendar_id`. |
| `attendee_emails` | string[] | no | ≤50; raw email format, no User lookup. |

**Returns:**

```json
{
  "result": {
    "status": "ok",
    "event": {
      "id": "google_event_id",
      "html_link": "https://calendar.google.com/...",
      "start": "2026-11-04T10:00:00+01:00",
      "end": "2026-11-04T10:30:00+01:00",
      "provider": "google",
      "calendar_id": "primary"
    },
    "invited_emails": []
  }
}
```

**Example:**

```bash
curl -X POST https://slotly.app/mcp/v1/tools/call \
  -H "Authorization: Bearer sltly_abc123..." \
  -H "Content-Type: application/json" \
  -d '{
    "name": "slotly.create_event",
    "params": {
      "start": "2026-11-04T10:00:00+01:00",
      "end":   "2026-11-04T10:30:00+01:00",
      "title": "Team standup reminder"
    }
  }'
```

---

### Confirm-before-write pattern (MCP client guidance)

Writes are side-effectful. We recommend clients:

1. Call `tools/list` and inspect `annotations.sensitivity`.
2. For any tool where `sensitivity == "write_external"`, prompt the user with the full params payload before dispatch.
3. Always set `idempotency_key` (any client-generated UUID). Repeat on retry.
4. On `503 upstream_oauth_failed`, link the user to `/settings/calendars` to reconnect.
5. On `400 slot_conflict`, re-run `slotly.find_slots` with the same window and offer the user a different slot.

---

## Deploy checklist

After merging a release that touches `/mcp/v1/*`:

1. Verify migrations applied: `python manage.py migrate --plan` shows no pending.
2. Create a test token in `/settings/api-tokens` with scopes `list_teams, find_slots, create_booking`.
3. Smoke read: `curl -H "Authorization: Bearer sltly_abc123..." https://slotly.app/mcp/v1/tools/list` → 200, see 8 tools with `annotations.sensitivity` set.
4. Smoke write (if `create_booking` is in your scopes):
   ```bash
   curl -X POST https://slotly.app/mcp/v1/tools/call \
     -H "Authorization: Bearer sltly_abc123..." \
     -H "Content-Type: application/json" \
     -d '{"name":"slotly.create_booking","params":{"attendee_user_ids":[<test-user-id>],"start":"<future ISO>","end":"<future ISO + 30m>"},"idempotency_key":"smoke-test-1"}'
   ```
5. Verify the Booking row exists in `/admin/scheduling/booking/` and the event appears in the test host's Google / Outlook calendar.
6. Hit the same curl again with the same `idempotency_key` → identical response, no new Booking row.
7. In admin, confirm two `MCPCall` rows: first `status="ok"`, second `status="ok" error_code="idempotency_hit"`.
8. Revoke the test token in `/settings/api-tokens`. Subsequent curl → 401.
