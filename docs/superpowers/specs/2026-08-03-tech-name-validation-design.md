# Tech Name Validation + Edit + Usage by Tech

Date: 2026-08-03

## Problem

1. **Tech names auto-assign on fuzzy match.** `modules/techs.py:find_tech_by_name`
   auto-assigns any match with ratio >= 0.5 with no confirmation gate. When an
   invoice contains "Nick Hussing" but the roster has "Nick" and "Nicholas", the
   wrong tech can be picked silently. The chosen name is baked into
   `unmatched_parts.tech_name` at ingest, so it's locked in before the user
   resolves anything.
2. **No way to edit a tech's name.** `modules/techs.py:update_tech` only updates
   `truck_id`/`warehouse_id`; the Edit form in `templates/dashboard_techs.html`
   has only a truck dropdown.
3. **Usage is truck-only.** `modules/analytics.py` groups by truck; there is no
   way to see which technician used parts.

## Design

### 1. Tech name validation gate (kind='tech' queue)

- **Exact match** (case-insensitive, trimmed) → auto-assign, process as today.
- **Anything else** (fuzzy or no match) → deduct NOTHING. The invoice is queued
  as a new row in `unmatched_parts` with `kind='tech'`, storing:
  - `raw_name` = the technician name from the invoice
  - `tech_name` = same raw name
  - new column `pending_data TEXT` (JSON) holding the extracted invoice data
    (parts list or package description)
  - the invoice IS marked processed (no re-poll loop, no duplicate rows)
- **Unmatched page** gets a third section "Tech Confirmation" listing
  `kind='tech'` rows with:
  - a dropdown of active techs (exact-ish candidates first)
  - an editable free-text name field (fallback: type the exact name)
- **Confirm** → read `pending_data`, re-run the format handler with the
  confirmed tech → parts are deducted or flow into the existing Unmatched
  Parts/Packages sections.

### 2. Edit tech name (Techs page)

- Add `name` param to `modules/techs.py:update_tech`.
- Add a name text input to the Edit form in `templates/dashboard_techs.html`.
- On rename, future invoices match exactly.

### 3. Usage by tech

- New analytics: `usage_by_tech(conn, warehouse_id, days)` and
  `usage_by_part_for_tech(conn, tech_name, days)` grouping `log` by `tech_name`.
- Usage page gets a **Tech filter** dropdown alongside warehouse + period.
  Selecting a tech shows parts used by that tech; unselected keeps the existing
  truck breakdown.

### Schema change

- Additive: `ALTER TABLE unmatched_parts ADD COLUMN pending_data TEXT`.
  Safe for existing rows (NULL).

## Files touched

- `modules/techs.py` — exact-or-queue matching; `update_tech` accepts name
- `modules/email_poller.py` — queue `kind='tech'` on non-exact tech; re-run
  handler on confirm
- `modules/analytics.py` — usage_by_tech, usage_by_part_for_tech
- `main.py` — confirm-tech route; usage page tech filter; unmatched page query
- `templates/dashboard_unmatched.html` — Tech Confirmation section
- `templates/dashboard_usage.html` — tech filter + breakdown
- `templates/dashboard_techs.html` — name input in Edit form
