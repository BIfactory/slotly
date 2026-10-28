# OAuth-Driven Busy Intervals

> **Zdeňkův incident fix:** Nový calendar přidaný na Google straně se automaticky zobrazí
> v busy výpočtu do 5 minut — bez nutnosti ručně přidávat ICS URL.

## Jak to funguje

### Přehled

Slotly má dva zdroje busy intervalů, které jsou sjednoceny (UNION):

1. **ICS pipeline** — existující ICS subscriptions (`apps/calendars`), polling každých 5 min
2. **OAuth busy** — tato funkce: polling přes Google Calendar freebusy API / MS Graph calendarView

Oba zdroje běží nezávisle. OAuth busy je **additivní** — nevytlačuje ICS pipeline.

### Polling cycle

Celery beat task `scheduling.sync_all_oauth_accounts` se spouští každých **5 minut**
(shodně s ICS cadencí). Pro každý připojený `GoogleAccount` a `MicrosoftAccount`:

1. Enqueues `scheduling.sync_one_oauth_account(provider, account_pk)`
2. Per-account task:
   a. Načte seznam všech kalendarů z `calendarList` (Google) nebo `/me/calendars` (Microsoft)
   b. Fetchne busy intervaly pro okno `[now - 7d, now + 60d]` (shodné s ICS oknem)
   c. Atomicky nahradí `OAuthBusyWindow` řádky pro daný účet (DELETE + bulk INSERT)

**Nový calendar mid-cycle:** `calendarList` se re-fetchne při každém cyklu (žádné cache).
Nový calendar se zobrazí do 5 minut od přidání na Google/Microsoft straně.

### Freebusy API

| Provider | Endpoint | Poznámka |
|----------|----------|----------|
| Google | `POST /calendar/v3/freeBusy` | max 50 calendars/request; batch se automaticky |
| Microsoft | `GET /me/calendars/{id}/calendarView` | per-calendar; filter `showAs != free` |

### Datový model

```
OAuthBusyWindow(
    user          FK → User (CASCADE)
    provider      "google" | "microsoft"
    google_account    FK → GoogleAccount (CASCADE, nullable)
    microsoft_account FK → MicrosoftAccount (CASCADE, nullable)
    calendar_id   str  — ID kalendáře u providera
    starts_at     DateTimeField
    ends_at       DateTimeField
    fetched_at    DateTimeField (auto)
)
```

Index na `(user, starts_at, ends_at)` pro rychlé dotazy search enginu.

## Privacy

`OAuthBusyWindow` ukládá **pouze** `(starts_at, ends_at)` — žádné názvy eventů,
popis, lokace, účastníci. Toto je záměrné a vyžadováno PRD §5.2 a F15-R9.

Jakékoliv přidání dalšího pole musí projít privacy review.

Shodné chování jako ICS pipeline (ukládá jen `dtstart`/`dtend`).

## Fallback — co se stane, když OAuth selže

**Broken token (uživatel odvolal souhlas):**
- Task loguje warning a přeskočí daný účet
- Existující `OAuthBusyWindow` řádky pro daný účet jsou **zachovány** (stale data jsou lepší než prázdná busy list)
- ICS pipeline funguje **nezávisle** a nadále blokuje busy intervaly z ICS subscriptions

**Odpojení účtu:**
- `OAuthBusyWindow` řádky pro daný účet jsou **cascade-deleted** při smazání `GoogleAccount` / `MicrosoftAccount` záznamu
- ICS subscriptions nejsou ovlivněny

## Sémantický drift — all-day transparent events

Mezi ICS pipeline a OAuth busy existuje záměrný rozdíl v sémantice all-day eventů:

| Zdroj | All-day transparent event |
|-------|--------------------------|
| **ICS pipeline** | **Busy** — all-day eventy jsou vždy považovány za blokující (vacation, sick day) |
| **OAuth busy** | **Free** — Google freebusy API vrací jen opaque eventy; transparent (i all-day transparent) se neobjeví |

Tato odchylka je záměrná a zachována pro zpětnou kompatibilitu ICS pipeline.
Výsledkem je, že všechny-denní event označený "Available" na Google se projeví jako busy
skrz ICS pipeline, ale ne skrze OAuth busy — finální výsledek závisí na tom, zda uživatel
má ICS subscription pro daný kalendář.

Pro nové kalendáře (bez ICS) platí OAuth semantika — all-day transparent eventy blokovat nebudou.

## Operatorská poznámka

Po nasazení proběhne první polling cycle do 5 minut. `OAuthBusyWindow` řádky
jsou viditelné přes Django admin (`scheduling → OAuth busy windows`).

Ověření funkce:
```sql
SELECT count(*), provider FROM scheduling_oauthbusywindow GROUP BY provider;
```
