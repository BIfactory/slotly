# OAuth setup pro produkci — Google + Microsoft 365

Krok-za-krokem, co musí operator Slotly udělat mimo aplikaci, aby fungovalo
booking + SSO. Uživatelé aplikace do žádné konzole nelezou — jen kliknou na
"Sign in with Google/Microsoft".

Prod hosts:
- API: `https://api.slotly.team`
- Frontend: `https://slotly.team`

Redirect URI se u obou providerů registrují na backend, ne na frontend.

---

## Microsoft OAuth

**App registration:** See `infra/azure-ad-app-registration.md` for the Azure AD app creation steps, Container App env wiring, and secret rotation runbook.

**Scopes requested:**
- `offline_access` — refresh token for background sync
- `User.Read` — profile identity (name + email)
- `Calendars.Read` — free/busy intervals (no event titles, descriptions, or attendees)
- `Calendars.ReadWrite` — create events on explicit booking

**Verification:** See `docs/microsoft-verification.md` for the publisher-verification workflow (optional until beta scales).

**Supported account types:** work/school Microsoft 365 (any tenant, multi-tenant audience) + personal Outlook.com / outlook.live.com.

**Known constraints:**
- Enterprise tenants may require admin consent before any user in the tenant can grant Slotly access. The frontend shows an "Your IT admin needs to approve Slotly" banner when the OAuth flow returns `AADSTS65001`.
- Logo (240×240 PNG max 100KB) and Terms/Privacy URLs are set manually in the Azure Portal under App registrations → Slotly → Branding & properties — these cannot be set via `az` CLI for multi-tenant apps.

---

## 1. Google Cloud Console

1. Otevři <https://console.cloud.google.com> a vyber (nebo vytvoř) projekt
   `slotly-prod`.
2. **APIs & Services → Library** → povol:
   - Google Calendar API
   - Google People API (potřeba pro `userinfo` endpoint)
3. **APIs & Services → OAuth consent screen** → *External*, publish do
   "In production":
   - App name: `Slotly`
   - User support email: `hulin@bifactory.cz`
   - App domain: `https://slotly.team`
   - Authorized domains: `slotly.team`
   - Developer contact: `hulin@bifactory.cz`
   - Scopes → **Add or remove scopes** → dopiš:
     - `.../auth/calendar.events`
     - `.../auth/calendar.freebusy`
     - `openid`, `email`, `profile`
   - Test users: pár vlastních Google adres pro pilot
   - Submit for verification až Google zbrání ("this app isn't verified").
     Do té doby to funguje pro test users bez omezení.
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   - Type: **Web application**
   - Name: `Slotly backend`
   - Authorized JavaScript origins: `https://slotly.team`
   - Authorized redirect URIs:
     - `https://api.slotly.team/api/oauth/google/callback`
   - Ulož **Client ID** + **Client secret** — jdou do env `GOOGLE_OAUTH_CLIENT_ID`
     a `GOOGLE_OAUTH_CLIENT_SECRET`.

## 2. Microsoft Azure AD (Entra ID)

1. Otevři <https://portal.azure.com> → Microsoft Entra ID → **App registrations**
   → **New registration**:
   - Name: `Slotly`
   - Supported account types: **Accounts in any organizational directory (any
     Microsoft Entra ID tenant) and personal Microsoft accounts (e.g.
     outlook.com)** — jinak přijdeš o personal Outlook users.
   - Redirect URI (Web): `https://api.slotly.team/api/oauth/microsoft/callback`
   - Ulož **Application (client) ID** → `MICROSOFT_OAUTH_CLIENT_ID`.
2. **Certificates & secrets → New client secret**:
   - Description: `Slotly backend`
   - Expires: 24 měsíců (poznač si datum a přidej si do kalendáře reminder na
     rotaci nejméně měsíc předem — Slotly padne, když secret vyprší).
   - Zkopíruj **Value** (ne Secret ID!) → `MICROSOFT_OAUTH_CLIENT_SECRET`.
3. **API permissions → Add a permission → Microsoft Graph → Delegated**:
   - `offline_access`, `openid`, `email`, `profile`, `User.Read`,
     `Calendars.ReadWrite`
   - Klikni **Grant admin consent for &lt;tenant&gt;** — jinak některé tenant
     policies zablokují první přihlášení uživatele.

## 3. Env vars v prod Container App

Přes `az containerapp update` doplň OAuth secrets do `slotly-api` container appky.
Secrety uložit jako Azure Container Apps secret, ne plain env, aby nebyly v Bicep
templatu ani v azure portal deployment history.

