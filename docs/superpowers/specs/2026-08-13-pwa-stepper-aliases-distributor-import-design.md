# PWA Stepper + Aliases Admin + Distributor Excel Import — Design

**Date:** 2026-08-13
**Status:** Approved (user, 2026-08-13)
**Scope:** PWA deduction flow, parts aliases admin, distributors + Excel catalog import

## Objective

Three coordinated changes to make deductions intentional (Confirm-only), aliases
manageable in the owner panel (and visible in the PWA), and the system able to
absorb distributor catalogs (Service Spring first, others later) with a canonical
SKU that does not depend on any single distributor's numbering.

## Background

The PWA currently deducts on every `-1` tap, with no way to back out without
deducting. The owner panel can add one alias at a time, with no display or
removal of existing aliases. Distributors are hardcoded to five placeholders
(`dist_a..dist_d`, `amazon`); there is no UI to add a real distributor, and
no way to bulk-import a vendor's catalog (Service Spring, a garage-door parts
distributor, delivered `Service_Spring Excel.xlsx` with 35 components and
purchase history through Aug 2026).

The user requires:

1. Defer deductions until the green Confirm button is pressed; allow backing
   out with a discard-confirmation dialog. The deduction queue must remain
   robust across hours or days offline.
2. Manage **multiple aliases** per canonical part in the admin UI (bulk add,
   show list, remove individual), so the tech can search by any distributor
   name and the system can later cross-link homologous products between
   distributors.
3. Import a distributor's Excel catalog (`Service_Spring Excel.xlsx` first;
   other distributors expected) as a reusable flow.

## Design

### 1. PWA stepper + Confirm (required)

**Detail view layout (single screen, dark theme preserved):**

```
┌──────────────────────────────────┐
│ ← back        Part Name           │
│                                   │
│ Truck stock: 10 · [OK]            │
│                                   │
│ To deduct:                        │
│   [ − ]   [   1   ]   [ + ]      │
│                                   │
│ Will deduct 1 · New stock: 9     │
│                                   │
│           [ ✓ Confirm ]           │
└──────────────────────────────────┘
```

**Behavior:**

- `openPart(partId)` sets `current = part`, `stagedQty = 1`, renders the stepper.
- `−` button: `stagedQty = max(1, stagedQty - 1)`. Visually disabled at qty=1
  (`disabled` attribute + opacity 0.4).
- `+` button: `stagedQty = stagedQty + 1`. No upper bound.
- Number input: editable, `type="number" min="1"`. On `blur`/`change`, clamp to
  integer ≥ 1. On `input`, also re-render the "Will deduct" line.
- `stagedQty` only affects the UI; it does **not** touch `current.stock`,
  `queueDeduction`, or `syncNow`.
- Confirm (green): if `stagedQty > 0` → `current.stock -= stagedQty`;
  `queueDeduction(current, stagedQty)` (single batch entry, qty = full staged
  amount); `syncNow()`; `showCatalog()`. If `stagedQty == 0` (defensive) →
  just `showCatalog()`. After commit, `stagedQty = 0`.
- Back arrow: if `stagedQty > 1` → `confirm("Discard deduction of N units?")`
  (native iOS dialog). Yes → `showCatalog()` (discard). No → stay on detail.
  If `stagedQty == 1` (default) → `showCatalog()` with no prompt (nothing to
  discard). The "dirty" rule is simply `stagedQty > 1`, which captures all
  meaningful interaction without tracking an extra flag.
- The Confirm button is always enabled; pressing it with the default qty=1 is
  a valid one-unit deduction.

**Offline robustness (already in place, not changed):** the IDB queue + the
localStorage mirror + 2s retry + SW background sync (Chrome/Android only) cover
multi-day offline use. The stepper does not weaken this; it only stages the
UI decision.

**Cache-bust:** bump SW to `v13`, SHELL assets to `?v=13`, and `index.html` refs
to `?v=13`. The iOS SW-cache gotcha (ignoreSearch + old SW active until
force-close) still applies — the user must force-close and reopen the PWA on
iPhone after deploy.

### 2. Aliases admin + PWA visibility

`parts.aliases` (JSON text) and the `scorePart` search loop already exist
(`tech_pwa/app.js` iterates `[sku, name, ...aliases]` with exact-match=100 /
includes=85 / Levenshtein=60). The catalog endpoint already parses and returns
`aliases`. What's missing is admin UX and PWA visibility.

**New server endpoints (form posts, owner-only):**

- `POST /dashboard/parts/bulk-add-alias` (form: `part_id`, `aliases_csv`).
  Splits on commas, trims, drops empties, dedupes against existing aliases,
  appends, saves.
