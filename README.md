# HeavenlySourcing

End-to-end procurement automation for restaurants. Upload a menu PDF and the
system:

1. Parses every dish into structured recipes and ingredients (GPT-4o + PyMuPDF).
2. Pulls real USDA pricing data so the kitchen knows market context.
3. Discovers wholesale distributors near the restaurant via Google Places.
4. Sends RFP emails to those distributors over real SMTP.
5. Polls the inbox, parses replies with an LLM, builds a per-ingredient
   comparison matrix, auto-negotiates price matches, and recommends a
   multi-vendor optimal cart — one click to approve and dispatch POs.

Built for the take-home challenge but engineered like a small product:
PostgreSQL persistence, async background tasks, idempotent IMAP polling,
graceful fallbacks for missing USDA data.

---

## Architecture at a glance

```
┌──────────────────────────────────────────────────────────────────────┐
│                        React + Vite (port 5173)                      │
│   Profile · Menu Upload · Recipes/Forecast · Quotes · History        │
└─────────────────────────────────────────┬────────────────────────────┘
                                          │ axios
┌─────────────────────────────────────────┴────────────────────────────┐
│                      FastAPI (port 8000)                              │
│                                                                       │
│  api/menu          – upload, parse, recipe ingredient editing         │
│  api/procurement   – cycles, comparison, approve, history             │
│  api/ingredients   – pack-size overrides per restaurant               │
│  api/notifications – read/markup notifications                        │
│  api/admin         – USDA backfill / coverage diagnostics             │
│                                                                       │
│  agents/menu_parser     – GPT-4o text + vision parsing, auto-split    │
│  agents/scoring_engine  – optimal cart per ingredient, win-rate score │
│                                                                       │
│  services/usda_client      – FoodData Central (FDC) ID resolution     │
│  services/ams_pricing      – AMS Market News wholesale prices         │
│  services/places_discovery – Google Places (New) vendor discovery     │
│  services/email_daemon     – SMTP send + IMAP poll + LLM reply parse  │
│  services/pack_inference   – culinary qty → wholesale pack qty        │
└─────────────────────────────────────────┬────────────────────────────┘
                                          │ SQLModel
┌─────────────────────────────────────────┴────────────────────────────┐
│                       PostgreSQL                                      │
│   menus · dishes · recipes · ingredients · recipe_ingredients         │
│   ingredient_prices · distributors · procurement_cycles               │
│   distributor_quotes · distributor_quote_items · purchase_receipts    │
│   notifications · cycle_ingredients_needed · cycle_dish_forecasts     │
└──────────────────────────────────────────────────────────────────────┘

   external: OpenAI · USDA FDC · USDA AMS · Google Places · Gmail SMTP/IMAP
```

---

## How the 5 assignment steps map to the code

| Step | What it does | Key files | DB tables |
|---|---|---|---|
| **1. Menu → Recipes** | Extract text via PyMuPDF (no OCR), batch through GPT-4o-mini in parallel, recursive bisection retry on truncated JSON, canonicalize units. | `agents/menu_parser.py`, `agents/ingredient_units.py`, `api/menu.py` | `menus`, `dishes`, `recipes`, `ingredients`, `recipe_ingredients` |
| **2. USDA Pricing Trends** | POST to FDC `/foods/search` for stable food IDs; discover the right AMS report slug per commodity, follow multi-section reports into the right sub-section, drop discontinued slugs, normalise per-package and cents-denominated prices to honest $/lb. Sparkline + latest/avg badge surfaced on the recipes screen. | `services/usda_client.py`, `services/ams_pricing.py`, `api/admin.py` | `ingredients.usda_fdc_id`, `ingredient_prices` |
| **3. Find Local Distributors** | Google Places API (New) with progressive radius rings (10→20→30→50 mi) until ≥6 vendors found, deduped by name, persisted. | `services/places_discovery.py`, `api/procurement.py::_background_procurement` | `distributors`, `procurement_cycles` |
| **4. Send RFP Emails** | Real SMTP. HTML table with `Recipe Need · Order This · Delivery Window · Reference Benchmark` columns. Pack-aware quantities (`#10 can`, `5-lb bag`). Plus-addressed routing so demo replies all land in one Gmail. | `services/email_daemon.py::send_rfp_email`, `services/pack_inference.py`, `services/usda_client.py::build_benchmarks` | `distributor_quotes`, `cycle_ingredients_needed` |
| **5. Collect & Compare** | IMAP poller every 30s. GPT-4o parses replies into structured prices. Per-ingredient comparison matrix. Auto-trigger price-match emails to losing vendors when all RFPs are in. Multi-vendor optimal-cart approval. Decline detection ("out of stock"). Order history with per-vendor PO breakdown + invoice request. | `services/email_daemon.py`, `agents/scoring_engine.py`, `api/procurement.py` | `distributor_quote_items`, `purchase_receipts`, `notifications` |

