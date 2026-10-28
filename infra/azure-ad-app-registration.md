# Azure AD App Registration — Slotly Microsoft OAuth

Tento dokument popisuje, jak byl Azure AD (Entra ID) app registration pro Slotly vytvořen a jak ho spravovat.

**App ID (Client ID):** `de731224-93d5-4c37-8e53-e31e31fa478e`  
**Object ID:** `684906d1-2733-49d6-a67d-28d0545ba205`  
**Tenant:** `bifactory.cz` (fe9754b6-16aa-49f7-94ed-95f5424036c8)  
**Sign-in audience:** `AzureADandPersonalMicrosoftAccount`  
**Secret expires:** `2028-10-28T19:55:01Z` ← **přidat si reminder do kalendáře na 2028-09-28**

---

## Kroky 1–4: CLI (idempotentní)

### Krok 1: Check for existing app + create

```bash
# Vždy nejdřív zkontroluj jestli app už existuje
az ad app list --display-name "Slotly" --query "[].{appId:appId, id:id}" -o json

# Pokud prázdný výsledek, vytvoř novou app:
az ad app create \
  --display-name "Slotly" \
  --sign-in-audience AzureADandPersonalMicrosoftAccount \
  --web-redirect-uris "https://api.slotly.team/api/oauth/microsoft/callback" \
  --query "{appId:appId, displayName:displayName, id:id}" -o json
```

### Krok 2: Vytvoř Service Principal

Nutné pro admin consent grant:

```bash
APP_ID="<appId z kroku 1>"
az ad sp create --id "$APP_ID"
```

### Krok 3: Přidej API permissions

Microsoft Graph delegated scopes (přesná GUID pro každý scope):

```bash
APP_OBJECT_ID="<id z kroku 1>"
GRAPH_API_ID="00000003-0000-0000-c000-000000000000"

# offline_access, User.Read, Calendars.Read, Calendars.ReadWrite
for pid in \
  7427e0e9-2fba-42fe-b0c0-848c9e6a8182 \
  e1fe6dd8-ba31-4d61-89e7-88639da4683d \
  465a38f9-76ea-45b9-9f34-9e8b0d4b0b42 \
  1ec239c2-d7c9-4623-a91a-a9775856bb36; do
  az ad app permission add \
    --id "$APP_OBJECT_ID" \
    --api "$GRAPH_API_ID" \
    --api-permissions "${pid}=Scope"
done

# Grant admin consent pro bifactory.cz tenant (eliminuje tenant-consent dialog)
az ad app permission grant \
  --id "$APP_ID" \
  --api "$GRAPH_API_ID" \
  --scope "offline_access User.Read Calendars.Read Calendars.ReadWrite"
```

Permission GUIDs reference:
| Scope | GUID |
|-------|------|
| `offline_access` | `7427e0e9-2fba-42fe-b0c0-848c9e6a8182` |
| `User.Read` | `e1fe6dd8-ba31-4d61-89e7-88639da4683d` |
| `Calendars.Read` | `465a38f9-76ea-45b9-9f34-9e8b0d4b0b42` |
| `Calendars.ReadWrite` | `1ec239c2-d7c9-4623-a91a-a9775856bb36` |

### Krok 4: Vytvoř client secret (24-month expiry)

```bash
APP_OBJECT_ID="684906d1-2733-49d6-a67d-28d0545ba205"
az ad app credential reset --id "$APP_OBJECT_ID" --years 2 \
  --query "{secret:password, endDateTime:endDateTime}" -o json
```

Zkopíruj `secret` value (zobrazí se jen jednou!) → ulož do `infra/.secrets/microsoft_oauth`:

```
MICROSOFT_OAUTH_CLIENT_ID=de731224-93d5-4c37-8e53-e31e31fa478e
MICROSOFT_OAUTH_CLIENT_SECRET=<secret value>
MICROSOFT_OAUTH_SECRET_EXPIRES_AT=<endDateTime>
```

### Krok 5: Nastav Homepage URL

```bash
APP_OBJECT_ID="684906d1-2733-49d6-a67d-28d0545ba205"
az ad app update --id "$APP_OBJECT_ID" --web-home-page-url "https://slotly.team"
```

---

