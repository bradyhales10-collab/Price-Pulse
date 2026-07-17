# Part Pulse

Production foundation for a controlled Partzilla browser probe for Kawasaki OEM parts.

This step does not scrape prices, authenticate, bypass access controls, run a crawler, or schedule jobs. It opens one selected product page and saves diagnostics so the site behavior can be reviewed before parser work begins.

## Windows Setup

From PowerShell:

```powershell
Set-Location "C:\path\to\partzilla-pricing-monitor"
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

If `py -3.12` is not available, use:

```powershell
python -m venv .venv
```

## Input CSV

Place the input file here:

```text
data/input/Partzilla_Kawasaki_Test_Parts.csv
```

The loader trims surrounding whitespace, preserves OEM part numbers exactly, rejects blank manufacturers, and rejects blank part numbers. The search-observed product name and MSRP are treated only as test hints.

## Run Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The test configuration disables pytest's cache, and the tests use checked-in fixture CSVs instead of temporary files. This avoids Windows profile temp-folder permission issues.

## Local Dashboard

The Part Pulse dashboard is a local web interface for uploading part files, starting controlled price checks, and reviewing the SQLite database. It does not display authentication state or manage credentials.

Install dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Start the dashboard:

```powershell
.\.venv\Scripts\python.exe dashboard.py --database data/database/pricing_monitor_regression.db
```

Open:

```text
http://127.0.0.1:8000
```

Stop it with:

```text
Ctrl+C
```

The current phase includes dashboard, product catalog, price check, price comparison, scan runs, and data-quality pages. Review queue, pricing rules, and settings are shown as future navigation items.

### Dashboard Workflow

Start the local dashboard:

```powershell
Set-Location "C:\path\to\partzilla-pricing-monitor"
.\.venv\Scripts\python.exe dashboard.py --database data/database/pricing_monitor.db
```

Open:

```text
http://127.0.0.1:8000
```

Use `Price Check` to upload an `.xlsx` or `.csv` file. When the headers are recognized, the page shows how many part rows were read, how many are valid, and whether anything needs fixing. Macro-enabled workbooks, executables, zip files, and oversized uploads are rejected.

Required import columns:

```text
Internal_SKU
Manufacturer
OEM_Part_Number
Our_Current_Price
```

Optional import columns:

```text
Product_Name
Calc_Cost
Product_Category
Units_Sold_12M
Inventory_Qty
Scan_Priority
Is_Active
```

If the preview looks correct, click `Start Checking Prices`. The dashboard confirms the import, starts a controlled authenticated collection, and shows live status with checked count, remaining count, recent part results, and an ETA. The browser opens visibly by default and uses lightweight loading that skips images, fonts, and media while keeping page scripts and product information available. If the login expires, run `auth_bootstrap.py` again to sign in visibly and refresh `data/private/partzilla_auth_state.json`.

Use `Start Fresh` on the Price Check page to remove all uploaded products, competitor results, review decisions, scan runs, and import history before testing a new sheet. Login state, competitor configuration, and pricing rules are preserved.

Use `Price Comparison` after collection. It compares our current price to the latest stored Partzilla state, calculates dollar difference, percentage difference, our gross margin, and margin at the Partzilla price. `Export All Results` and `Export Selected Results` create a review workbook under:

```text
data/output/exports/
```

The review workbook intentionally leaves `Suggested_New_Price` blank and sets `Review_Status` to `Pending Review`.

## Run One Probe

Default mode opens visible Chromium:

```powershell
.\.venv\Scripts\python.exe run_probe.py --part-number 41080-1514
```

Optional arguments:

```powershell
.\.venv\Scripts\python.exe run_probe.py --part-number 41080-1514 --headless
.\.venv\Scripts\python.exe run_probe.py --part-number 41080-1514 --slow-mo 250 --timeout 45000
```

## Inspect One Product

This command opens one explicitly selected product page, classifies the page, extracts public product data, and writes a structured `observation.json`:

```powershell
.\.venv\Scripts\python.exe inspect_product.py --part-number 41080-1514
```

Inspector output is saved under a timestamped folder:

```text
data/output/diagnostics/<timestamp>_<part-number>/
```

Each folder contains:

```text
screenshot.png
rendered.html
diagnostics.txt
observation.json
```

## Controlled Authenticated Feasibility Check

Authenticated browser state is sensitive. The file below is treated like a password, is stored only locally, and is excluded by `.gitignore`:

```text
data/private/partzilla_auth_state.json
```

Bootstrap a session by signing in manually in the visible browser window:

```powershell
.\.venv\Scripts\python.exe auth_bootstrap.py
```

Then inspect exactly one selected product with that saved session:

```powershell
.\.venv\Scripts\python.exe inspect_authenticated.py --part-number 41080-1514
```

For price-forensics validation, run the authenticated inspection with manual confirmation:

```powershell
.\.venv\Scripts\python.exe inspect_authenticated.py --part-number 41080-1514 --manual-price-confirmation --debug-raw-price-signals
.\.venv\Scripts\python.exe inspect_authenticated.py --part-number 55061-5438-739 --manual-price-confirmation --debug-raw-price-signals
```

Authenticated diagnostics are intentionally limited to:

```text
data/output/authenticated_diagnostics/<timestamp>_<part-number>/observation.json
data/output/authenticated_diagnostics/<timestamp>_<part-number>/sanitized_diagnostics.txt
data/output/authenticated_diagnostics/<timestamp>_<part-number>/price_evidence.json
data/output/authenticated_diagnostics/<timestamp>_<part-number>/raw_price_signals.json
```

The older DOM debug files remain optional with `--debug-price-dom`.

## Five-Part Authenticated Validation

Run each authenticated validation case one at a time from normal PowerShell:

```powershell
.\.venv\Scripts\python.exe validate_authenticated.py --part-number 41080-1514
.\.venv\Scripts\python.exe validate_authenticated.py --part-number 55061-5438-739
.\.venv\Scripts\python.exe validate_authenticated.py --part-number K53001-240
.\.venv\Scripts\python.exe validate_authenticated.py --part-number 14081-005
.\.venv\Scripts\python.exe validate_authenticated.py --part-number 92071-2128
```

After all five finish, send back:

```text
data/output/authenticated_validation_summary.csv
data/output/authenticated_validation_review.txt
```

## SQLite Data Layer

The durable database lives at:

```text
data/database/pricing_monitor.db
```

SQLite files and temporary SQLite sidecar files are ignored by Git. Money is stored as integer cents, not floating-point numbers, so values like `$282.32` are stored as `28232` and can be compared without rounding drift.

The database separates audit events from meaningful history:

- `scan_events` records every attempted product check, including failures and no-change checks.
- `listing_history` records only meaningful monitored changes: first observation, price, availability, supersession, or multiple changes.

Initialize the database:

```powershell
.\.venv\Scripts\python.exe initialize_database.py
```

Import the product master:

```powershell
.\.venv\Scripts\python.exe import_products.py --file data/input/Partzilla_Kawasaki_Test_Parts.csv --dry-run
.\.venv\Scripts\python.exe import_products.py --file data/input/Partzilla_Kawasaki_Test_Parts.csv
```

Import verified authenticated validation results:

```powershell
.\.venv\Scripts\python.exe import_validation_results.py --file data/output/authenticated_validation_summary.csv
```

Inspect and export:

```powershell
.\.venv\Scripts\python.exe inspect_database.py
.\.venv\Scripts\python.exe inspect_database.py --part-number 41080-1514
.\.venv\Scripts\python.exe export_current_prices.py
.\.venv\Scripts\python.exe export_price_changes.py
```

Save one manually initiated live authenticated validation result to the database:

```powershell
.\.venv\Scripts\python.exe validate_authenticated.py --part-number 41080-1514 --save-to-database
```

## Controlled 25-Part Collection

`collect_parts.py` runs a manually initiated authenticated collection over an explicit input CSV. It does not scan every listing in the database, does not schedule itself, and does not retry failed products automatically.

The collector:

- Requires an explicit input file and `--max-parts`.
- Matches CSV parts to existing database products/listings before opening a browser.
- Requires the user to type `RUN` before creating a live scan run.
- Reuses one visible authenticated Chromium context.
- Processes products sequentially with a conservative delay.
- Persists each product immediately in its own transaction.
- Stops on blocked/challenge/authentication-loss access conditions.
- Keeps scan events separate from listing history, so unchanged checks do not create false price history.

Dry run:

```powershell
.\.venv\Scripts\python.exe collect_parts.py --file data/input/Partzilla_Kawasaki_Test_Parts.csv --max-parts 25 --dry-run
```

Live controlled run:

```powershell
.\.venv\Scripts\python.exe collect_parts.py --file data/input/Partzilla_Kawasaki_Test_Parts.csv --max-parts 25 --save-to-database
```

Optional pacing:

```powershell
.\.venv\Scripts\python.exe collect_parts.py --file data/input/Partzilla_Kawasaki_Test_Parts.csv --max-parts 25 --save-to-database --delay-seconds 10
```

You can also pass the production competitor explicitly:

```powershell
.\.venv\Scripts\python.exe collect_parts.py --competitor partzilla --file data/input/Partzilla_Kawasaki_Test_Parts.csv --max-parts 25 --save-to-database
```

## Multi-Competitor Framework

The project now has a competitor adapter framework under `app/competitors/`.

Competitor statuses:

- `active`: production collection path is available. Partzilla is the baseline active competitor.
- `experimental_probe`: feasibility testing only. MotoSport is currently experimental and is not used for production pricing decisions.
- `disabled`: reserved for competitors that should not be used.

Partzilla remains the proven production collector. MotoSport has only a feasibility probe adapter.

Run the MotoSport probe manually with:

```powershell
.\.venv\Scripts\python.exe probe_competitor.py --competitor motosport --file data/input/MotoSport_Probe_Parts.csv --max-parts 6
```

Run the 25-part multi-OEM MotoSport coverage probe manually with:

```powershell
.\.venv\Scripts\python.exe probe_competitor.py --competitor motosport --file data/input/MotoSport_25_Part_Probe.csv --max-parts 25
```

Probe outputs are written under:

```text
data/output/competitor_probes/motosport/<timestamp>/
```

MotoSport probe behavior:

- manual only
- default max 10 parts
- hard max 25 parts
- minimum 5 second delay
- sequential requests only
- no production `current_listing_state` or `listing_history` updates by default
- optional `--save-probe-to-database` writes only to `competitor_probe_results`
- normal product-page probes do not add items to cart

MotoSport pages may show `See Price in Cart`. In that case, the visible crossed-out amount is treated as a reference/list value only. It is not stored as `selling_price`, is not used as a competitor selling price, and is excluded from lowest-competitor calculations.

### Experimental MotoSport Cart Price Probe

Cart price probing is separate from the normal MotoSport probe and remains experimental. It is disabled unless explicitly enabled with both a flag and exact confirmation text.

Export cart-hidden rows from the latest safe MotoSport product-page probe:

```powershell
.\.venv\Scripts\python.exe export_cart_hidden_probe_input.py --competitor motosport --latest
```

This creates:

```text
data/input/MotoSport_Cart_Hidden_Probe.csv
```

Run the experimental cart-price probe manually:

```powershell
.\.venv\Scripts\python.exe probe_cart_price.py --competitor motosport --file data/input/MotoSport_Cart_Hidden_Probe.csv --max-parts 5 --experimental-cart-pricing
```

For the first two known cart-hidden MotoSport pages, use:

```powershell
.\.venv\Scripts\python.exe probe_cart_price.py --competitor motosport --file data/input/MotoSport_Known_Cart_Hidden_Probe.csv --max-parts 2 --experimental-cart-pricing
```

When prompted, the exact confirmation text is required:

```text
RUN CART PRICE PROBE
```

Cart-price probe safety limits:

- experimental only; results are not written to production `current_listing_state`
- hard max 5 products
- one item at a time
- no checkout
- no shipping information
- no payment information
- no account creation or login
- no proxies, stealth tools, fingerprint spoofing, CAPTCHA bypass, or security bypass
- stop on block, challenge, CAPTCHA, login wall, 403, 429, unexpected cart behavior, or access-warning page
- cleanup is required after every item
- if cleanup fails, the probe stops

Cart price probing should not be used beyond limited testing until reviewed and approved. MotoSport is seeded with `cart_price_probe_status = legal_review_needed`.

The dashboard Price Check page shows competitor selection. Partzilla and MotoSport can be selected independently; selected competitors are checked against the uploaded parts list and saved to production price history.

## Future Internal API Readiness

The internal source abstraction lives under `app/internal_sources/`.

- Excel and CSV uploads remain the current production internal product-data path.
- `api_source.py` is a placeholder only.
- No API credentials are implemented.
- Future API tokens should come from environment variables or a secure secrets mechanism, not SQLite.
- Internal product state now tracks source metadata separately from comparison logic.

Collection outputs are saved under:

```text
data/output/collection_runs/<scan_run_id>/
```

Each run writes:

```text
collection_summary.csv
collection_review.txt
run_metadata.json
```

After a run, inspect the database:

```powershell
.\.venv\Scripts\python.exe inspect_database.py
```

If authentication is lost, rerun `auth_bootstrap.py` manually before starting a new collection run.

Do not share `data/private/partzilla_auth_state.json`.

## Step 3 Five-Part Validation

The Step 3 helper validates one explicitly selected product at a time. It does not loop through all five products.

Run each command manually from PowerShell:

```powershell
.\.venv\Scripts\python.exe validate_step3.py --part-number 41080-1514
.\.venv\Scripts\python.exe validate_step3.py --part-number 55061-5438-739
.\.venv\Scripts\python.exe validate_step3.py --part-number K53001-240
.\.venv\Scripts\python.exe validate_step3.py --part-number 14081-005
.\.venv\Scripts\python.exe validate_step3.py --part-number 92071-2128
```

After running the five commands, send back:

```text
data/output/step3_validation_summary.csv
data/output/step3_validation_review.txt
```

Only send individual `observation.json` files for cases that show warnings, low confidence, blocks, challenges, not-found pages, or navigation errors.

## Diagnostics Output

Each probe saves timestamped diagnostics under:

```text
data/output/screenshots/
data/output/html/
data/output/diagnostics/
data/output/logs/
```

After the first probe, send back:

- The newest `.txt` file from `data/output/diagnostics/`
- The matching `.html` file from `data/output/html/`
- The matching `.png` screenshot from `data/output/screenshots/`
- `data/output/logs/partzilla_probe.log`