---

## Local setup

### 1. Postgres

```bash
# macOS
brew install postgresql@16 && brew services start postgresql@16
createdb heavenlysourcing
```

### 2. Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # fill in the keys (see below)
alembic upgrade head        # creates all tables
uvicorn main:app --reload   # http://localhost:8000
```

### 3. Frontend

```bash
cd frontend
npm install
cp .env.example .env.local  # set VITE_API_URL if backend isn't on localhost:8000
npm run dev                 # http://localhost:5173
```

---

## Environment variables

| Var | Purpose | Where to get one |
|---|---|---|
| `DATABASE_URL` | Postgres connection | `postgresql://user:pass@localhost:5432/heavenlysourcing` |
| `OPENAI_API_KEY` | Menu parsing, quote parsing, recommendation text | <https://platform.openai.com/api-keys> |
| `USDA_API_KEY` | FoodData Central FDC IDs | <https://fdc.nal.usda.gov/api-key-signup.html> (free) |
| `AMS_API_KEY` | Market News wholesale prices | <https://mymarketnews.ams.usda.gov/mymarketnews-api/api-key> (free) |
| `GOOGLE_PLACES_API_KEY` | Distributor discovery | Google Cloud Console — enable **Places API (New)** AND **Geocoding API** on the project, then create a key with access to both |
| `SMTP_USER` / `SMTP_PASSWORD` | Outbound RFP / PO emails | Gmail address + app password (16-char) |
| `IMAP_USER` / `IMAP_PASSWORD` | Inbound vendor reply polling | Same Gmail account is fine; same app password works for IMAP |
| `CORS_ALLOWED_ORIGINS` | Comma-separated list of frontend URLs | Defaults to `http://localhost:5173` |

For a Gmail-based demo, **plus-addressing** is used to route replies per vendor:
sending to `you+RoyalFood@gmail.com` lands in `you@gmail.com` and lets the IMAP
poller correlate replies back to the right RFP.

---

## End-to-end demo (≈6 minutes)

1. **Profile** → name + Dallas zip (`75201` is good — populated AMS region + dense distributor coverage).
2. **Menu** → drop a PDF. The animated cooking-loader rotates quips while parallel GPT-4o calls run (1 page per call, 8 parallel max). Lands on Recipes when done; USDA backfill happens in a daemon thread.
3. **Recipes** → expand any cheese-heavy dish. The mozzarella row should show a `$/lb USDA` badge with a green sparkline (data pulled live from AMS slug `1083 - Cheese - Central U.S.`). Hover an ingredient to edit / delete inline; click `+ Add ingredient` to fix anything the LLM missed.
4. **Forecast** → set portions per dish, click **Start Procurement Cycle**. Land on `/quotes` with a "Finding local distributors near 75201…" spinner that flips to "6 distributors found, RFPs sent" within ~10 seconds.
5. **Inbox** → 6 emails arrive. Reply to 3 of them with prices, and 1 with "out of stock for everything." Within ~60s the IMAP poller parses them; the comparison matrix populates with the cheapest vendor per ingredient highlighted in green, plus a notification fires for the decline.
6. **Negotiate** → once all RFPs are answered or declined, the system auto-fires bargaining emails to vendors who lost on items where someone else came in cheaper. ("You're winning these. Match these others and we'll send you the whole basket.")
7. **Approve** → click **Approve Optimal Cart**. POs split per winning vendor, fire individual confirmation emails, mark losers as `DECLINED`, cycle flips to `AWAITING_RECEIPT`.
8. **Order History** → on `/procurement`, click the order row → expanded panel shows per-vendor PO with items, totals, invoice status, and a "Request invoice" button for vendors who haven't sent one back yet.