- `POST /dashboard/parts/remove-alias` (form: `part_id`, `alias`). Removes
  one alias if present.

**New module helpers in `modules/parts.py`:**

- `bulk_add_aliases(conn, part_id, aliases_csv)` — returns the new alias list.
- `remove_alias(conn, part_id, alias)` — returns bool (removed?).

**Owner panel UI (`templates/dashboard_parts.html`):**

For each part row, inside the existing Edit `<details>`, replace the current
one-at-a-time "Add Alias" form with:

```
Aliases: [ red spring × ] [ 207 × ] [ torsion spring × ]
[ Add aliases (comma-separated) ____________ ] [ Save ]
```

Each chip is a small `<form method="post" action="/dashboard/parts/remove-alias">`
with hidden `part_id` and `alias`, button "×". The bulk-add input accepts a
comma-separated list and posts to `bulk-add-alias`. Aliases display only when
non-empty. The Edit toggle stays as-is.

**PWA detail view (`tech_pwa/app.js`):** beneath the part name, render a
subtle line `Also known as: a · b · c` only when `aliases.length > 0`. This
helps the tech confirm the right part and demonstrates aliases are active.
Sorted, deduped, escaped.

**Tests:** add `tests/test_aliases_admin.py` covering `bulk_add_aliases` and
`remove_alias` (add, dedupe, remove, remove-missing).

### 3. Dynamic distributors + Excel import

#### 3a. Drop the hardcoded distributor list

`modules/distributors.py` currently exports `DISTRIBUTOR_IDS = ("dist_a", ...,
"amazon")` and `import_distributor_csv` rejects unknown distributor IDs. This
blocks adding Service Spring (or any new vendor) without code edits.

Change:

- Remove the `DISTRIBUTOR_IDS` constant.
- `list_distributors(conn)` already reads from the table — keep it.
- `get_distributor(conn, distributor_id)` already reads from the table — keep it.
- `upsert_mapping`, `set_active`, `list_mappings_for_product`, `get_active_mapping`,
  `batch_set_active` — no changes needed (they take a distributor_id string).