```bash
az containerapp secret set \
  --name slotly-api \
  --resource-group <slotly-rg> \
  --secrets \
    google-client-id=<GOOGLE_CLIENT_ID> \
    google-client-secret=<GOOGLE_CLIENT_SECRET> \
    ms-client-id=<MS_CLIENT_ID> \
    ms-client-secret=<MS_CLIENT_SECRET>

az containerapp update \
  --name slotly-api \
  --resource-group <slotly-rg> \
  --set-env-vars \
    GOOGLE_OAUTH_CLIENT_ID=secretref:google-client-id \
    GOOGLE_OAUTH_CLIENT_SECRET=secretref:google-client-secret \
    GOOGLE_OAUTH_REDIRECT_URI=https://api.slotly.team/api/oauth/google/callback \
    MICROSOFT_OAUTH_CLIENT_ID=secretref:ms-client-id \
    MICROSOFT_OAUTH_CLIENT_SECRET=secretref:ms-client-secret \
    MICROSOFT_OAUTH_REDIRECT_URI=https://api.slotly.team/api/oauth/microsoft/callback
```

Container App udělá revision restart sám. Ověř:

```bash
curl -sI https://api.slotly.team/api/oauth/google/start?anon=1
# → 302 na accounts.google.com (ne 503, ne 500)
curl -sI https://api.slotly.team/api/oauth/microsoft/start?anon=1
# → 302 na login.microsoftonline.com
```

## 4. Django migrace

Nové tabulky (M18b + booking requests). Aplikuj přes management command
v novém revision (deploy.sh release už spouští `migrate` v entrypoint.sh):

```bash
./infra/deploy.sh build
./infra/deploy.sh release
```

Ověř:
```bash
az containerapp logs show \
  --name slotly-api \
  --resource-group <slotly-rg> \
  --tail 40 --follow
```
V logu při startu má být:
```
Applying scheduling.0002_googleaccount_write_calendar_id... OK
Applying scheduling.0003_microsoftaccount... OK
Applying scheduling.0004_bookingrequest... OK
```

## 5. Frontend

Frontend se nasadí sám push do `main` (Azure SWA). Před pushem:
```bash
cd frontend && npm run build
```

Po deploy zkontroluj:
- `https://slotly.team/auth/login` → vidíš "Sign in with Google" + "Sign in
  with Microsoft" tlačítka
- `https://slotly.team/settings/integrations` → obě karty (Google i Microsoft)

## 6. Runtime test golden path

1. Odhlaš se, otevři `https://slotly.team/auth/register`.
2. **Sign up with Google** → consent screen ukazuje Slotly + kalendář scope
   → povolit → padne na `/profile?google=connected&anon=1`.
3. `/settings/integrations` → Google karta je *Connected as ...*, vidíš dropdown
   *Calendar for new meetings* s tvými kalendáři.
4. Přepni share link na `/profile` (karta *Your public booking link*, toggle
   *On*) → zkopíruj URL.
5. V incognito otevři URL → vidíš kalendář, klik na volný slot → dialog s
   volbou *Online* / *In person*.
6. Vyplň jméno + email → *Book* → měl bys dostat pozvánku s **Google Meet
   linkem** v e-mailu do 30s.
7. Zkus i *In person* → napiš adresu → *Send request* → v Slotly bell + `/bookings`
   se objeví žádost → **Approve** → do inboxu návštěvníka přijde pozvánka
   bez Meet linku, ale s adresou v popisu.

## 6. Podpora více účtů

Uživatel si může připojit více Google i více Microsoft účtů do jednoho Slotly
profilu. Systém podporuje až 10 účtů na poskytovatele (soft cap). Jeden účet
přes všechny poskytovatele je označen jako *default* — sem se zapisují nové
schůzky a události, které Slotly vytváří.

Na stránce `/settings/calendars` vidí uživatel tabulku všech připojených účtů
s možností zvolit default pomocí radio tlačítka. Připojení stejné e-mailové
adresy podruhé obnoví token místo vytvoření nového řádku; připojení jiné
adresy přidá nový účet do seznamu. Legacy endpointy `/api/google-account` a
`/api/microsoft-account` zůstávají funkční a vrací default účet (nebo první
dostupný) pro zpětnou kompatibilitu.

## 7. Když něco nefunguje

| Symptom | Kde koukat |
|---|---|
| `503 Google not configured` na `/api/oauth/google/start` | Env `GOOGLE_OAUTH_CLIENT_ID`/`SECRET` nejsou v revision |
| Redirect URI mismatch po consentu | V Google/Azure konzoli nesouhlasí přesně (`https://` vs `http://`, trailing slash) |
| `AADSTS50011: The reply URL specified in the request does not match` | To samé pro Microsoft |
| Uživatel se přihlásí SSO, ale Slotly ho odhlásí | `SESSION_COOKIE_SECURE=True` + první login proběhl přes `http://` |
| MS 401 při list_calendars s corporate účtem | Admin consent chybí — v Azure Portal Grant admin consent |
| Meet link chybí v pozvánce | Chybí scope `calendar.events` — user musí Reconnect |
| Teams link chybí | Osobní outlook.com účty nedostávají Teams — funkční jen pro work/school |

## OAuth-driven busy intervals

Připojený OAuth účet automaticky vystavuje **všechny** calendary jako busy zdroje —
bez nutnosti ručně přidávat ICS URL. Polling probíhá každých 5 minut.

Podrobnosti o implementaci, privacy modelu a sémantickém driftu viz [`docs/oauth-busy.md`](oauth-busy.md).