---

## API surface

All endpoints live under `/api`.

### Menu / recipes
- `POST /menu/upload` — accepts a base64 PDF or image; PDFs queue a background job
- `GET  /menu/upload/status/{job_id}` — poll for parsing progress + result
- `GET  /menu/recipes/with-prices` — recipes with ingredient list + USDA price summary
- `POST /menu/dishes/{dish_id}/ingredients` — add an ingredient to a dish
- `PATCH /menu/recipe-ingredients/{ri_id}` — edit name/qty/unit (swaps to a different `Ingredient` if identity changes; never mutates the shared row)
- `DELETE /menu/recipe-ingredients/{ri_id}` — remove from this dish only

### Procurement
- `POST /procurement/cycle/initiate` — body: `{dish_forecasts: {dish_id: portions}}`
- `GET  /procurement/cycle/active` — current cycle status + per-vendor quotes
- `GET  /procurement/cycle/active/comparison` — ingredient × vendor matrix + optimal cart
- `POST /procurement/cycle/active/approve-optimal` — split PO across winning vendors
- `GET  /purchase-history` — completed cycles
- `GET  /purchase-history/{cycle_id}` — per-vendor breakdown for one cycle
- `POST /purchase-history/{cycle_id}/vendors/{distributor_id}/request-receipt` — chase up missing invoice

### Admin / diagnostics
- `GET  /admin/usda/coverage` — counts of ingredients with FDC IDs / price rows + samples of unmapped / mapped-but-empty names
- `POST /admin/usda/backfill` — retry USDA enrichment for any NULL ingredients (`{"force": false, "background": false}`)
- `POST /admin/usda/reset-caches` — drop in-memory AMS report / slug / negative caches so the next backfill rediscovers from scratch

### Other
- `GET/POST /profile` — restaurant profile
- `GET /notifications`, `GET /notifications/recent`, `POST /notifications/{id}/read`
- `GET/PATCH /ingredients/{id}/pack` — per-restaurant pack-size override

---

## Database schema (key tables)

```
restaurant_profiles
└── menus
    └── dishes            (active flag for soft-delete)
        └── recipes
            └── recipe_ingredients   →  ingredients
                                       ├── usda_fdc_id (FoodData Central)
                                       ├── pack_*_override (per-restaurant)
                                       └── ingredient_prices   (AMS observations)

procurement_cycles                   (status: DISCOVERING_DISTRIBUTORS → COLLECTING_QUOTES → AWAITING_RECEIPT → COMPLETED)
├── cycle_dish_forecasts             (qty per dish for this week)
├── cycle_ingredients_needed         (rolled-up ingredient demand + pack plan)
├── distributor_quotes               (one per RFP; status: PENDING → RECEIVED → APPROVED/DECLINED)
│   └── distributor_quote_items      (per-ingredient prices; upserted on each reply)
└── purchase_receipts                (invoices parsed from vendor replies)

distributors                         (one per discovered vendor; google_place_id + demo_routing_email)
notifications                        (audit feed for the bell icon)
```

All tables are managed via Alembic (`backend/alembic/versions/`).

---

## Design decisions worth calling out

**LLM robustness.** The menu parser dispatches one page per GPT-4o call, in
parallel up to 8 at a time. If a response truncates or returns malformed JSON,
`_extract_dishes_lossy` salvages well-formed dish objects from the partial
output, and `_parse_menu_text_with_autosplit` recursively bisects the chunk and
retries — no silent data loss.