- `import_distributor_csv` — relax the "unknown distributor" check to a warning
  (the distributor exists if there's a row). Keep the function for the existing
  CSV mapping import path; do not break it.
- `update_distributor` already takes any id — keep it.

**New dashboard CRUD (`templates/dashboard_distributors.html`):**

- List distributors with name + email + edit form (POST to
  `POST /dashboard/distributors/update`).
- "Create distributor" form (name required, email optional; POST to
  `POST /dashboard/distributors/create`).
- No delete (preserves history in `product_distributors`).
- Link from the parts page header.

#### 3b. Excel import

**Parser (`modules/excel_import.py`, new):**

- Function: `parse_service_spring_usage(xlsx_bytes) -> list[dict]` returning
  one dict per non-empty data row:
  `{category, part_no, description, total_qty, avg_unit_cost, last_purchase}`.
  Reads sheet "2026 Monthly Usage"; header is row 8; data rows 9..N where
  `Part #` is non-empty. Skips summary rows. `last_purchase` is the Excel
  date (or None).
- Function: `import_distributor_catalog(conn, distributor_id, rows) ->
  {created, updated, warnings}`:
  - Compute `counter = COUNT(parts) + 1` once at the start.
  - For each row:
    - Look up an existing `product_distributors` row by
      `(distributor_id, distributor_sku=part_no)`. If found → reuse its
      `product_id`.
    - Else create a new canonical part: id = `f"P-{counter:04d}"`,
      name = description, category = category, cost = avg_unit_cost,
      active = 1, aliases = []. Increment `counter`.
    - Merge aliases: add `part_no`; if `description != part_no` and
      `description != name` add `description`. Dedupe.
    - Upsert `product_distributors(sku=part_no, name=description,
      is_active=1)`. (is_active=1 because Service Spring is the active
      vendor for these newly-sourced parts. A follow-up homologation flow
      will handle "don't overwrite an existing active mapping" when a
      second distributor is imported.)
    - Track `created` (new canonical) vs `updated` (canonical existed,
      fields refreshed).
  - Warnings: row-level issues (empty part_no, duplicate in file, parse
    failure). Never abort on a bad row.
  - Commit at the end.
  - Idempotent: re-running updates without duplicating aliases or canonicals.

**Endpoint:** `POST /dashboard/parts/import-distributor-excel` (multipart,
owner-only). Accepts `distributor_id` (form) and `file` (.xlsx). Reads bytes,
calls parser + importer, redirects back to `/dashboard/parts` with a flash
`message` showing counts. Renders warnings/errors in the existing
`distributor_import_result` block on `dashboard_parts.html`.

**UI in `/dashboard/parts`:** new panel "Import Distributor Catalog":

```
[ Select distributor ▾ ]   [ Choose .xlsx ]   [ Import ]
```

Requires openpyxl (already a project dep — used by tests). Validate file
extension and content type.

**Homologation (cross-distributor linking) is deferred.** The aliases and
`product_distributors` infrastructure is enough to support it later: a second
distributor's `Part #` matching an existing alias on a canonical would let
the importer suggest "map to canonical P-0007?" with a confirmation step. We
will add that UI in a follow-up when the second distributor arrives. For now,
importing a second distributor with overlapping physical parts will create
new canonicals (and the operator can merge manually via the alias UI).

#### 3c. Why auto-generated canonical SKU (`P-NNNN`)

The user noted that every distributor numbers the same physical part
differently, and cross-distributor homologation will be the hard part. A
canonical SKU that derives from any single distributor's numbering would
couple our identity to that vendor and make homologation brittle. Auto-gen
decouples it; aliases carry the distributor's identifiers for tech search and
future matching. The `P-` prefix is short, sortable, and unambiguous in logs.

## Scope (in)

- Stepper UI + Confirm + back confirmation
- Alias chips + bulk-add + remove in admin
- PWA alias display
- Drop `DISTRIBUTOR_IDS`
- Distributor CRUD UI
- Service Spring Excel import (parser + endpoint + UI)

## Scope (out / deferred)

- Cross-distributor homologation UI (match-suggestion on import)
- CSV distributor mapping (legacy `import_distributor_csv`) — keep working,
  do not invest further
- Background sync (Chrome/Android) — already works
- PRD deferrals unchanged: C.3 pickup email; B.2.3 Dispatch; small-parts
  warehouse; warranty-line; monthly/weekly email pipeline
- Auto-deploy — user deploys manually in Coolify (unchanged)

## Files to touch

| File | Change |
|---|---|
| `tech_pwa/app.js` | stepper render, back confirmation, alias display |
| `tech_pwa/app.css` | stepper / chip styles |
| `tech_pwa/index.html` | bump refs to `?v=13` |
| `tech_pwa/sw.js` | `VERSION='v13'`, SHELL `?v=13` |
| `modules/distributors.py` | remove `DISTRIBUTOR_IDS`; relax csv check |
| `modules/parts.py` | `bulk_add_aliases`, `remove_alias` |
| `modules/excel_import.py` (new) | parser + importer |
| `main.py` | new endpoints (distributors CRUD, aliases, excel import) |
| `templates/dashboard_distributors.html` (new) | CRUD UI |
| `templates/dashboard_parts.html` | alias chips, import panel, distributors link |
| `tests/test_aliases_admin.py` (new) | alias add/remove |
| `tests/test_distributor_import.py` (new) | parser + importer |

## Risks

- **iOS SW caching**: bump v13 + user force-closes/reopens PWA on iPhone.
- **Excel variability**: parser must tolerate header row drift, blank rows,
  summary rows, and missing optional columns. Warnings over aborts.
- **Distributor rigidity legacy**: removing `DISTRIBUTOR_IDS` could affect
  other call sites. Mitigation: greppable search; only `import_distributor_csv`
  references it, and we relax that path with a warning instead of an error.
- **Canonical SKU collisions on re-import**: idempotency via
  `product_distributors(distributor_id, distributor_sku)` lookup before
  allocating a new `P-NNNN`.

## Acceptance criteria

- PWA: opening a part shows qty=1; − is disabled; tapping + then − returns
  to 1 with no confirm prompt on back; tapping + to 3 then back prompts
  "Discard deduction of 3 units?"; Confirm deducts 3 and returns to catalog.
- Admin: a part with 2 aliases shows two chips; bulk-add of "x, y, x" adds
  only "x" and "y" (deduped); × on a chip removes it; PWA detail shows the
  alias line after a fresh catalog fetch.
- Service Spring import of the 35-row Excel: 35 created, 0 warnings;
  distributor "Service Spring" appears in the list; 35 `product_distributors`
  rows; each canonical has `Part #` in its aliases; catalog served to PWA
  includes aliases.
- Re-importing the same file: 0 created, 35 updated, 0 warnings.
- Tests: `pytest tests/` green; `node --check tech_pwa/app.js`,
  `node --check tech_pwa/sw.js` clean.