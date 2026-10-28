# Slotly MCP API

Slotly exposes a [Model Context Protocol](https://spec.modelcontextprotocol.io/specification/2024-11-05/) (MCP) endpoint that lets AI assistants and automation tools query your scheduling data on your behalf.

**Spec version:** `2024-11-05`  
**Base URL:** `https://<your-domain>/mcp/v1/`

---

## 1. Overview

The MCP endpoint lets any MCP-compatible client (e.g. Claude Desktop, Claude CLI, custom LLM agents) call four tools:

| Tool | Short description |
|---|---|
| `slotly.list_teams` | List teams the authenticated user belongs to |
| `slotly.list_members` | List members of a given team |
| `slotly.find_slots` | Find free meeting slots across a team |
| `slotly.check_time` | Check whether a set of members is free in a time window |

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

### Claude CLI

Use `claude mcp add` with the `--transport http` option and supply a Bearer header:

```bash
claude mcp add slotly \
  --transport http \
  https://example.slotly.app/mcp/v1 \
  --header "Authorization: Bearer sltly_<your-token>"
```

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "slotly": {
      "command": "npx",
      "args": [
        "@modelcontextprotocol/server-fetch",
        "https://example.slotly.app/mcp/v1",
        "--header", "Authorization: Bearer sltly_<your-token>"
      ]
    }
  }
}
```

> **Note:** Different MCP clients wire HTTP transport differently. The key requirement is that every request includes the `Authorization: Bearer <token>` header. Consult your client's documentation for the exact configuration syntax.

---

## 7. Scopes reference

| Scope | What it unlocks |
|---|---|
| `list_teams` | `slotly.list_teams` |
| `list_members` | `slotly.list_members` |
| `find_slots` | `slotly.find_slots` |
| `check_time` | `slotly.check_time` |

Tokens can be scoped narrowly — e.g. a token with only `find_slots` can call `slotly.find_slots` but will receive `403 scope_denied` for any other tool.