**Honest USDA labelling.** `build_benchmarks` resolves prices in 3 tiers and
the **same tiering is now surfaced on the Recipes page**, not just inside RFP
emails:
1. real USDA AMS Market News — green `USDA` badge, `$X.XX/lb · avg $Y.YY/lb`,
   sparkline if there are ≥2 historical points.
2. industry estimates — amber `industry est · <tag>` badge, `~$X.XX/<unit>`.
   Resolved in two passes:
     a. **Per-ingredient override** — name-keyed table (`_INGREDIENT_OVERRIDES`)
        for high-volume items whose natural unit isn't mass-based, e.g.
        Pizza Dough `$0.60/each`, Pizza Sauce `$0.06/fl oz`, BBQ / Ranch /
        Alfredo Sauce `$0.09–0.13/fl oz`. Substring-matched, longest key wins.
     b. **Category midpoint** — 8 hard-coded `$/lb` averages
        (`_CATEGORY_BENCHMARK_PER_LB`, e.g. Dairy $4.50, Proteins $6.00,
        Produce $2.50). Fires only when the recipe unit is mass-compatible
        (`lb / oz / g / kg`) so a $/lb number never appears next to fl-oz
        sauces or per-each items.
   Both passes are NOT USDA-sourced — the badge color and `~` prefix make
   this visually unambiguous, and the rendered label carries an explicit
   `(industry est, <tag>)` suffix.
3. `no USDA data` — slate grey, no number. Used for fl-oz sauces / each-unit
   bottles where a $/lb estimate would be more misleading than helpful.

The API response carries them in two separate fields so the frontend can
choose which to render: `usda_price` (tier 1, with series + latest + avg)
and `usda_estimate` (tier 2, only populated when tier 1 is empty).

**AMS report extraction quirks.** `services/ams_pricing.py` papers over three
real things AMS does that aren't documented:

1. *Multi-section reports.* The bare `/reports/{slug}` URL returns only the
   `Report Header` rows for everything outside the dairy slugs 1082–1085 /
   1092 — no commodity, no price. The actual price rows live in a sibling
   section whose name varies by family (`Report Details`, `Report Detail`,
   `Report Detail Simple`, `Report Metrics`, …). `_fetch_report_body`
   inspects `reportSections`, ranks the non-header sections (detail → price
   → metric → volume), and refetches the first one that carries price
   columns. It also appends `lastDays=30` because AMS *ignores* `limit` on
   section endpoints — an un-filtered Atlanta Vegetables fetch is 124 MB.
2. *Discontinued reports float to the top.* AMS leaves dead reports in the
   listing with `(Discontinued)` in the title. Those scored higher than the
   active equivalents for produce keywords (`wholesale` bonus) and filled
   the entire 20-slot candidate cap. `_candidate_reports_for` now drops
   anything whose blob contains `discontinued`.
3. *Three different price denominations.* Dairy quotes `Dollars per Pound`.
   Poultry / eggs quote `Cents Per Lb` / `Cents Per Dozen`. Terminal Markets
   quote per package with no unit field at all (`5 kg/11 lb flats`,
   `40 lb cartons`, `1 1/9 bushel cartons`). `_extract_rows` detects each
   case — converts cents → dollars, parses pounds out of `package` (with
   commodity-specific bushel fallbacks), and *drops* rows whose package
   weight is unrecoverable rather than storing a wrong-by-10× $/lb.
   `unit_override` on the extracted row forces the storage unit to `lb`
   whenever a per-package divisor was applied, so the UI never shows
   `$2.71/each` for a per-30-lb-carton pineapple price.

**Pack inference.** `services/pack_inference.py` translates "10 fl oz of pizza
sauce" into "1 #10 can (~104 fl oz)" before sending the RFP, so vendors quote
on units they actually sell. Per-restaurant overrides supported via
`PATCH /api/ingredients/{id}/pack`.