## Kroky 5–6: Manuální kroky v Azure Portal

Tyto kroky nelze provést přes az CLI pro `AzureADandPersonalMicrosoftAccount` apps.

### Co uvidíš v Portalu

```
portal.azure.com → Microsoft Entra ID → App registrations → Slotly → Branding & properties
```

### Krok 5: Upload Logo

- Klikni **Upload new logo**
- Nahraj `~/Desktop/slotly-oauth-logo-512.png` (stejné jako Google OAuth consent screen logo)
- Azure Portal logo požadavek: PNG, max 256×256, max 100KB — zmenši před nahráním pokud třeba
- Uložit

### Krok 6: Nastav Privacy + Terms URLs

Na stejné stránce **Branding & properties**:

| Pole | Hodnota |
|------|---------|
| Home page URL | `https://slotly.team` (nastaveno přes CLI) |
| Terms of service URL | `https://slotly.team/terms` |
| Privacy statement URL | `https://slotly.team/privacy` |

Uložit.

---

## Rotace secretu

Provést nejpozději **2028-09-28** (měsíc před expirací `2028-10-28`).

```bash
APP_OBJECT_ID="684906d1-2733-49d6-a67d-28d0545ba205"

# 1. Vytvoř nový secret (starý zůstane platný do konce jeho platnosti)
az ad app credential reset --id "$APP_OBJECT_ID" --years 2 \
  --query "{secret:password, endDateTime:endDateTime}" -o json

# 2. Ulož nový secret do infra/.secrets/microsoft_oauth

# 3. Nasaď nový secret do Container App (viz infra/deploy.sh ms-oauth-env)
./infra/deploy.sh ms-oauth-env

# 4. Ověř že app funguje (302 na microsoft login, ne 503)
curl -sI https://api.slotly.team/api/oauth/microsoft/start

# 5. Smaž starý secret z Azure Portal (Certificates & secrets → delete)
```

Přidej si do kalendáře: **2028-09-28 — rotovat MICROSOFT_OAUTH_CLIENT_SECRET**

---

## Troubleshooting

### AADSTS65001: The user or administrator has not consented

Příčina: enterprise tenant vyžaduje admin consent pro Calendar scopes.

Řešení pro uživatele: "Požádej svého IT admina o povolení aplikace Slotly v Azure AD."

Řešení pro admina: 
```
https://login.microsoftonline.com/<tenant-id>/adminconsent?client_id=de731224-93d5-4c37-8e53-e31e31fa478e
```

V Settings UI se tato chyba zobrazuje jako: "Your IT admin needs to approve Slotly for your organization."

### AADSTS50011: The reply URL specified in the request does not match

Příčina: Redirect URI v Azuru nesouhlasí přesně s tím co backend posílá.

Ověř:
```bash
az ad app show --id "de731224-93d5-4c37-8e53-e31e31fa478e" \
  --query "web.redirectUris" -o json
```

Musí být přesně: `["https://api.slotly.team/api/oauth/microsoft/callback"]`

Ověř env var v Container App:
```bash
az containerapp show -g slotly-prod -n slotly-backend \
  --query "properties.template.containers[0].env[?name=='MICROSOFT_OAUTH_REDIRECT_URI']" -o json
```

### 503 "Microsoft OAuth not configured"

Příčina: env vars `MICROSOFT_OAUTH_CLIENT_ID` nebo `MICROSOFT_OAUTH_CLIENT_SECRET` chybí v revision.

```bash
./infra/deploy.sh ms-oauth-env
```

### Personal account (Outlook.com) access denied

Příčina: app je omylem nakonfigurována jen pro AAD accounts (ne personal).

Ověř audience:
```bash
az ad app show --id "de731224-93d5-4c37-8e53-e31e31fa478e" \
  --query "signInAudience" -o json
# musí být: "AzureADandPersonalMicrosoftAccount"
```

Pokud vrátí `AzureADOnly` nebo jiné — vytvoř novou app (audience nelze změnit po vytvoření).

### Unverified publisher warning

Uživatel uvidí na consent screenu: "This app isn't verified by Microsoft."

Toto je normální pro nové apps s Calendars.ReadWrite scopem. Viz `docs/microsoft-verification.md`
pro postup jak submit for publisher verification.
