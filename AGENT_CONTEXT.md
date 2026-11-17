# DoorFlex — Agent Context Document
**Read this before touching any file. This is the single source of truth.**  
**Last updated:** August 19, 2026  
**Prepared by:** Fernando (dev lead)

---

## 0. How to Use This Document

This document answers every question you might have before building. Read it fully before writing a single line of code. When something contradicts the PRD, this document wins — it was written after the client interview.

At the end of this document there is a section called **"Unresolved — Ask Fernando"** for the handful of things that genuinely need human input before you can proceed.

---

## 1. Who Is Who

| Person | Role | What They Do |
|---|---|---|
| **Nick Hussing** | Owner of DoorFlex / Total Garage | Makes business decisions. Receives Winston's weekly payment photo via text. Approves tech routes. |
| **Maria** | Back-office (full-time) | Manually copies WO#s every morning, cross-checks payments in the afternoon, calculates weekly commissions. Everything she does today is what we're automating. |
| **The technicians** | Field workers (e.g. Nick H. in Square screenshots) | Go to job sites, perform repairs, collect payment via Winston's Square app. Will use the PWA. |
| **Winston** | The warranty company (client's client) | Assigns jobs via Dispatch.me. Pays DoorFlex weekly via a photo texted to Nick. Owns the Square account and Dispatch.me account. DoorFlex has no API access to either. |
| **Fernando** | Dev lead (you're talking to him) | Built the PWA. Your contact for technical decisions. |

---

## 2. The Business Model in Plain English

1. Winston gets service requests from homeowners (garage door repairs, mostly).
2. Winston assigns those jobs to DoorFlex via Dispatch.me (DoorFlex has "tech access" only — no admin, no API).
3. DoorFlex sends a technician to the home.
4. The tech collects payment at the site using **Winston's Square account** (not DoorFlex's). DoorFlex cannot access Square's API — the account belongs to Winston.
5. The tech marks the job complete in Dispatch.me with closing notes.
6. At the end of the week, one of Winston's owners texts Nick a **photo** (not PDF, not Excel — a photo of a spreadsheet on a screen or printed paper) showing which jobs were paid and how much.
7. DoorFlex gets **50% of the payout** per job, minus parts costs.
8. Maria reconciles everything manually. This takes her full-time hours every week.

**The critical problem:** Winston sometimes deletes jobs from Dispatch.me when the tech forgets closing notes. When a job disappears from Dispatch, Maria can't see it, Nick can't see it, and Winston doesn't pay for it. Nick said there were 6 missing jobs just last week. There is currently zero evidence DoorFlex can use to dispute this. The PWA becomes that evidence system.

---

## 3. What We Can and Cannot Access

| System | Owner | DoorFlex Access | API Available to Us |
|---|---|---|---|
| Dispatch.me | Winston | Tech access only (view assigned jobs, add closing notes) | ❌ No — API belongs to Winston's enterprise account |
| Square | Winston | Tech can process payments on Winston's device | ❌ No — account belongs to Winston |
| DoorFlex PWA backend | Fernando/DoorFlex | Full access | ✅ Yes — we own it |
| Dispatch.me (scraper) | Winston | Credentials of the tech (Nick H.) | ⚠️ Technically possible via Playwright, violates ToS, risk accepted by Nick |
| Winston weekly report | Winston | Nick receives photo via SMS | Manual forward to email — we parse with vision AI |
| Email (tech's Gmail) | Tech | Full access | ✅ Yes — tech can set forwarding rules |

**Important:** Nick has explicitly accepted the risk of scraping Dispatch.me. He understands it violates ToS and that if Winston detects it, they may terminate the subcontract — which he believes is already at risk anyway due to the missing job payments.

---

## 4. How Jobs Are Assigned — The Full Picture

This is more complex than it sounds. Pay attention.

- Jobs can be assigned **up to a week in advance**. Nick showed 36 jobs scheduled for Thursday, and jobs through the following Thursday already on the calendar.
- Routes **change frequently**. Jobs get added, removed, or reshuffled multiple times before the day of service.
- **Same-day changes happen but are limited:** usually 1–2 job additions max if the route is moving fast. Winston calls Nick directly to ask if they can take more.
- **Notifications are unreliable and incomplete:**
  - Some warranty companies send email when a job is assigned.
  - Some send SMS.
  - Some send nothing — the tech has to check the app manually.
  - Nick said: "The only way to have an accurate account of what's on the schedule each day is having access to Dispatch."
  - Email/SMS notifications do NOT reliably indicate what day the job is, so we cannot use them as the primary data source.
- **The only reliable source is the Dispatch.me app itself.** This is why we scrape.
- Maria's current process: she opens Dispatch.me every morning and manually copies every job for the day into her spreadsheet. She checks again throughout the day for changes.

**Implication for the scraper:** it cannot run just once at 7am. It needs to run multiple times per day (every 2 hours is the plan) to catch additions and removals. Removals are the critical ones — we need to log them as evidence before they disappear.

---

## 5. How Payment Works in the Field

1. Tech arrives at job site.
2. Tech does the work.
3. Tech opens **Winston's Square app** on their device.
4. Tech charges customer via "Tap to Pay" or card present. The charge shows as "Custom Amount" — no line items, no WO# anywhere in Square.
5. Square shows approved/denied on screen.
6. **Tech then goes back to Dispatch.me** and marks the invoice as paid, adds closing notes.
7. The Square receipt shows: amount, cashier name (e.g. "Nick Hussing"), card type, receipt #. **No WO# anywhere.**

**The matching problem:** Maria currently matches Square payments to Dispatch jobs by **dollar amount + date**. This fails constantly because the same amount appears multiple times in a week (e.g., $1,066.75 appeared 5 times in the week of July 27).

**Our solution:** the tech captures payment in the PWA immediately after collecting, linking WO# + amount + receipt photo in our own system. We stop depending on Square for reconciliation.

**Payment scenarios (all must be handled):**
- Card present (most common): collect now, done.
- Cash: collect now, done.
- Check: collect now + check number.
- Not collected — landlord/tenant situation: landlord not on site, tenant can't authorize. (~10% of jobs). Winston sends a Square payment link later.
- Not collected — property management chain: tenant → property management → homeowner chain. Can take 2–3 days.
- Not collected — warranty covers it: no payment from homeowner needed.

Maria notes pending payments in her spreadsheet and follows up. We need to capture the reason so she knows what to follow up on.

---

## 6. Winston's Weekly Report — Exact Format

- Arrives as a **photo** (screenshot of a spreadsheet, or photo of printed paper) texted to Nick's personal phone.
- Nick copy-pastes it to Maria (also likely via text or WhatsApp).
- **It is never a PDF. It is never an Excel file.** It's a photo. Every time.
- The report we have as a sample (Winston_jobs_47.pdf) was converted to PDF by someone for sharing — the original is always a photo.
- Timing: arrives weekly, day unknown. Nick does not know exact time — "whenever Winston sends it."
- Jobs from previous weeks can appear late. Winston can report a job 2–3 weeks after it happened.
- The photo contains: tech name, date per job, WO#, total sales, payout, commission (50% of payout), notes (e.g. "did not collect service call fee"), subtotal, parts deduction, total.

**Sample data from the report (week of July 27 – Aug 1, 2026, tech: Nick):**

| Date | WO# | Total Sales | Payout | Commission |
|---|---|---|---|---|
| Jul 27 | 25699148 | $839.32 | $769.00 | $384.50 |
| Jul 27 | 622602235 | $1,310.43 | $75.00 | $37.50 |
| Jul 27 | 341657927 | $1,066.75 | $983.00 | $491.50 |
| Jul 27 | 6786329 | $2,103.15 | — | — |
| Jul 28 | 66992469 | — | $100.00 | $50.00 |
| Jul 28 | 3299860 | — | $80.00 | $40.00 |
| Jul 29 | 68349489 | $536.85 | $579.00 | $289.50 |
| Jul 29 | 342418106 | $671.46 | $601.00 | $300.50 |
| Jul 29 | 10151917 | — | $80.00 | $40.00 |
| Jul 29 | 3300690 | — | $80.00 | $40.00 |
| Jul 29 | 67644519 | — | $100.00 | $50.00 |
| Jul 30 | 3298415 | $590.23 | $527.00 | $263.50 |
| Jul 30 | 3302532 | — | $80.00 | $40.00 |
| Jul 30 | 3300473 | — | $80.00 | $40.00 |
| Jul 30 | 67806329 | — | $1,989.00 | $994.50 |
| Jul 30 | 68193579 | $616.00 | $692.00 | $346.00 |
| Jul 31 | 622634103 | $1,066.75 | $1,033.00 | $516.50 |
| Jul 31 | 65339739 | $1,066.75 | $1,052.00 | $526.00 |
| Jul 31 | 67039639 | — | $100.00 | $50.00 |
| Jul 31 | 68241519 | $1,505.37 | $1,452.00 | $726.00 |
| Aug 1 | 622633872 | — | $85.00 | $42.50 |
| Aug 1 | 68895789 | $790.58 | $813.00 | $406.50 |
| Aug 1 | 68790579 | $1,066.75 | $1,062.00 | $531.00 |
| Aug 1 | 67036469 | — | $100.00 | $50.00 |
| Jul 20 | 622575733 | $1,066.75 | $1,040.00 | $520.00 |
| Jul 22 | 65605139 | — | $100.00 | $50.00 |
| — | 341308457 | $0.00 | $0.00 | $0.00 (did not collect) |

Subtotal payout: $13,652.00  
Parts deduction (door): -$427.00  
**Total commission: $6,399.00**

**Note on WO# format:** WO numbers have inconsistent digit counts. Some are 7 digits, some are 8, some are 9, some start with 3-digit prefixes (e.g., 622xxxxxx). The vision AI parser must extract them as strings, never as integers (leading zeros would be lost, and the varying lengths suggest they are IDs from different warranty companies routed through Dispatch).

**Note on jobs with no Total Sales:** some jobs show only payout and commission, no total sales figure. This is normal — service-only jobs or diagnostic-only fees don't always have a full sales figure. The commission column is the authoritative one.

---

## 7. Technician Profile

- Name in screenshots: **Nick Hussing** (owner is also a tech, or uses his name for the Square account)
- Devices: mix of iPhone and Android. **Assume iOS is primary** — Nick mentioned iPhone usage, and iOS is the harder constraint for Background Sync.
- Tech-savviness: Nick described them as tech-savvy, "that's one of the main ways we communicate." They use the PWA already.
- Communication: currently via text message. Nick is open to adding Slack or another app if it makes automation easier.
- Number of techs: **not confirmed in this interview.** Fernando needs to ask. Assume multiple (plural was used throughout the conversation).
- The tech currently:
  1. Wakes up, checks Dispatch app for the day's route
  2. Drives to jobs
  3. Collects payment in Square
  4. Goes back to Dispatch, marks invoice paid, adds closing notes
  5. (Sometimes) forgets step 4, causing Winston to delete the job

**The PWA should insert itself between step 3 and step 4** — after payment, before they leave the site. Not instead of Dispatch closing notes — in addition to it.

---

## 8. Dispatch.me App — What the Tech Sees

From the screenshot labeled "My Jobs":
- Calendar view at top (swipe left/right by day)
- Each job shows: time window (e.g. "8:00am - 1:00pm MST"), tech name, and job title
- Job title format: **"[Type] - [WO#] [Company abbrev]..."** e.g. "In Person - 73012439 ELC N..." or "First Time Dispatch - Normal..."
- Address shown below title: street, city, state, zip
- Status icons: gray circle = not started, blue checkmark = completed, orange truck = in transit

**What the scraper needs to extract per job:**
- WO# (from the title, after the dash)
- Job type (the part before the dash: "In Person", "First Time Dispatch", etc.)
- Time window start and end
- Address (street, city, state, zip)
- Status icon / completion status
- Tech name (for multi-tech setups)

The Dispatch settings screen shows: Notification Settings, Offline Settings, Offline Actions, Help, Sign Out. **Notification Settings exists** — it may be possible to enable email notifications per job. This should be tested manually before assuming the scraper is the only option.

---

## 9. Square App — What the Tech Sees

From screenshots:
- **Transactions list:** shows amount + time only. No WO#, no customer name, no notes.
- **Transaction detail:** shows amount, date/time, card type, receipt #, customer name ("Unknown Name" in the screenshot), cashier name ("Nick Hussing").
- **Item sold:** "Custom Amount" — not a catalog item, just a dollar amount typed in.
- **Checkout screen:** has Library (Items, Gift cards, Rewards, Discounts, Service charges, Services, Doors category with 2 items).
- There is a **Doors category with 2 items** in the Square catalog. These may be line items used when a door sale is involved.
- The "Tap to Pay" label at top indicates they use iPhone's NFC for card-present payments.

**Key insight:** the Square transaction has a "Notes" field that can be filled at checkout time. This field exports in the Item Details CSV. However, since Winston owns the Square account, we cannot enforce that the tech fills this field. The PWA capture is our workaround — the tech logs the WO# in our system, not in Square.

---

## 10. Existing PWA — Technical Details

Fernando built this. Here is everything known about it:

**Stack (inferred from description — Fernando should confirm):**
- Frontend: vanilla JS PWA (no React, no Vue — single `index.html` + `app.js`)
- Service worker: `sw.js`
- Storage: IndexedDB (primary) + localStorage (fallback mirror)
- Backend: unknown framework — Fernando to confirm (Node/Express? Python/FastAPI?)
- Database: unknown — Fernando to confirm

**Offline-first architecture:**
- Every user action queues locally first
- Sync happens: manually, on reconnect, on focus/visibility, every 15s (queue drain), every 120s (catalog refresh)
- Background Sync tag `deductions` handles sync when app is closed — **Android only**
- iOS: app must be open and have network to sync

**Idempotency pattern (critical — replicate for new stores):**
- Each batch has a `batch_id` (UUID generated client-side)
- Server records processed `batch_id`s
- Retried syncs with same `batch_id` are ignored by server
- Client uses "shadow pending quantities" to show correct UI before server confirms

**Auth pattern:**
- Token in URL at install time: `?t=<token>`
- Token hashed server-side, checked via `pwa.get_tech_by_token`
- Persisted in `kv` IndexedDB store + localStorage
- 401 response clears token and forces reinstall (tech gets new link)

**Existing endpoints (confirmed):**
```
GET  /tech/catalog?token=
POST /tech/deductions          body: {token, batch_id, items}
GET  /tech/lookups/brands
GET  /tech/lookups/models
GET  /tech/lookups/designs
GET  /tech/lookups/insulations
GET  /tech/lookups/sizes
GET  /tech/lookups/windows
GET  /tech/lookups/colors
GET  /tech/lookups/generic-options
POST /tech/door-orders         body: {token, ...}
POST /tech/door-orders/cancel  body: {token, ...}
```

**What we do NOT know yet (ask Fernando):**
- Backend language and framework
- Database engine (PostgreSQL? MySQL? SQLite? Firebase?)
- Where the backend is hosted (VPS? Railway? Fly.io? Local?)
- Whether there's already a `technicians` table and what its schema is
- Whether there's already any admin interface
- Current deployment process (how does Fernando push changes?)
- Whether `pwa.get_tech_by_token` is a function name or an endpoint

---

## 11. Dedicated Laptop — Planned Setup

Nick is providing a dedicated laptop for automations. Key constraints:
- Always on, plugged into Ethernet and power outlet
- In a dedicated room (no personal data)
- Nick mentioned: "I'm willing to get a dedicated laptop and dedicated phone to this automation"
- OS preference: not stated. Recommend Ubuntu 22.04 LTS for simplicity, but Windows works too
- Internet: Ethernet (reliable) — no WiFi dependency
- The laptop runs: Playwright scraper, n8n orchestrator, mail listener, Claude API calls
- All scraped data + report photos backed up to cloud daily

**n8n vs. other orchestrators:**
We chose n8n (self-hosted, free) because:
- Runs locally — works even if n8n.io is down
- Visual editor Nick/Maria can inspect
- Handles retries, error alerts, scheduling natively
- No per-execution cost

---

## 12. Door Catalog — Reference Data

Nick confirmed these brands/models during the call. They sell primarily to HOA communities in Arizona.

### AMARR
| Model | Type | Notes |
|---|---|---|
| Northwoods | Modern plank | Newer style |
| 3552 | Aluminum full view | |
| 3502 | Aluminum full view | |
| Classic Flush | Steel | |
| Classic Full View | Steel | |
| Classic | Steel | |
| Carriage Court | Steel | |
| Designer's Choice | Steel | |
| Hillcrest | Steel | |
| Heritage | Steel | |
| Olympus | Steel | |

### CLOPAY
Models not confirmed by name in the interview. Nick said "Clopay" and referenced the same design names (Northwoods, Classic, etc.) — but these may be AMARR names he applied loosely. **Verify with Nick before adding Clopay-specific models.**

### SAFEWAY (via Service Springs)
- Nick said: "That's with Service Springs. You should be able to find that on Service Springs' catalog that I sent you."
- Fernando should have received this catalog. If not, request it from Nick.
- Do not add placeholder Safeway models — wait for the actual catalog.

### HOA Color Constraints
~90% of Arizona jobs are HOA communities. Typical allowed colors:
- Desert tan
- Sandstone  
- White
- Almond

When a tech selects a job tagged as HOA, the color picker in the door order form should surface these four colors first.

---

## 13. Commission Rules

These are the exact rules from Nick's own words:

**Base rule:** 50% of the payout figure (Winston's column, not total sales).

**Parts/door deduction:** when DoorFlex orders a door for a job, they pay upfront (e.g., $2,000 for a door). When Winston pays, DoorFlex gets reimbursed: 50% of the door cost back + 50% of the total sale commission. The parts deduction in the Winston report is a single line item deducted from the weekly total, not per-job.

**Example from Nick:**
- DoorFlex orders door: pays $2,000 upfront
- Winston pays $3,000 for the completed job 3 weeks later
- DoorFlex receives: $1,000 (50% of door cost reimbursement) + $1,500 (50% of $3,000 sale) = $2,500

**Service call only jobs:** some jobs are diagnostic only ($75–$100). Commission is still 50% — e.g., $80 payout → $40 commission.

**Not-collected jobs:** if payment wasn't collected, commission is $0 until collected. Winston may send a payment link directly to the customer later — when that resolves, it should appear in a future Winston report.

**The commission column in Winston's report is authoritative.** Don't recalculate it — use it. Our job is to verify we have a PWA capture for every job in that column and flag the ones we don't.

---

## 14. Reconciliation Logic — Exact Rules

When Winston's report arrives, run this logic:

```
FOR each job in winston_report:
  IF job.wo_number EXISTS in job_captures:
    status = "matched" — commission confirmed
  ELSE:
    status = "winston_only" — tech may not have captured, or late job from prior week
    → do NOT auto-flag as dispute (may be a late payment from weeks ago)
    → add to Maria's review queue

FOR each job in job_captures WHERE date IN report_period:
  IF job.wo_number NOT IN winston_report:
    IF job.status = "removed_by_dispatch":
      status = "DISPUTE" — Winston deleted this job, we have evidence
      → auto-generate evidence packet
    ELSE:
      status = "captured_not_reported" — Winston hasn't paid yet
      → add to pending queue, check again next week

FOR each expected_job WHERE date IN report_period AND status = "removed_by_dispatch":
  IF NOT IN job_captures:
    status = "deleted_no_evidence" — Winston deleted AND tech didn't capture
    → add to Maria's manual review (nothing to dispute with)
```

**Evidence packet contents (for DISPUTE jobs):**
- WO number
- Tech name
- Job address (from expected_jobs)
- Dispatch-assigned time window
- `captured_at` timestamp (client) + `received_at` timestamp (server)
- Amount charged
- Payment method
- Receipt photo (linked URL)
- Closing notes entered by tech
- Screenshot of Dispatch.me job (from scraper, if available)

---

## 15. iOS — Specific Constraints

This is the highest-risk technical area. Be conservative.

**What does NOT work on iOS PWAs:**
- Background Sync API (the `sync` event in service workers) — not supported
- Push notifications on iOS < 16.4
- Push notifications when PWA is not added to home screen (must be installed)

**What DOES work on iOS PWAs (16.4+, installed to home screen):**
- Web Push API (push notifications while app is closed)
- IndexedDB
- Service worker (fetch events, cache API)
- `online`/`offline` events
- `visibilitychange` and `focus` events

**Our iOS strategy:**
1. Aggressive sync on every app open (visibilitychange → visible)
2. End-of-day push notification at 6pm if unsynced captures exist
3. Visual indicator showing pending count so tech knows to sync before going home
4. Receipt photos stored as blobs in IndexedDB — upload when online, do not block the capture flow on upload

**Push notification requirements for iOS:**
- PWA must be installed to home screen (not just opened in Safari)
- iOS 16.4+ required
- VAPID keys required
- User must grant permission explicitly
- Permission prompt must be triggered by a user gesture (button tap), not on page load

**Installation flow for techs:**
- Tech receives token link
- Opens in Safari on iPhone
- Must tap "Add to Home Screen" before push notifications work
- Consider adding an in-app prompt that detects if running in browser vs. installed and guides the tech to install

---

## 16. Unresolved — Ask Fernando Before Proceeding

These are the only things that cannot be determined from existing context. Do not assume or guess.

| # | Question | Why It Blocks |
|---|---|---|
| 1 | What is the backend stack? (language, framework) | Determines how to write new endpoints |
| 2 | What database engine? | Determines SQL dialect and migration approach |
| 3 | Where is the backend hosted? | Determines deployment process for new endpoints |
| 4 | Is there a `technicians` table? What's the schema? | All new tables reference tech IDs |
| 5 | What does `pwa.get_tech_by_token` resolve to? (function, ORM call, raw SQL?) | Need to follow same auth pattern in new endpoints |
| 6 | Does any admin interface exist? | Determines if admin dashboard is greenfield |
| 7 | How many technicians currently? | Affects push subscription design and scraper auth |
| 8 | Does the scraper use one set of Dispatch credentials (Nick's) or per-tech? | Nick's login may show all techs; individual tech logins only show their own jobs |
| 9 | Has Fernando already added `jobs_today` or `job_capture_queue` to IndexedDB? | Avoids duplicate store creation |
| 10 | What day/time does the Winston photo typically arrive? | Affects whether the mail listener needs to handle weekend arrivals |
| 11 | Is the Safeway/Service Springs catalog available? | Blocks door catalog completion |
| 12 | Are there multiple techs under the same DoorFlex subcontract, or just Nick? | Affects how we attribute captures to techs in the commission report |

---

## 17. Decisions Already Made — Do Not Revisit

These were explicitly decided by Nick and/or Fernando. Do not suggest alternatives.

| Decision | Rationale |
|---|---|
| Scrape Dispatch.me with Playwright | Only reliable data source. Nick accepted the ToS risk. |
| Parse Winston report with vision AI (Claude) | Report is always a photo. No structured export available. |
| PWA as evidence system, not just workflow tool | Winston deletes jobs — we need independent proof. |
| No Square API integration | Winston owns the account. Not negotiable. |
| 50/50 commission split hardcoded for Winston | Nick confirmed this is fixed for this contract. |
| Dedicated laptop for automations | Nick is providing it. Decision made. |
| n8n for orchestration on laptop | Self-hosted, free, appropriate for this scale. |
| Receipts stored server-side with server timestamp | Client timestamps are not trustworthy for dispute evidence. |
| Jobs removed from Dispatch are NEVER deleted from our DB | They become evidence. Soft-delete only with `removed_at` timestamp. |
| iOS push at 6pm for unsynced captures | Best available mechanism for iOS given Background Sync limitation. |

---

## 18. Files Available for Reference

| File | Contents |
|---|---|
| `doorflex_prd.md` | Full product requirements with agent checklist |
| `Winston_jobs_47.pdf` | Sample Winston weekly report (originally a photo, converted to PDF for sharing) |
| Screenshots 1–9 (jpg) | Square transaction detail, Square transactions list, Square checkout/library, Dispatch "My Jobs" list, Dispatch settings, Dispatch inbox |

---

*End of Agent Context Document*