**Optimal cart, not best vendor.** `agents/scoring_engine.py::build_optimal_cart`
picks the cheapest vendor per ingredient. The "score" surfaced in the UI is a
win-rate (items won / items quoted), not an opaque composite.

**Auto-negotiation.** `services/email_daemon.py::_autotrigger_price_match` only
fires after every RFP is `RECEIVED` or `DECLINED`. It groups items per vendor
into "winning" vs "losing" lists and sends a bargaining email asking the loser
to match the competitor or offer an overall discount on the consolidated basket.

**IMAP self-loop guard.** Plus-addressing causes our own outbound mail to land
back in the inbox. `_is_self_sent` + `_is_our_outbound_subject` together drop
those before the LLM ever sees them, so we don't ghost-create quotes from our
own RFP HTML.

**Decline detection.** When a vendor replies with no prices, `_detect_decline_signal`
keyword-matches phrases like "out of stock," "regret to inform,"
"cannot fulfill" — marks the quote `DECLINED` and creates a notification with a
~200-char excerpt. Empty/unparseable replies still create a "no prices found"
notification so nothing goes silent.

**Atomic edits without bleed.** `Ingredient` rows are shared across dishes;
`RecipeIngredient` is the per-dish join row. Editing the name/unit/category on
one dish never mutates the shared `Ingredient` — it finds-or-creates a new one
and re-points the join row. Quantity-only edits update only the join row.

---

## Project layout

```
heavenly-sourcing/
├── backend/
│   ├── agents/
│   │   ├── menu_parser.py          # GPT-4o menu extraction + auto-split retry
│   │   ├── ingredient_units.py     # canonicalize qty/unit
│   │   └── scoring_engine.py       # optimal cart + win-rate score + recommendation prompt
│   ├── api/
│   │   ├── menu.py                 # upload, recipes, ingredient editing
│   │   ├── procurement.py          # cycles, comparison, approve, history
│   │   ├── ingredients.py          # pack overrides
│   │   ├── notifications.py
│   │   ├── admin.py                # USDA backfill / coverage
│   │   └── profile.py
│   ├── services/
│   │   ├── usda_client.py          # FDC search + benchmark resolution
│   │   ├── ams_pricing.py          # AMS Market News discovery + extraction
│   │   ├── places_discovery.py     # Google Places (New) progressive radius
│   │   ├── email_daemon.py         # SMTP + IMAP + LLM reply parsing
│   │   └── pack_inference.py       # culinary → wholesale pack translation
│   ├── models/                     # SQLModel tables
│   ├── alembic/versions/           # DB migrations
│   ├── main.py                     # FastAPI app + IMAP scheduler lifespan
│   └── requirements.txt
├── frontend/
│   └── src/
│       ├── components/
│       │   ├── ProfileSetup.jsx
│       │   ├── MenuUpload.jsx          # animated cooking loader
│       │   ├── RecipeAccordion.jsx     # forecast + inline ingredient editing
│       │   ├── QuoteTracker.jsx        # active cycle + comparison
│       │   ├── ComparisonMatrix.jsx    # ingredient × vendor matrix
│       │   ├── PurchaseHistory.jsx     # clickable order detail + invoice ping
│       │   ├── NotificationBell.jsx
│       │   └── NotificationToast.jsx
│       ├── App.jsx
│       └── main.jsx
└── README.md
```

---

## Stack

- **Backend:** FastAPI 0.104, SQLModel 0.0.14, PostgreSQL, Alembic, APScheduler
- **LLM:** OpenAI GPT-4o (vision + text) and GPT-4o-mini (structured extraction)
- **PDF:** PyMuPDF for native-text extraction (vision fallback for scanned PDFs)
- **Email:** stdlib `smtplib` + `imaplib` (Gmail app passwords)
- **External APIs:** USDA FoodData Central, USDA AMS Market News, Google Places (New)
- **Frontend:** React 18, Vite 5, Tailwind 3, react-toastify, recharts

---

## License

MIT — see header in source files where present.
