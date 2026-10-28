# Microsoft OAuth Publisher Verification — kompletní postup

Slotly žádá **sensitive scope** Microsoft Graph:
- `Calendars.ReadWrite` — write events to the user's calendar

Writable calendar scopes způsobí, že Microsoft zobrazí consent screen s varováním "This app is not verified". Toto varování se odstraní po Publisher Verification.

| Publisher status | UX pro nového uživatele |
|---|---|
| **Unverified (výchozí)** | Žlutý banner: "This app isn't verified by Microsoft yet." Uživatel musí kliknout **Accept** přes warning — funguje, ale není hezké |
| **Verified** | Normální consent screen s Verified badge, žádný warning |

Cíl: dostat se na řádek 2.

> **Kdy řešit:** Publisher verification má smysl po 2-4 týdnech beta provozu, ne hned.
> Dokud nemáš real users hlásit problémy s warningem, nespěchej. App funguje i bez verifikace.

---

## Fáze 1 — HNED: Ověř že app je v produkci (ne jen test)

Nová app registration je automaticky dostupná pro všechny uživatele se signInAudience `AzureADandPersonalMicrosoftAccount` — žádný publish step jako u Google. Uživatelé uvidí consent screen hned.

**Ověř** (chvilka po Task 2 deploy):

```bash
curl -sI https://api.slotly.team/api/oauth/microsoft/start
# Bez session cookie: 401 (normální — endpoint vyžaduje login)
# NE 503 (503 = env vars chybí)
```

Pokud vrátí 503, spusť:
```bash
./infra/deploy.sh ms-oauth-env
```

**Co uživatel uvidí** při prvním Connect Microsoft:
> "This app isn't verified by Microsoft yet."

Je to žlutý warning ale má tlačítko **Accept** — projde bez problémů. Publisher Verification warning odstraní.

---

## Fáze 2 — Připravit Publisher Verification (asi hodina práce)

### 2a. Ověř doménu `slotly.team` v Azure

Podmínka pro Publisher Verification: musíš mít domain `slotly.team` ověřenou v Entra ID tenantu `bifactory.cz`.

```
portal.azure.com → Microsoft Entra ID → Custom domain names
```

Pokud `slotly.team` tam není → **Add custom domain** → dostaneš TXT record → přidej do DNS registrátora.

### 2b. Vyplň Publisher name

```
portal.azure.com → Microsoft Entra ID → App registrations → Slotly → Branding & properties
```

- **Publisher domain**: nastavit na `slotly.team` (musí být verified viz 2a)
- Uložit

### 2c. Logo a branding

Zkontroluj (nebo nahraj) v **Branding & properties**:

| Pole | Hodnota |
|---|---|
| Logo | `~/Desktop/slotly-oauth-logo-512.png` (čtvercové PNG, max 256×256, max 100KB) |
| Home page URL | `https://slotly.team` |
| Terms of service URL | `https://slotly.team/terms` |
| Privacy statement URL | `https://slotly.team/privacy` |

### 2d. Privacy policy compliance

`/privacy` musí obsahovat:
- Výčet každého scope a proč ho používáme
- Jak data uchováváme a jak je lze smazat
- Explicitní zmínku Microsoft Graph scopes (přidej analogicky ke Google sekci)

### 2e. Natočit demo video (volitelné pro MS verifikaci)

Microsoft Publisher Verification **nevyžaduje video** jako Google. Ale pokud chceš vyšší úroveň ověření (Certified app), video pomůže.

---

## Fáze 3 — Submit Publisher Verification

```
portal.azure.com → Microsoft Entra ID → App registrations → Slotly → Branding & properties → Publisher verification
```

Klikni **Add a verified publisher** → zadej MPN ID (Microsoft Partner Network). Pokud BIfactory ještě není v MPN:

1. Zaregistruj se zdarma na https://partner.microsoft.com
2. Počkej na aktivaci (obvykle < 24h)
3. Vrať se do App registration → Publisher verification → zadej MPN ID

Po úspěšném propojení se na consent screenu zobrazí **Verified** badge.

---

## Scope justification texty

Tyto texty jsou připravené pro případ, že Microsoft nebo partner review je požaduje.

**`Calendars.Read`**
```
Read free/busy times from the user's Microsoft 365 or Outlook.com calendar
to compute shared availability with their teammates. Slotly reads only
start/end + free/busy flag — never event titles, attendees, or descriptions.
This is Slotly's core "find a time everyone can meet" feature.
```

**`Calendars.ReadWrite`**
```
Create and update calendar events when the user explicitly books a meeting
through Slotly (by clicking "Book" from a search result or a public share
link). We also list the user's calendars so they can choose which calendar
new meetings should be written to. We never read event titles or attendees
through this scope.
```

**`offline_access`**
```
Required so Slotly can refresh the Microsoft access token in the background
without re-prompting the user. Without offline_access, the user would need
to re-authenticate every hour. Token is stored encrypted (Fernet AES-128)
in the database and can be revoked by disconnecting the account from Settings.
```

**`User.Read`**
```
Fetch the user's display name and email address from Microsoft Graph
(/me endpoint) to create the Slotly profile identity and display it on
their public scheduling link. No other profile data is read.
```

---

## Rotace client secret

**Deadline: 2028-09-28** (měsíc před expirací `2028-10-28T19:55:01Z`).

```bash
# Vytvoř nový secret
APP_OBJECT_ID="684906d1-2733-49d6-a67d-28d0545ba205"
az ad app credential reset --id "$APP_OBJECT_ID" --years 2 \
  --query "{secret:password, endDateTime:endDateTime}" -o json

# Ulož do infra/.secrets/microsoft_oauth

# Nasaď do Container App
./infra/deploy.sh ms-oauth-env
```

Přidej si do kalendáře: **2028-09-28 — rotovat MICROSOFT_OAUTH_CLIENT_SECRET**.

---

## TL;DR co udělat hned a co počkat

| Akce | Kdy | Čas |
|---|---|---|
| `./infra/deploy.sh ms-oauth-env` + ověř 401 (ne 503) | Hned po merge PR | 5 min |
| Ověř `slotly.team` doménu v Entra ID | Před verifikací | 15 min |
| Publisher Verification (MPN + domain) | Po 2-4 týdnech beta provozu | 1-2h |
| Přidat reminder rotace secretu do kalendáře | Hned | 2 min |
