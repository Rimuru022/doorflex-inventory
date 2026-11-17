/* DoorFlex Tech PWA — offline-first catalog + tap-to-deduct.
 * Data model: catalog cached from /tech/catalog; pending deductions queued in
 * IndexedDB; synced as idempotent batches via /tech/deductions.
 *
 * Robustness guarantees:
 *  - deductions are NEVER lost (persisted to IndexedDB before any sync)
 *  - sync processes every queued batch; one failure never blocks the rest
 *  - retries are scheduled while items are pending (not only on `online`)
 *  - Background Sync (Chrome/Android) pushes even if the app was killed
 *  - going back to the catalog never reloads the page (no network needed)
 */
'use strict';

const DB_NAME = 'doorflex-tech';
const DB_VERSION = 4;
const CATALOG_KEY = 'catalog';
const TOKEN_KEY = 'token';
const TOKEN_LS_KEY = 'doorflex.token';
const QUEUE_LS_KEY = 'doorflex.queue';
const DOOR_QUEUE_LS_KEY = 'doorflex.doorqueue';
const JOB_CAPTURE_QUEUE_LS_KEY = 'doorflex.jobcapturequeue';
const ACTIVE_WO_LS_KEY = 'doorflex.activeWo';
const RETRY_MS = 2000;
const FETCH_TIMEOUT_MS = 10000;

let db;
let catalog = [];
let filter = '';
let current = null;
let syncing = false;
let syncStartedAt = 0;
let retryTimer = null;

/* Pending sync shadow: maps part_id -> total qty still in the queue. The UI
 * uses (server stock - shadow) so that reopening the app after a partial sync
 * still shows the right number (server - what is still pending). Rebuilt from
 * IndexedDB on boot and after every sync mutation. */
const pendingShadows = new Map();

function shadowOf(partId) {
  return pendingShadows.get(partId) || 0;
}

function effectiveStock(part) {
  return Math.max(0, part.stock - shadowOf(part.part_id));
}

async function rebuildPendingShadows() {
  pendingShadows.clear();
  const pending = await loadPending();
  for (const batch of pending) {
    for (const item of batch.items || []) {
      pendingShadows.set(
        item.sku,
        (pendingShadows.get(item.sku) || 0) + (item.qty || 0),
      );
    }
  }
}

function rerenderAfterShadowChange() {
  renderGrid();
  if (current) renderDetail();
  updatePendingBadge();
}

/* ─── IndexedDB ──────────────────────────────────────────────────────────── */

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = (e) => {
      const d = e.target.result;
      if (!d.objectStoreNames.contains('kv')) d.createObjectStore('kv');
      // v2: queue store WITHOUT keyPath. Out-of-line keys are more portable
      // across iOS Safari builds and remove any ambiguity around
      // put(value, key) when keyPath is set. Any batches in the old
      // keyPath store are dropped — acceptable, they would be stale.
      if (e.oldVersion < 2 && d.objectStoreNames.contains('queue')) {
        d.deleteObjectStore('queue');
      }
      if (!d.objectStoreNames.contains('queue')) d.createObjectStore('queue');
      // v3: door_queue keyed by job_wo — a separate queue from deductions
      // (never touches stock), plus a cache store for /tech/lookups/* JSON so
      // the door-order wizard still works offline once a tech has opened it
      // at least once with signal.
      if (!d.objectStoreNames.contains('door_queue')) d.createObjectStore('door_queue');
      if (!d.objectStoreNames.contains('lookup_cache')) d.createObjectStore('lookup_cache');
      // v4: jobs_today (cached job list, keyed by wo_number) + job_capture_queue
      // (pending closing-notes/payment captures, keyed by capture_id) — the
      // independent evidence record feature. Same out-of-line-key convention.
      if (!d.objectStoreNames.contains('jobs_today')) d.createObjectStore('jobs_today');
      if (!d.objectStoreNames.contains('job_capture_queue')) d.createObjectStore('job_capture_queue');
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function idbGet(store, key) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(store, 'readonly');
    const r = tx.objectStore(store).get(key);
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}

function idbSet(store, key, value) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(store, 'readwrite');
    tx.objectStore(store).put(value, key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

function idbDel(store, key) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(store, 'readwrite');
    tx.objectStore(store).delete(key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

function idbAll(store) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(store, 'readonly');
    const r = tx.objectStore(store).getAll();
    r.onsuccess = () => resolve(r.result || []);
    r.onerror = () => reject(r.error);
  });
}

function idbClear(store) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(store, 'readwrite');
    tx.objectStore(store).clear();
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

/* ─── Token ──────────────────────────────────────────────────────────────── */

async function clearToken() {
  try { await idbDel('kv', TOKEN_KEY); } catch (e) {}
  try { localStorage.removeItem(TOKEN_LS_KEY); } catch (e) {}
}

async function getToken() {
  const fromUrl = new URLSearchParams(location.search).get('t');
  if (fromUrl) {
    // Both writes are independent best-effort: a failed IndexedDB write
    // (quota, blocked transaction, private-mode Safari) must not skip the
    // localStorage mirror, or the token could be lost entirely.
    try { await idbSet('kv', TOKEN_KEY, fromUrl); } catch (e) { /* IDB unavailable */ }
    try { localStorage.setItem(TOKEN_LS_KEY, fromUrl); } catch (e) { /* storage full/blocked */ }
    const clean = location.pathname + location.search.replace(/[?&]t=[^&]*/, '');
    history.replaceState(null, '', clean);
    return fromUrl;
  }
  // IndexedDB first; fall back to the localStorage mirror (iOS home-screen apps
  // can run in a partitioned storage context — this covers token loss).
  let stored = null;
  try { stored = await idbGet('kv', TOKEN_KEY); } catch (e) { stored = null; }
  if (stored) return stored;
  try { stored = localStorage.getItem(TOKEN_LS_KEY); } catch (e) { stored = null; }
  if (stored) {
    await idbSet('kv', TOKEN_KEY, stored);
    return stored;
  }
  return null;
}

/* ─── Catalog ────────────────────────────────────────────────────────────── */

async function fetchCatalog(token, signal) {
  const url = `/tech/catalog?token=${encodeURIComponent(token)}`;
  // fetchWithTimeout, not a bare fetch: Safari can hang a request indefinitely
  // instead of failing it, which would otherwise leave callers (syncNow via
  // refreshCatalog) stuck awaiting forever with their `syncing` lock held.
  const resp = await fetchWithTimeout(url, { cache: 'no-store' }, FETCH_TIMEOUT_MS, signal);
  if (resp.status === 401) throw new InvalidTokenError();
  if (!resp.ok) throw new Error(`catalog ${resp.status}`);
  const data = await resp.json();
  await idbSet('kv', CATALOG_KEY, { parts: data.parts, meta: data });
  return data;
}

class InvalidTokenError extends Error {}

async function loadCatalog() {
  const cached = await idbGet('kv', CATALOG_KEY);
  return cached ? cached.parts : [];
}

/* Refresh the catalog from the server after a sync cycle so the app reflects
 * the latest server values automatically (deductions from this tech, other
 * techs, stock fixes from the dashboard, new/deactivated parts). Safe to call
 * when offline — it just keeps the current catalog and shadows. If the part
 * being viewed was removed server-side, close the detail view.
 *
 * The `force` flag is used by syncNow (which owns the syncing guard): the
 * 2-minute heartbeat / foreground events must NOT refresh while a sync is in
 * flight, or they would read a stale queue (batch not yet deleted) and the
 * pending shadow would be applied twice — the stock glitch where the number
 * briefly drops too much and then settles. */
async function refreshCatalog(force, signal) {
  if (syncing && !force) return;
  let token = null;
  try { token = await getToken(); } catch (e) { return; }
  if (!token) return;
  try {
    const data = await fetchCatalog(token, signal);
    catalog = data.parts;
    const sub = document.getElementById('sub');
    if (sub) sub.textContent = `${data.tech} · ${data.truck_id} · ${catalog.length} parts`;
    if (current) {
      const fresh = catalog.find((x) => x.part_id === current.part_id);
      if (fresh) {
        current = fresh;
        renderDetail();
      } else {
        // Part was removed/deactivated server-side while the tech had it open.
        showCatalog();
      }
    }
    await rebuildPendingShadows();
    renderGrid();
    updatePendingBadge();
  } catch (e) {
    // Offline or server error — keep the current catalog + shadows.
    if (e instanceof InvalidTokenError) {
      await clearToken();
      const sub = document.getElementById('sub');
      if (sub) sub.textContent = 'Link revoked — request a new install link.';
      document.getElementById('grid').innerHTML =
        '<div class="empty">This install link is no longer valid.<br><br>' +
        '<button class="btn" onclick="location.reload()">Re-check</button></div>';
    }
  }
}

/* ─── Jobs Today (My Jobs) ───────────────────────────────────────────────── */

let jobsToday = [];

function todayDateStr() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

async function fetchJobsToday(token, signal) {
  const url = `/tech/jobs/today?token=${encodeURIComponent(token)}&date=${todayDateStr()}`;
  const resp = await fetchWithTimeout(url, { cache: 'no-store' }, FETCH_TIMEOUT_MS, signal);
  if (resp.status === 401) throw new InvalidTokenError();
  if (!resp.ok) throw new Error(`jobs ${resp.status}`);
  const data = await resp.json();
  const jobs = data.jobs || [];
  try {
    await idbClear('jobs_today');
    for (const j of jobs) await idbSet('jobs_today', j.wo_number, j);
  } catch (e) { /* best effort — jobsToday (in-memory) is still current */ }
  return jobs;
}

async function loadJobsTodayCache() {
  try { return await idbAll('jobs_today'); } catch (e) { return []; }
}

/* Same "keep the cache on failure" shape as refreshCatalog — offline or a
 * server error must never wipe out the list a tech already has open. */
async function refreshJobsToday(signal) {
  let token = null;
  try { token = await getToken(); } catch (e) { return; }
  if (!token) return;
  try {
    jobsToday = await fetchJobsToday(token, signal);
    // Never re-render over an in-progress screenshot review: the background
    // refresh (2-min tick, focus, reconnect) would otherwise wipe the screen
    // the tech is actively correcting and lose their edits.
    const myjobs = document.getElementById('myjobs');
    if (myjobs && !myjobs.hidden && !screenshotParsedJobs) renderMyJobsList();
    if (document.getElementById('wopicker') && !document.getElementById('wopicker').hidden) renderWoPicker();
    renderActiveWoBanner();
    updateJobPendingBadge();
  } catch (e) {
    // Offline/error — keep whatever jobsToday already holds.
  }
}

/* ─── Active WO session ──────────────────────────────────────────────────────
 * A WO selected here applies automatically to every part deduction and to
 * the door-order wizard for the rest of the session — the tech no longer
 * tags each deduction individually. Persisted to localStorage so it survives
 * a reload. Closing that WO (confirmCloseJob) clears it and the tech must
 * pick another before deducting parts or starting a new door order again. */

let activeWo = '';
try { activeWo = localStorage.getItem(ACTIVE_WO_LS_KEY) || ''; } catch (e) { activeWo = ''; }

function setActiveWo(wo) {
  activeWo = String(wo || '').trim();
  try { localStorage.setItem(ACTIVE_WO_LS_KEY, activeWo); } catch (e) { /* best effort */ }
  renderActiveWoBanner();
}

function clearActiveWo() {
  activeWo = '';
  try { localStorage.removeItem(ACTIVE_WO_LS_KEY); } catch (e) { /* best effort */ }
  renderActiveWoBanner();
}

function renderActiveWoBanner() {
  const el = document.getElementById('activewobanner');
  if (!el) return;
  el.hidden = false;
  if (!activeWo) {
    el.classList.add('none');
    el.innerHTML = `
      <span>No active WO — pick a job before deducting parts.</span>
      <button type="button" class="active-wo-change" onclick="openWoPicker(false)">Select</button>`;
    return;
  }
  el.classList.remove('none');
  const job = jobsToday.find((j) => j.wo_number === activeWo);
  el.innerHTML = `
    <span>Active WO: <strong>${escapeHtml(activeWo)}</strong>${job && job.address ? ' — ' + escapeHtml(job.address) : ''}</span>
    <button type="button" class="active-wo-change" onclick="openWoPicker(true)">Change</button>`;
}

// requireActiveWo(): the gate for any entry point that must have a WO to
// tag its work to (opening a part detail, starting a door order). Opens the
// picker instead of proceeding when nothing is active. Returns whether the
// caller can proceed right now.
function requireActiveWo() {
  if (activeWo) return true;
  openWoPicker(false);
  return false;
}

function openWoPicker(allowCancel) {
  document.getElementById('wopicker').hidden = false;
  document.getElementById('detail').hidden = true;
  document.getElementById('doorwiz').hidden = true;
  document.getElementById('q').style.display = 'none';
  hideFloatingIndicators();
  renderWoPicker(allowCancel);
}

function closeWoPicker() {
  const el = document.getElementById('wopicker');
  el.hidden = true;
  el.innerHTML = '';
  document.getElementById('q').style.display = '';
  updatePendingBadge();
  updateDoorPendingBadge();
}

function renderWoPicker(allowCancel) {
  const el = document.getElementById('wopicker');
  const canCancel = allowCancel && !!activeWo;
  const header = `
    <div class="wiz-header">
      ${canCancel ? `<button class="btn back" onclick="closeWoPicker()" aria-label="Cancel">✕</button>` : '<div style="width:44px"></div>'}
      <div class="wiz-title">Select Active WO</div>
      <div style="width:44px"></div>
    </div>`;
  const intro = `<p class="text-sm" style="padding:0 4px 8px;color:var(--muted)">Every part you deduct and any door order you start will be tagged to this WO until you close it or change it.</p>`;
  const list = jobsToday.length
    ? '<div class="wiz-options">' + jobsToday.map((j, idx) => `
        <button class="wiz-option ${j.wo_number === activeWo ? 'selected' : ''}" onclick="chooseActiveWo(${idx})">
          WO# ${escapeHtml(j.wo_number)}
          ${j.address ? `<span class="sub">${escapeHtml(j.address)}</span>` : ''}
        </button>`).join('') + '</div>'
    : '<div class="wiz-empty">No jobs loaded for today yet.</div>';
  const manual = `
    <div class="wiz-body">
      <label class="job-field-label">Or enter a WO# by hand</label>
      <input type="text" id="wopicker-manual-input" placeholder="Work order #" enterkeyhint="done">
    </div>
    <div class="wiz-nav">
      <button class="btn next" onclick="submitManualActiveWo()">Set Active WO →</button>
    </div>`;
  el.innerHTML = header + intro + list + manual;
}

function chooseActiveWo(idx) {
  const job = jobsToday[idx];
  if (!job) return;
  setActiveWo(job.wo_number);
  closeWoPicker();
}

function submitManualActiveWo() {
  const input = document.getElementById('wopicker-manual-input');
  const val = (input.value || '').trim();
  if (!val) { input.focus(); return; }
  setActiveWo(val);
  closeWoPicker();
}

/* ─── Fuzzy search (client-side) ─────────────────────────────────────────── */

function normalize(s) {
  return (s || '').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');
}

function levenshtein(a, b) {
  const m = a.length, n = b.length;
  if (!m) return n;
  if (!n) return m;
  const dp = new Array(n + 1);
  for (let j = 0; j <= n; j++) dp[j] = j;
  for (let i = 1; i <= m; i++) {
    let prev = dp[0];
    dp[0] = i;
    for (let j = 1; j <= n; j++) {
      const tmp = dp[j];
      dp[j] = Math.min(
        dp[j] + 1,
        dp[j - 1] + 1,
        prev + (a[i - 1] === b[j - 1] ? 0 : 1)
      );
      prev = tmp;
    }
  }
  return dp[n];
}

function scorePart(part, q) {
  const qq = normalize(q);
  if (!qq) return 0;
  const name = normalize(part.name);
  const sku = normalize(part.part_id);
  const aliases = (part.aliases || []).map(normalize);
  let best = 0;
  for (const s of [sku, name, ...aliases]) {
    if (s === qq) return 100;
    if (s.includes(qq)) best = Math.max(best, 85 - Math.abs(s.length - qq.length));
    const dist = levenshtein(s.slice(0, Math.max(s.length, qq.length)), qq);
    best = Math.max(best, 60 - dist * 8);
  }
  return best;
}

function renderGrid() {
  const grid = document.getElementById('grid');
  const q = filter.trim();
  let list = catalog;
  if (q) {
    list = catalog
      .map((p) => ({ p, s: scorePart(p, q) }))
      .filter((x) => x.s > 0)
      .sort((a, b) => b.s - a.s)
      .map((x) => x.p);
  } else {
    list = [...catalog].sort((a, b) => (a.name || '').localeCompare(b.name || ''));
  }

  if (!list.length) {
    grid.innerHTML = `<div class="empty">${q ? 'No parts match "' + q + '"' : 'Catalog empty — check connection'}</div>`;
    return;
  }
  grid.innerHTML = list.map(partCard).join('');
}

function partCard(p) {
  const eff = effectiveStock(p);
  const cls = eff <= 0 ? 'danger' : eff < p.min_threshold ? 'warn' : 'ok';
  const label = eff <= 0 ? 'Out' : eff < p.min_threshold ? 'Low' : 'OK';
  const initial = (p.name || '?').charAt(0).toUpperCase();
  return `
  <div class="card" data-part="${p.part_id}" onclick="openPart('${p.part_id}')">
    <div class="placeholder">${initial}</div>
    <div class="name">${escapeHtml(p.name)}</div>
    <div class="sku">${escapeHtml(p.part_id)}</div>
    <span class="badge ${cls}">${label} · ${eff}</span>
  </div>`;
}

/* ─── Detail / deduct (stepper + Confirm) ─────────────────────────────────── */

let stagedQty = 1;

/* While a product detail is open, the floating indicators (pending badge +
 * sync/offline status) are hidden so the stepper + Confirm buttons stay
 * clear. They come back when the tech returns to the grid. */
let detailOpen = false;

function openPart(partId) {
  if (!requireActiveWo()) return;
  const p = catalog.find((x) => x.part_id === partId);
  if (!p) return;
  current = p;
  stagedQty = 1;
  detailOpen = true;
  hideFloatingIndicators();
  renderDetail();
  document.getElementById('detail').hidden = false;
  document.getElementById('q').style.display = 'none';
}

function hideFloatingIndicators() {
  const pending = document.getElementById('pending');
  if (pending) pending.hidden = true;
  const doorPending = document.getElementById('doorpending');
  if (doorPending) doorPending.hidden = true;
  hideSync();
}

function renderDetail() {
  if (!current) return;
  const p = current;
  const overlay = document.getElementById('detail');
  const eff = effectiveStock(p);
  const cls = eff <= 0 ? 'danger' : eff < p.min_threshold ? 'warn' : 'ok';
  const label = eff <= 0 ? 'Out' : eff < p.min_threshold ? 'Low' : 'OK';
  const aliases = (p.aliases || []).filter((a) => a && a !== p.name);
  const aliasesLine = aliases.length
    ? `<div class="aliases">Also known as: ${aliases.map(escapeHtml).join(' · ')}</div>`
    : '';
  const newStock = Math.max(0, eff - stagedQty);
  const overshoot = eff - stagedQty < 0;
  overlay.innerHTML = `
  <div class="detail">
    <div class="detail-header">
      <button class="btn back" onclick="goBack()" aria-label="Back to catalog">←</button>
      <div class="big">${escapeHtml(p.name)}</div>
    </div>
    ${aliasesLine}
    <div class="stock-line">
      Truck stock: <span class="num" id="stocknum">${eff}</span>
      &nbsp;·&nbsp;<span class="badge ${cls}">${label}</span>
    </div>
    <div class="stepper-row">
      <span class="stepper-label">To deduct:</span>
      <div class="stepper">
        <button type="button" class="btn step" id="step-minus" onclick="bumpQty(-1)" aria-label="Decrease by 1">−</button>
        <input type="number" id="qty-input" min="1" step="1" value="${stagedQty}" inputmode="numeric" oninput="setQtyFromInput(this.value)" onblur="clampQtyInput(this)">
        <button type="button" class="btn step" onclick="bumpQty(1)" aria-label="Increase by 1">+</button>
      </div>
    </div>
    <div class="pending-line ${overshoot ? 'overshoot' : ''}">
      Will deduct ${stagedQty} · New stock: ${newStock}
    </div>
    <div class="wo-tag-row">For WO# <strong>${escapeHtml(activeWo)}</strong></div>
    <div class="deduct-row">
      <button class="btn confirm" onclick="confirmDeduction()">✓ Confirm</button>
    </div>
  </div>`;
  syncMinusState();
}

function syncMinusState() {
  const minus = document.getElementById('step-minus');
  if (!minus) return;
  if (stagedQty <= 1) {
    minus.disabled = true;
    minus.classList.add('disabled');
  } else {
    minus.disabled = false;
    minus.classList.remove('disabled');
  }
}

function bumpQty(delta) {
  const next = stagedQty + delta;
  if (next < 1) {
    stagedQty = 1;
  } else {
    stagedQty = next;
  }
  const input = document.getElementById('qty-input');
  if (input) input.value = stagedQty;
  updatePendingLine();
  syncMinusState();
}

function setQtyFromInput(raw) {
  const n = parseInt(raw, 10);
  if (Number.isFinite(n) && n >= 1) {
    stagedQty = n;
  }
  updatePendingLine();
  syncMinusState();
}

function clampQtyInput(el) {
  let n = parseInt(el.value, 10);
  if (!Number.isFinite(n) || n < 1) n = 1;
  stagedQty = n;
  el.value = stagedQty;
  updatePendingLine();
  syncMinusState();
}

function updatePendingLine() {
  if (!current) return;
  const line = document.querySelector('.pending-line');
  if (!line) return;
  const eff = effectiveStock(current);
  const newStock = Math.max(0, eff - stagedQty);
  const overshoot = eff - stagedQty < 0;
  line.textContent = `Will deduct ${stagedQty} · New stock: ${newStock}`;
  line.classList.toggle('overshoot', overshoot);
}

let confirmDeductionInFlight = false;

// Guards against a double-tap firing two click events for one physical tap
// (touch devices, iOS in particular, can queue a second click that still
// dispatches to the Confirm button even after showCatalog() detaches it) —
// without this, queueDeduction's qty-accumulating queue would double the
// deducted amount.
function confirmDeduction() {
  if (!current || confirmDeductionInFlight) return;
  confirmDeductionInFlight = true;
  const qty = stagedQty;
  if (qty > 0) {
    // No mutation of current.stock: the shadow + IDB queue are the source of
    // truth for pending deductions. queueDeduction writes to IDB; the shadow
    // is rebuilt right after so the grid + detail reflect the new pending.
    queueDeduction(current, qty, activeWo);
    // Rebuild shadows + re-render so the grid card and the catalog count
    // show the new effective stock immediately.
    rebuildPendingShadows().then(rerenderAfterShadowChange);
  }
  stagedQty = 1;
  showCatalog();
  confirmDeductionInFlight = false;
}

function goBack() {
  if (!current) return;
  if (stagedQty > 1) {
    const ok = window.confirm(`Discard deduction of ${stagedQty} units?`);
    if (!ok) return;
  }
  stagedQty = 1;
  showCatalog();
}

function showCatalog() {
  current = null;
  stagedQty = 1;
  detailOpen = false;
  document.getElementById('detail').hidden = true;
  document.getElementById('detail').innerHTML = '';
  document.getElementById('q').style.display = '';
  document.getElementById('q').value = filter;
  renderGrid();
  // Re-surface the pending badge (if any) now that we're back on the grid.
  updatePendingBadge();
}

/* ─── Queue + sync ───────────────────────────────────────────────────────── */

let pendingBatch = null;

function uuid() {
  if (crypto && crypto.randomUUID) return crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === 'x' ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

async function queueDeduction(part, qty, woNumber) {
  if (!pendingBatch) {
    pendingBatch = {
      batch_id: uuid(),
      items: [],
      ts_start: Math.floor(Date.now() / 1000),
    };
  }
  const wo = woNumber || '';
  // Merge key includes wo_number too: two taps on the same part for two
  // different jobs must stay as separate line items, not silently combined
  // into one (which would make the per-job "parts used" view wrong).
  const existing = pendingBatch.items.find((i) => i.sku === part.part_id && (i.wo_number || '') === wo);
  if (existing) existing.qty += qty;
  else pendingBatch.items.push({ sku: part.part_id, qty, wo_number: wo, ts: Math.floor(Date.now() / 1000) });

  // Always mirror to localStorage first (synchronous, always works on iOS).
  // This is the durability guarantee: the batch survives even if IndexedDB
  // fails (corrupted store, quota, iOS quirk with keyPath+explicit-key).
  mirrorQueueToLS();

  try {
    await idbSet('queue', pendingBatch.batch_id, pendingBatch);
  } catch (e) {
    // IDB write failed — the LS mirror still has the batch and syncNow
    // will read from it as a fallback. Surface the error so the user
    // knows something is off (not silent).
    console.error('queue IDB write failed', e);
    showSync('Queue save degraded — retrying via cache', 'warn');
  }

  // Rebuild shadow + rerender so the catalog card reflects the new pending
  // (e.g., user deducts 3 → grid card shows server-3 immediately).
  await rebuildPendingShadows();
  rerenderAfterShadowChange();
  // Keep the SW Background Sync tag registered for every queued batch: if the
  // app is killed after this tap, Chrome/Android still wake the SW to push it.
  registerBgSync();
  syncNow();
}

/* ─── localStorage mirror of the queue (safety net) ────────────────────── */

function lsReadQueue() {
  try {
    const raw = localStorage.getItem(QUEUE_LS_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch (e) { return []; }
}
function mirrorQueueToLS() {
  try {
    // Merge pendingBatch + anything already in LS (deduped by batch_id).
    const existing = lsReadQueue().filter((b) => !pendingBatch || b.batch_id !== pendingBatch.batch_id);
    const list = pendingBatch ? [...existing, pendingBatch] : existing;
    localStorage.setItem(QUEUE_LS_KEY, JSON.stringify(list));
  } catch (e) { /* storage full / blocked — best effort */ }
}
function lsRemoveBatch(batchId) {
  try {
    const list = lsReadQueue().filter((b) => b.batch_id !== batchId);
    localStorage.setItem(QUEUE_LS_KEY, JSON.stringify(list));
  } catch (e) { /* best effort */ }
}

function fetchWithTimeout(url, opts, ms, externalSignal) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), ms || FETCH_TIMEOUT_MS);
  let combined = ctrl.signal;
  if (externalSignal) {
    if (typeof AbortSignal !== 'undefined' && AbortSignal.any) {
      combined = AbortSignal.any([ctrl.signal, externalSignal]);
    } else {
      // Fallback: forward the external abort into our controller.
      const onAbort = () => ctrl.abort();
      if (externalSignal.aborted) ctrl.abort();
      else externalSignal.addEventListener('abort', onAbort, { once: true });
      const origFinally = () => {
        if (!externalSignal.aborted) externalSignal.removeEventListener('abort', onAbort);
      };
      return fetch(url, { ...opts, signal: ctrl.signal })
        .finally(() => { clearTimeout(t); origFinally(); });
    }
  }
  return fetch(url, { ...opts, signal: combined }).finally(() => clearTimeout(t));
}

async function loadPending() {
  // Primary: IndexedDB. Fallback: localStorage mirror (if IDB is broken
  // or returns empty when LS has batches).
  let pending = [];
  try { pending = await idbAll('queue'); } catch (e) { pending = []; }
  if (pending.length === 0) {
    const lsBatches = lsReadQueue();
    if (lsBatches.length) {
      // Re-hydrate IDB from LS so the SW pushPending sees them too.
      for (const b of lsBatches) {
        try { await idbSet('queue', b.batch_id, b); } catch (e) { /* best effort */ }
      }
      pending = lsBatches;
    }
  }
  return pending;
}

/* Apply a server-confirmed deduction to the local catalog immediately, so the
 * grid never shows a stale pre-deduction value (or a double-subtracted one)
 * while a refresh is in flight. Only called for items the server accepted. */
function applyAcceptedToCatalog(items) {
  for (const it of items || []) {
    const part = catalog.find((x) => x.part_id === it.sku);
    if (part) part.stock = Math.max(0, part.stock - (it.qty || 0));
  }
}

async function syncNow() {
  if (syncing) return;
  syncing = true;
  syncStartedAt = Date.now();
  activeSyncAbort = new AbortController();
  const abortSignal = activeSyncAbort.signal;
  try {
    const token = await getToken();
    if (!token) { showSync('No token — reinstall from link', 'err'); return; }

    const pending = await loadPending();
    if (!pending.length) {
      showSync('All synced ✓', 'ok');
      setTimeout(hideSync, 1600);
      // Nothing to push, but still refresh from the server so the app shows
      // the latest stock (other techs' deductions, dashboard adjustments,
      // new/deactivated parts) automatically.
      await refreshCatalog(true, abortSignal);
      return;
    }

    let synced = 0;
    let failed = 0;
    let revoked = false;
    let rejections = [];
    for (const batch of pending) {
      try {
        const resp = await fetchWithTimeout('/tech/deductions', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token, batch_id: batch.batch_id, items: batch.items }),
        }, FETCH_TIMEOUT_MS, abortSignal);
        if (resp.status === 401) {
          revoked = true;
          break;
        }
        if (resp.ok) {
          let body = null;
          try { body = await resp.json(); } catch (e) { /* non-JSON (e.g. HTML error) */ }
          const rej = body && Array.isArray(body.rejected) ? body.rejected : null;
          if (body && body.status === 'already_processed') {
            // Idempotent: this batch was already applied server-side, so the
            // local catalog must reflect the deduction now too.
            applyAcceptedToCatalog(batch.items);
            try { await idbDel('queue', batch.batch_id); } catch (e) {}
            lsRemoveBatch(batch.batch_id);
            if (pendingBatch && pendingBatch.batch_id === batch.batch_id) pendingBatch = null;
            synced += 1;
          } else if (rej && rej.length > 0 && (!body.accepted || body.accepted.length === 0)) {
            // Server answered 200 but rejected every item → nothing was deducted.
            // Keep the batch so it is not silently lost; surface the reasons.
            // Every rejection reason is permanent (unknown/inactive SKU, bad
            // qty, a WO that's already closed) AND the server has already
            // marked this batch_id processed — so a retry can only come back
            // as `already_processed`, which would then wrongly subtract stock
            // that was never deducted. Drop the batch and surface why.
            for (const r of rej) rejections.push(`${r.sku}: ${r.reason}`);
            try { await idbDel('queue', batch.batch_id); } catch (e) {}
            lsRemoveBatch(batch.batch_id);
            if (pendingBatch && pendingBatch.batch_id === batch.batch_id) pendingBatch = null;
          } else {
            // Accepted (possibly with partial rejects): subtract exactly what
            // the server accepted so the grid is right this instant.
            if (body && Array.isArray(body.accepted) && body.accepted.length) {
              applyAcceptedToCatalog(body.accepted);
            } else {
              applyAcceptedToCatalog(batch.items);
            }
            try { await idbDel('queue', batch.batch_id); } catch (e) {}
            lsRemoveBatch(batch.batch_id);
            if (pendingBatch && pendingBatch.batch_id === batch.batch_id) pendingBatch = null;
            synced += 1;
          }
        } else {
          // Server answered but rejected (4xx/5xx) — keep the batch for retry.
          failed += 1;
        }
      } catch (e) {
        // Network error or timeout — keep the batch, keep going with the rest.
        failed += 1;
      }
    }

    if (revoked) {
      // Token revoked/rotated server-side: stop retrying and surface the state.
      await clearToken();
      clearRetry();
      try { localStorage.removeItem(QUEUE_LS_KEY); } catch (e) {}
      await rebuildPendingShadows();
      rerenderAfterShadowChange();
      showSync('Link revoked — new link needed', 'err');
      const sub = document.getElementById('sub');
      if (sub) sub.textContent = 'Link revoked — request a new install link.';
      return;
    }

    // Some batches were deleted/kept; rebuild shadow from whatever is left in
    // the queue so the grid reflects the new effective stock immediately.
    await rebuildPendingShadows();
    rerenderAfterShadowChange();

    // Pull the latest server catalog so the app reflects the deductions that
    // were just pushed (and any server-side changes) automatically. If we're
    // offline this is a no-op and the current catalog + shadows stay.
    if (synced > 0) {
      await refreshCatalog(true, abortSignal);
    }

    if (rejections.length > 0) {
      const first = rejections.slice(0, 3).join(' · ');
      showSync(`Rejected: ${first}`, 'err');
      // Rejected batches are dropped above (permanent), so only keep retrying
      // if something else in the queue actually failed transiently.
      if (failed > 0) { registerBgSync(); scheduleRetry(); }
    } else if (failed > 0 && synced > 0) {
      showSync(`${synced} synced · ${failed} pending`, 'warn');
      registerBgSync();
      scheduleRetry();
    } else if (failed > 0) {
      // Keep it quiet: the "N pending" badge already tells the tech there are
      // items queued. No "offline" banner in the middle of the UI.
      registerBgSync();
      scheduleRetry();
    } else {
      showSync('All synced ✓', 'ok');
      setTimeout(hideSync, 1600);
    }
  } finally {
    if (activeSyncAbort && activeSyncAbort.signal === abortSignal) {
      activeSyncAbort = null;
    }
    syncing = false;
  }
}

function scheduleRetry() {
  clearTimeout(retryTimer);
  retryTimer = setTimeout(syncNow, RETRY_MS);
}

function clearRetry() {
  clearTimeout(retryTimer);
  retryTimer = null;
}

async function updatePendingBadge() {
  const el = document.getElementById('pending');
  if (!el) return;
  const pending = await idbAll('queue');
  const count = pending.reduce(
    (n, b) => n + (b.items || []).reduce((m, it) => m + (it.qty || 0), 0),
    0,
  );
  // Keep the badge hidden while a product detail is open.
  if (detailOpen) { el.hidden = true; return; }
  if (count > 0) {
    el.textContent = `${count} pending`;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

function showSync(msg, kind) {
  const el = document.getElementById('sync');
  if (detailOpen) return; // don't clutter the detail view / buttons
  el.textContent = msg;
  el.className = 'sync-indicator show ' + kind;
}
function hideSync() { document.getElementById('sync').className = 'sync-indicator'; }

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);
}

/* ─── Door Order wizard ──────────────────────────────────────────────────────
 * NOT a stock deduction — a separate offline queue (door_queue, keyed by
 * job_wo) posted to /tech/door-orders. One decision per screen (gloves, bad
 * signal). Catalog dropdown data comes from /tech/lookups/* and is cached in
 * IndexedDB (lookup_cache) so the wizard still works once opened offline. */

let doorWiz = null; // { data: {...answers}, stepIdx, steps, _options }
let doorSyncing = false;

const DOOR_DOWNSTREAM = {
  brand: ['model', 'flow', 'model_label', 'design', 'insulation', 'width', 'height',
    'windows', 'window_style', 'glass_type', 'color_base', 'color_overlay',
    'construction', 'insulated'],
  model: ['design', 'insulation', 'width', 'height', 'windows', 'window_style',
    'glass_type', 'model_label', 'construction', 'insulated'],
  design: ['insulation', 'width', 'height', 'windows', 'window_style', 'glass_type'],
  insulation: ['width', 'height', 'windows', 'window_style', 'glass_type'],
  width: [], height: [],
  windows_yesno: ['window_style', 'glass_type'],
  window_style: ['glass_type'],
  model_name: ['construction', 'insulated', 'width', 'windows', 'window_style'],
  construction: ['insulated'],
  insulated_yesno: [], size: [], color: [], address: [],
};

// Mirrors modules/door_orders.py::QUOTE_REQUIRED_MODELS — high-value/rare
// lines where the office needs a distributor quote before quoting the
// customer. Keep in sync if that set changes server-side.
const QUOTE_REQUIRED_MODELS = ['avante', 'canyon_ridge', '3502', '3552'];

const STEP_TITLES = {
  brand: 'Brand',
  model: 'Model',
  design: (d) => (d.flow === 'avante' || d.flow === 'amarr_aluminum' ? 'Glass variant' : 'Series / Design'),
  insulation: 'Insulation',
  width: 'Width',
  height: 'Height',
  windows_yesno: 'Windows?',
  window_style: 'Window style',
  glass_type: 'Glass type',
  model_name: 'Collection / model name',
  construction: 'Construction',
  insulated_yesno: 'Insulated?',
  size: 'Approx. size',
  color: 'Color',
  address: 'Customer address',
  review: 'Review & confirm',
};

function fieldForStep(key) {
  if (key === 'size') return 'width'; // generic flow: one size string in `width`, height stays empty
  if (key === 'color') return 'color_base';
  return key;
}

function computeDoorSteps(data) {
  const steps = ['brand'];
  if (!data.brand) return steps;
  steps.push('model');
  if (!data.model) return steps;

  if (data.flow === 'coachman' || data.flow === 'amarr') {
    steps.push('design');
    if (!data.design) return steps;
    steps.push('insulation');
    if (!data.insulation) return steps;
    steps.push('width');
    if (!data.width) return steps;
    steps.push('height');
    if (!data.height) return steps;
    steps.push('windows_yesno');
    if (data.windows === undefined) return steps;
    if (data.windows) {
      steps.push('window_style');
      if (!data.window_style) return steps;
      steps.push('glass_type');
      if (!data.glass_type) return steps;
    }
  } else if (data.flow === 'avante' || data.flow === 'amarr_aluminum') {
    steps.push('design');
    if (!data.design) return steps;
    steps.push('width');
    if (!data.width) return steps;
    steps.push('height');
    if (!data.height) return steps;
  } else {
    // Named generic-flow models (e.g. Canyon Ridge) already know their name
    // from the Model step — only "Other" needs a free-text name typed in.
    if (data.model === 'other') {
      steps.push('model_name');
      if (!data.model_label) return steps;
    }
    steps.push('construction');
    if (!data.construction) return steps;
    steps.push('insulated_yesno');
    if (data.insulated === undefined) return steps;
    steps.push('size');
    if (!data.width) return steps;
    steps.push('windows_yesno');
    if (data.windows === undefined) return steps;
    if (data.windows) {
      steps.push('window_style');
      if (!data.window_style) return steps;
    }
  }

  steps.push('color');
  if (!data.color_base) return steps;
  steps.push('address');
  if (!data.customer_address) return steps;
  steps.push('review');
  return steps;
}

// Same branching as computeDoorSteps, but never stops at the first
// unanswered field — used only for the "Step X of Y" display so Y is a
// stable total from the moment the flow is known, instead of growing by 1
// with every step answered (which made every step read "N of N"). Before
// windows_yesno is answered it assumes windows will be needed (the longer
// path); the total settles to its final value once that's answered.
function computeDoorTotalSteps(data) {
  let n = 1; // brand
  if (!data.brand) return n;
  n += 1; // model
  if (!data.model) return n;

  if (data.flow === 'coachman' || data.flow === 'amarr') {
    n += 5; // design, insulation, width, height, windows_yesno
    if (data.windows !== false) n += 2; // window_style, glass_type
  } else if (data.flow === 'avante' || data.flow === 'amarr_aluminum') {
    n += 3; // design, width, height
  } else {
    if (data.model === 'other') n += 1; // model_name
    n += 4; // construction, insulated_yesno, size, windows_yesno
    if (data.windows !== false) n += 1; // window_style
  }

  n += 3; // color, address, review
  return n;
}

async function fetchLookup(path, params) {
  const qs = new URLSearchParams(params || {}).toString();
  const url = qs ? `${path}?${qs}` : path;
  try {
    const resp = await fetchWithTimeout(url, { cache: 'no-store' }, 8000);
    if (!resp.ok) throw new Error(`lookup ${resp.status}`);
    const data = await resp.json();
    idbSet('lookup_cache', url, data).catch(() => {});
    return data;
  } catch (e) {
    const cached = await idbGet('lookup_cache', url).catch(() => null);
    if (cached) return cached;
    throw e;
  }
}

async function stepOptions(key, data) {
  switch (key) {
    case 'brand': {
      const r = await fetchLookup('/tech/lookups/brands');
      return r.brands.map((b) => ({ value: b, label: b }));
    }
    case 'model': {
      const r = await fetchLookup('/tech/lookups/models', { brand: data.brand });
      return r.models.map((m) => ({ value: m.key, label: m.label, flow: m.flow }));
    }
    case 'design': {
      const r = await fetchLookup('/tech/lookups/designs', { model: data.model });
      if (data.flow === 'coachman') {
        return r.designs.map((d) => ({ value: d.code, label: `Design ${d.code} (Series ${d.series})` }));
      }
      return r.designs.map((d) => ({ value: d.code, label: d.code }));
    }
    case 'insulation': {
      const r = await fetchLookup('/tech/lookups/insulations', { model: data.model, design: data.design });
      return r.insulations.map((i) => ({ value: i.code, label: i.label }));
    }
    case 'width': {
      const r = await fetchLookup('/tech/lookups/sizes', { model: data.model, design: data.design, insulation: data.insulation || '' });
      return (r.widths || []).map((w) => ({ value: w, label: w }));
    }
    case 'height': {
      const r = await fetchLookup('/tech/lookups/sizes', { model: data.model, design: data.design, insulation: data.insulation || '' });
      return (r.heights || []).map((h) => ({ value: h, label: h }));
    }
    case 'window_style': {
      if (data.flow === 'generic') {
        const r = await fetchLookup('/tech/lookups/generic-options');
        return r.window_styles.map((s) => ({ value: s, label: s }));
      }
      const r = await fetchLookup('/tech/lookups/windows', { model: data.model, design: data.design });
      return r.styles.map((s) => ({ value: s.code, label: s.label }));
    }
    case 'glass_type': {
      const r = await fetchLookup('/tech/lookups/windows', { model: data.model, design: data.design, insulation: data.insulation || '' });
      return r.glass_types.map((g) => ({ value: g.code, label: g.label }));
    }
    case 'construction': {
      const r = await fetchLookup('/tech/lookups/generic-options');
      return r.construction.map((c) => ({ value: c, label: c }));
    }
    case 'size': {
      const r = await fetchLookup('/tech/lookups/generic-options');
      return r.sizes.map((s) => ({ value: s, label: s }));
    }
    case 'color': {
      const r = await fetchLookup('/tech/lookups/colors', { model: data.model || '' });
      return r.colors.map((c) => ({ value: c, label: c }));
    }
    default:
      return [];
  }
}

function renderDoorReview(data) {
  const itemNumber = data.flow === 'coachman' ? `${data.insulation}${data.design}`
    : data.flow === 'avante' ? data.design : '';
  const needsQuote = QUOTE_REQUIRED_MODELS.includes(data.model);

  // Short label/value pairs render as a single receipt-style line; anything
  // that can run long (address) gets its own full-width stacked block.
  const doorRows = [];
  doorRows.push(['Brand', data.brand]);
  doorRows.push(['Model', data.model_label || data.model]);
  if (data.flow === 'coachman' || data.flow === 'amarr') {
    doorRows.push(['Series / Design', data.design]);
    doorRows.push(['Insulation', data.insulation]);
  }
  if (data.flow === 'generic') {
    doorRows.push(['Construction', `${data.construction || ''}${data.insulated ? ' · Insulated' : ''}`]);
  }
  doorRows.push(['Size', data.flow === 'generic' ? data.width : `${data.width} × ${data.height}`]);
  doorRows.push(['Windows', data.windows ? `Yes — ${data.window_style || ''}${data.glass_type ? ' · ' + data.glass_type : ''}` : 'No']);
  doorRows.push(['Color', data.color_base]);

  const jobRows = [];
  jobRows.push(['Job WO', data.job_wo]);

  const line = ([k, v]) => `
    <div class="review-row">
      <span class="review-label">${escapeHtml(k)}</span>
      <span class="review-value">${escapeHtml(v || '—')}</span>
    </div>`;
  const stacked = ([k, v]) => `
    <div class="review-row stacked">
      <span class="review-label">${escapeHtml(k)}</span>
      <span class="review-value">${escapeHtml(v || '—')}</span>
    </div>`;

  return `
    <div class="wiz-review">
      ${itemNumber
        ? `<div class="item-number"><span class="item-number-label">Item number</span>${escapeHtml(itemNumber)}</div>`
        : '<div class="verify-note">⚠ No catalog item number — verify with distributor</div>'}
      ${needsQuote ? '<div class="quote-banner">⚠ Get a distributor quote before quoting the customer</div>' : ''}
      <div class="review-card">${doorRows.map(line).join('')}</div>
      <div class="review-card">${jobRows.map(line).join('')}${stacked(['Customer', data.customer_address])}</div>
    </div>
    <div class="wiz-nav">
      <button class="btn confirm" onclick="confirmDoorOrder()">✓ Send to office</button>
    </div>`;
}

async function renderDoorStep() {
  const wiz = document.getElementById('doorwiz');
  if (!doorWiz) return;
  const steps = computeDoorSteps(doorWiz.data);
  doorWiz.steps = steps;
  if (doorWiz.stepIdx > steps.length - 1) doorWiz.stepIdx = steps.length - 1;
  const key = steps[doorWiz.stepIdx];

  // Auto-skip windows_yesno when the chosen design doesn't allow windows at
  // all (Coachman Series 3/4, or an Amarr collection with no window option).
  if (key === 'windows_yesno' && (doorWiz.data.flow === 'coachman' || doorWiz.data.flow === 'amarr')) {
    let info;
    try {
      info = await fetchLookup('/tech/lookups/windows', { model: doorWiz.data.model, design: doorWiz.data.design });
    } catch (e) {
      info = { allowed: false };
    }
    if (!info.allowed) {
      doorWiz.data.windows = false;
      doorWiz.stepIdx += 1;
      return renderDoorStep();
    }
  }

  const titleFn = STEP_TITLES[key];
  const title = typeof titleFn === 'function' ? titleFn(doorWiz.data) : titleFn;
  // The total is only meaningful once a model (and therefore a flow) is
  // chosen — before that, "Step 1 of 1" / "2 of 2" is just restating the
  // current position, not a real count, so omit the counter rather than
  // show a number that reads as stuck.
  const stepCount = doorWiz.data.model
    ? `<div class="wiz-step-count">Step ${doorWiz.stepIdx + 1} of ${computeDoorTotalSteps(doorWiz.data)}</div>`
    : '';
  const header = `
    <div class="wiz-header">
      <button class="btn back" onclick="${doorWiz.stepIdx === 0 ? 'closeDoorWizard()' : 'doorWizBack()'}" aria-label="Back">←</button>
      <div class="wiz-title">${escapeHtml(title || '')}</div>
      <button class="btn back" onclick="closeDoorWizard()" aria-label="Cancel">✕</button>
    </div>
    ${stepCount}`;

  let body = '';
  if (key === 'model_name' || key === 'address') {
    const current = key === 'model_name' ? doorWiz.data.model_label : doorWiz.data.customer_address;
    const placeholder = key === 'model_name' ? 'e.g. Canyon Ridge' : '123 Main St, City';
    const isArea = key === 'address';
    body = `
      <div class="wiz-body">
        ${isArea
          ? `<textarea id="doorwiz-input" placeholder="${escapeHtml(placeholder)}">${escapeHtml(current || '')}</textarea>`
          : `<input type="text" id="doorwiz-input" placeholder="${escapeHtml(placeholder)}" value="${escapeHtml(current || '')}" enterkeyhint="next">`}
      </div>
      <div class="wiz-nav">
        <button class="btn next" onclick="submitDoorText('${key}')">Next →</button>
      </div>`;
  } else if (key === 'windows_yesno' || key === 'insulated_yesno') {
    const field = key === 'windows_yesno' ? 'windows' : 'insulated';
    const current = doorWiz.data[field];
    body = `
      <div class="wiz-toggle-row">
        <button class="wiz-option ${current === true ? 'selected' : ''}" onclick="chooseDoorBool('${field}', true)">Yes</button>
        <button class="wiz-option ${current === false ? 'selected' : ''}" onclick="chooseDoorBool('${field}', false)">No</button>
      </div>`;
  } else if (key === 'review') {
    body = renderDoorReview(doorWiz.data);
  } else {
    let options = [];
    try {
      options = await stepOptions(key, doorWiz.data);
    } catch (e) {
      wiz.innerHTML = header + '<div class="wiz-empty">Couldn\'t load options — check connection and try again.</div>';
      return;
    }
    doorWiz._options = options;
    const currentValue = doorWiz.data[fieldForStep(key)];
    if (!options.length) {
      body = '<div class="wiz-empty">No options available.</div>';
    } else {
      body = '<div class="wiz-options">' + options.map((o, idx) => `
        <button class="wiz-option ${o.value === currentValue ? 'selected' : ''}" onclick="chooseDoorOption('${key}', ${idx})">
          ${escapeHtml(o.label)}
        </button>`).join('') + '</div>';
    }
  }

  wiz.innerHTML = header + body;
}

function chooseDoorOption(key, idx) {
  const opt = doorWiz._options[idx];
  if (!opt) return;
  const field = fieldForStep(key);
  (DOOR_DOWNSTREAM[key] || []).forEach((f) => { delete doorWiz.data[f]; });
  doorWiz.data[field] = opt.value;
  if (key === 'model') {
    doorWiz.data.flow = opt.flow;
    // "Other" needs a real typed name — don't pre-fill it with the button's
    // own placeholder label. Named models (Coachman, Avante, Canyon Ridge)
    // already know their name, so fill it in and skip asking again.
    doorWiz.data.model_label = opt.value === 'other' ? '' : opt.label;
  }
  if (key === 'color') {
    doorWiz.data.color_overlay = opt.value;
  }
  doorWiz.stepIdx += 1;
  renderDoorStep();
}

function chooseDoorBool(field, value) {
  const key = field === 'windows' ? 'windows_yesno' : 'insulated_yesno';
  (DOOR_DOWNSTREAM[key] || []).forEach((f) => { delete doorWiz.data[f]; });
  doorWiz.data[field] = value;
  doorWiz.stepIdx += 1;
  renderDoorStep();
}

function submitDoorText(key) {
  const el = document.getElementById('doorwiz-input');
  const val = (el.value || '').trim();
  if (!val) { el.focus(); return; }
  const field = key === 'model_name' ? 'model_label' : 'customer_address';
  (DOOR_DOWNSTREAM[key] || []).forEach((f) => { delete doorWiz.data[f]; });
  doorWiz.data[field] = val;
  doorWiz.stepIdx += 1;
  renderDoorStep();
}

function doorWizBack() {
  if (doorWiz && doorWiz.stepIdx > 0) { doorWiz.stepIdx -= 1; renderDoorStep(); }
}

function openDoorWizard() {
  if (!requireActiveWo()) return;
  confirmDoorOrderInFlight = false;
  // job_wo comes from the active-WO session, never typed here — the WO# is
  // the key the office's reconciliation joins on, so it's picked once (from
  // today's Dispatch list where possible) instead of retyped per flow.
  doorWiz = { data: { job_wo: activeWo }, stepIdx: 0, steps: [] };
  document.getElementById('doorwiz').hidden = false;
  document.getElementById('detail').hidden = true;
  document.getElementById('q').style.display = 'none';
  hideFloatingIndicators();
  renderDoorStep();
}

function closeDoorWizardSilent() {
  const wiz = document.getElementById('doorwiz');
  wiz.hidden = true;
  wiz.innerHTML = '';
  document.getElementById('q').style.display = '';
  updatePendingBadge();
  updateDoorPendingBadge();
}

function closeDoorWizard() {
  // job_wo is pre-filled from the active-WO session, not something the tech
  // answered here — it must not by itself trigger the "discard?" prompt.
  const hasAnswers = doorWiz && Object.keys(doorWiz.data).some((k) => k !== 'job_wo');
  if (hasAnswers) {
    const ok = window.confirm('Discard this door order?');
    if (!ok) return;
  }
  doorWiz = null;
  closeDoorWizardSilent();
}

let confirmDoorOrderInFlight = false;

// Same double-tap guard as confirmDeduction(): this function is async and
// awaits before replacing the wizard's DOM, leaving a real window where a
// second tap could re-run it before the first call finishes.
async function confirmDoorOrder() {
  if (confirmDoorOrderInFlight) return;
  confirmDoorOrderInFlight = true;
  const data = doorWiz.data;
  const itemNumber = data.flow === 'coachman' ? `${data.insulation}${data.design}`
    : data.flow === 'avante' ? data.design : '';
  const payload = {
    job_wo: data.job_wo,
    brand: data.brand || '',
    model: data.model || '',
    model_label: data.model_label || data.model || '',
    series_design: (data.flow === 'coachman' || data.flow === 'amarr') ? data.design : '',
    insulation: (data.flow === 'coachman' || data.flow === 'amarr') ? data.insulation : '',
    item_number: itemNumber,
    width: data.width || '',
    height: data.flow === 'generic' ? '' : (data.height || ''),
    windows: !!data.windows,
    window_style: data.window_style || '',
    glass_type: data.glass_type || '',
    color_base: data.color_base || '',
    color_overlay: data.color_overlay || data.color_base || '',
    construction: data.construction || '',
    insulated: !!data.insulated,
    customer_address: data.customer_address || '',
    notes: data.flow === 'generic' ? `Manual spec — ${data.model_label || ''}` : '',
  };
  await queueDoorOrder(payload);
  if (navigator.vibrate) { try { navigator.vibrate(200); } catch (e) { /* unsupported */ } }
  document.getElementById('doorwiz').innerHTML = `
    <div class="wiz-body" style="display:flex;flex-direction:column;align-items:center;justify-content:center;flex:1;gap:12px;">
      <div style="font-size:48px;">✓</div>
      <div style="font-size:18px;font-weight:800;">Sent to office</div>
    </div>`;
  setTimeout(() => { doorWiz = null; closeDoorWizardSilent(); confirmDoorOrderInFlight = false; }, 1300);
}

/* ─── Door order queue + sync ─────────────────────────────────────────────
 * Independent from the deduction queue (own IndexedDB store, own LS mirror,
 * own guard flag) so a stuck door-order sync can never block stock syncing
 * or vice versa. Shares the same Background Sync tag ('deductions') and the
 * same foreground trigger points (see requestSync / the 15s heartbeat). */

function lsReadDoorQueue() {
  try {
    const raw = localStorage.getItem(DOOR_QUEUE_LS_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch (e) { return []; }
}
function lsWriteDoorQueue(list) {
  try { localStorage.setItem(DOOR_QUEUE_LS_KEY, JSON.stringify(list)); } catch (e) { /* best effort */ }
}
function lsAddDoorOrder(order) {
  lsWriteDoorQueue([...lsReadDoorQueue().filter((o) => o.job_wo !== order.job_wo), order]);
}
function lsRemoveDoorOrder(jobWo) {
  lsWriteDoorQueue(lsReadDoorQueue().filter((o) => o.job_wo !== jobWo));
}

async function queueDoorOrder(payload) {
  lsAddDoorOrder(payload); // synchronous safety net first, same pattern as queueDeduction
  try {
    await idbSet('door_queue', payload.job_wo, payload);
  } catch (e) {
    console.error('door_queue IDB write failed', e);
  }
  await updateDoorPendingBadge();
  registerBgSync();
  pushDoorOrders();
}

async function pushDoorOrders() {
  if (doorSyncing) return;
  doorSyncing = true;
  try {
    const token = await getToken();
    if (!token) return;
    let pending = [];
    try { pending = await idbAll('door_queue'); } catch (e) { pending = lsReadDoorQueue(); }
    if (!pending.length) return;

    for (const order of pending) {
      try {
        const resp = await fetchWithTimeout('/tech/door-orders', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...order, token }),
        }, FETCH_TIMEOUT_MS);
        if (resp.status === 401) break; // revoked — the deduction sync path surfaces this
        if (resp.ok) {
          try { await idbDel('door_queue', order.job_wo); } catch (e) { /* best effort */ }
          lsRemoveDoorOrder(order.job_wo);
        } else if (resp.status === 409) {
          // The WO was already closed server-side — retrying can never succeed,
          // so drop it rather than let it retry forever, and tell the tech.
          try { await idbDel('door_queue', order.job_wo); } catch (e) { /* best effort */ }
          lsRemoveDoorOrder(order.job_wo);
          showSync(`WO ${order.job_wo} is already closed — door order not saved`, 'err');
        }
        // other non-ok statuses: leave queued, retried on the next trigger
      } catch (e) {
        // offline / timeout — leave queued
      }
    }
  } finally {
    doorSyncing = false;
    updateDoorPendingBadge();
  }
}

async function updateDoorPendingBadge() {
  const el = document.getElementById('doorpending');
  if (!el) return;
  if (doorWiz) { el.hidden = true; return; } // hidden while the wizard itself is open
  let pending = [];
  try { pending = await idbAll('door_queue'); } catch (e) { pending = lsReadDoorQueue(); }
  if (pending.length > 0) {
    el.textContent = `${pending.length} door order${pending.length > 1 ? 's' : ''} pending`;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

/* ─── My Jobs / Job Detail (capture) ──────────────────────────────────────────
 * Independent evidence record — NOT a deduction, NOT a door order. Own
 * IndexedDB store + LS mirror + guard flag, same shape as the door-order
 * queue above. A tech can capture a job even if it isn't in jobsToday (typed
 * in from Dispatch directly) — the server accepts any wo_number. */

let jobDetailWo = null;
let jobDraft = null;
let closeJobInFlight = false;
let jobSyncing = false;

const PAYMENT_METHOD_LABELS = { card: 'Card', cash: 'Cash', check: 'Check', not_collected: 'Not Collected' };
const NOT_COLLECTED_REASONS = [
  'Pending customer', 'Landlord/tenant pending', 'Insurance pending',
  'Warranty covers full amount', 'Other',
];

function jobStatusLabel(status) {
  return {
    pending: 'Pending', in_progress: 'In progress', captured: 'Captured',
    not_collected: 'Not collected', removed_by_dispatch: 'Removed',
  }[status] || status;
}

function formatJobWindow(start, end) {
  if (!start) return '';
  try {
    const opts = { hour: 'numeric', minute: '2-digit' };
    const sTxt = new Date(start).toLocaleTimeString([], opts);
    if (!end) return sTxt;
    return `${sTxt} – ${new Date(end).toLocaleTimeString([], opts)}`;
  } catch (e) { return start; }
}

function openMyJobs() {
  document.getElementById('myjobs').hidden = false;
  document.getElementById('detail').hidden = true;
  document.getElementById('doorwiz').hidden = true;
  document.getElementById('q').style.display = 'none';
  hideFloatingIndicators();
  renderMyJobsList();
  refreshJobsToday(); // pull-to-open acts as the "pull to refresh" trigger
}

function closeMyJobs() {
  const el = document.getElementById('myjobs');
  el.hidden = true;
  el.innerHTML = '';
  document.getElementById('q').style.display = '';
  updatePendingBadge();
  updateDoorPendingBadge();
  updateJobPendingBadge();
}

function jobCard(j) {
  const cls = j.status === 'captured' ? 'ok' : j.status === 'not_collected' ? 'warn'
    : j.status === 'removed_by_dispatch' ? 'danger' : '';
  const window = formatJobWindow(j.window_start, j.window_end);
  return `
  <div class="job-card" onclick="openJobDetail('${j.wo_number}')">
    <div class="job-card-top">
      <span class="job-wo">WO# ${escapeHtml(j.wo_number)}</span>
      <span class="badge ${cls}">${escapeHtml(jobStatusLabel(j.status))}</span>
    </div>
    <div class="job-address">${escapeHtml(j.address || 'No address on file')}</div>
    <div class="job-meta">${escapeHtml(window)}${j.job_type ? ' · ' + escapeHtml(j.job_type) : ''}</div>
  </div>`;
}

function renderMyJobsList() {
  const el = document.getElementById('myjobs');
  const header = `
    <div class="wiz-header">
      <button class="btn back" onclick="closeMyJobs()" aria-label="Back">←</button>
      <div class="wiz-title">My Jobs</div>
      <div style="width:44px"></div>
    </div>
    <div class="job-upload-row">
      <button type="button" class="btn" onclick="document.getElementById('screenshot-input').click()">📷 Upload today's schedule</button>
      <input type="file" id="screenshot-input" accept="image/*" style="display:none" onchange="onScreenshotSelected(this)">
    </div>`;
  if (!jobsToday.length) {
    el.innerHTML = header + '<div class="wiz-empty">No jobs loaded for today. Check back after 7am, or upload a screenshot of your Dispatch schedule above.</div>';
    return;
  }
  const sorted = [...jobsToday].sort((a, b) => (a.window_start || '').localeCompare(b.window_start || ''));
  el.innerHTML = header + `<div class="job-list">${sorted.map(jobCard).join('')}</div>`;
}

/* ─── Dispatch screenshot upload (replaces the scraper) ──────────────────
 * The tech screenshots their own Dispatch "My Jobs" view (zero ToS risk —
 * it's their own legitimate access) and uploads it here. Same parse -> review
 * -> confirm shape as door captures: nothing reaches expected_jobs until the
 * tech explicitly confirms the (possibly corrected) list on this screen. A
 * misread WO# here is exactly as costly as one in a money field — it's the
 * same key the weekly Winston reconciliation joins on. */

let screenshotParsedJobs = null;

async function onScreenshotSelected(input) {
  const file = input.files && input.files[0];
  input.value = ''; // allow re-selecting the same file later
  if (!file) return;

  const el = document.getElementById('myjobs');
  el.innerHTML = `
    <div class="wiz-header"><div class="wiz-title">My Jobs</div></div>
    <div class="wiz-empty">Reading schedule…</div>`;

  try {
    const token = await getToken();
    const fd = new FormData();
    fd.append('file', file, file.name || 'schedule.jpg');
    const resp = await fetchWithTimeout(
      `/tech/jobs/parse-screenshot?token=${encodeURIComponent(token)}`,
      { method: 'POST', body: fd }, 30000,
    );
    const data = await resp.json();
    if (!resp.ok || data.status !== 'ok') {
      throw new Error(data.detail || data.error || `parse failed (${resp.status})`);
    }
    screenshotParsedJobs = data.jobs || [];
    renderScreenshotReview(screenshotParsedJobs, data.warnings || []);
  } catch (e) {
    el.innerHTML = `
      <div class="wiz-header"><div class="wiz-title">My Jobs</div></div>
      <div class="wiz-empty">Couldn't read that screenshot — check connection and try again.<br><br>
      <button class="btn" onclick="renderMyJobsList()">Back</button></div>`;
  }
}

function renderScreenshotReview(jobs, warnings) {
  const warningsByRow = {};
  for (const w of warnings) {
    if (w.row_index !== null && w.row_index !== undefined) {
      (warningsByRow[w.row_index] = warningsByRow[w.row_index] || []).push(w);
    }
  }
  const header = `
    <div class="wiz-header">
      <button class="btn back" onclick="renderMyJobsList()" aria-label="Cancel">✕</button>
      <div class="wiz-title">Confirm Schedule</div>
      <div style="width:44px"></div>
    </div>`;
  if (!jobs.length) {
    document.getElementById('myjobs').innerHTML = header +
      '<div class="wiz-empty">No jobs found in that screenshot. Try again with a clearer photo.</div>';
    return;
  }
  // Every parsed field is editable here — this review screen is the ONLY
  // chance to correct the AI's read (addresses and names get misread as
  // easily as WO#s), because once confirmed a job is locked from edits.
  // Wrapped in its own block so a label+input pair stays stacked when several
  // are placed side by side inside .screenshot-field-row (a flex container).
  const field = (i, name, label, value, placeholder) => `
    <div class="screenshot-field-block">
      <label class="screenshot-field-label">${escapeHtml(label)}</label>
      <input type="text" class="screenshot-field" data-idx="${i}" data-field="${name}"
        value="${escapeHtml(value || '')}" placeholder="${escapeHtml(placeholder || '')}">
    </div>`;

  const rows = jobs.map((j, i) => `
    <div class="job-card">
      <label class="screenshot-field-label">WO#</label>
      <input type="text" class="screenshot-field" data-idx="${i}" data-field="wo_number"
        value="${escapeHtml(j.wo_number || '')}"
        style="${warningsByRow[i] ? 'border:2px solid var(--danger)' : ''}">
      ${warningsByRow[i] ? `<div class="text-xs" style="color:var(--danger)">⚠ ${escapeHtml(warningsByRow[i][0].message)}</div>` : ''}
      ${field(i, 'address', 'Address', j.address, 'Street address')}
      <div class="screenshot-field-row">
        ${field(i, 'city', 'City', j.city, '')}
        ${field(i, 'state', 'State', j.state, '')}
        ${field(i, 'zip', 'Zip', j.zip, '')}
      </div>
      <div class="screenshot-field-row">
        ${field(i, 'window_start', 'Window start', j.window_start, '8:00am')}
        ${field(i, 'window_end', 'Window end', j.window_end, '1:00pm')}
      </div>
      ${field(i, 'job_type', 'Job type', j.job_type, 'e.g. In Person')}
    </div>`).join('');
  document.getElementById('myjobs').innerHTML = header +
    `<p class="text-sm" style="padding:0 4px 8px;color:var(--muted)">Found ${jobs.length} job(s) — check every field against the screenshot and fix anything wrong. Once confirmed, these can't be edited here.</p>` +
    `<div class="job-list">${rows}</div>` +
    `<div class="wiz-nav"><button class="btn confirm" onclick="confirmScreenshotJobs()">✓ Looks right, add these jobs</button></div>`;
}

let confirmScreenshotInFlight = false;

async function confirmScreenshotJobs() {
  if (confirmScreenshotInFlight || !screenshotParsedJobs) return;
  confirmScreenshotInFlight = true;
  const jobs = screenshotParsedJobs.map((j) => ({ ...j }));
  document.querySelectorAll('.screenshot-field').forEach((input) => {
    const idx = parseInt(input.getAttribute('data-idx'), 10);
    const name = input.getAttribute('data-field');
    if (jobs[idx] && name) jobs[idx][name] = input.value.trim();
  });

  try {
    const token = await getToken();
    const resp = await fetchWithTimeout('/tech/jobs/confirm-screenshot', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token, jobs }),
    }, FETCH_TIMEOUT_MS);
    if (resp.ok) {
      screenshotParsedJobs = null;
      await refreshJobsToday();
      renderMyJobsList();
    } else {
      alert('Could not save the schedule — check connection and try again.');
    }
  } catch (e) {
    alert('Could not save the schedule — check connection and try again.');
  } finally {
    confirmScreenshotInFlight = false;
  }
}

// A job can need several distinct notes (what was done, what was recommended,
// what the customer declined). They're kept as separate entries while the tech
// types and joined into the single closing_notes text on submit.
function joinClosingNotes(notes) {
  return (notes || []).map((n) => n.trim()).filter(Boolean).join('\n');
}

function jobCaptureReady() {
  if (!jobDraft) return false;
  if (!joinClosingNotes(jobDraft.closing_notes)) return false;
  if (!jobDraft.payment_method) return false;
  if (jobDraft.payment_method === 'card' && !jobDraft.photoFile) return false;
  return true;
}

function updateClosingNote(idx, value) {
  jobDraft.closing_notes[idx] = value;
  updateCloseJobButtonState();
}

function addClosingNote() {
  jobDraft.closing_notes.push('');
  renderJobDetail();
}

function removeClosingNote(idx) {
  jobDraft.closing_notes.splice(idx, 1);
  if (!jobDraft.closing_notes.length) jobDraft.closing_notes.push('');
  renderJobDetail();
}

function updateCloseJobButtonState() {
  const btn = document.querySelector('#jobdetail .wiz-nav .btn.confirm');
  if (btn) btn.disabled = !jobCaptureReady();
}

function openJobDetail(woNumber) {
  const job = jobsToday.find((j) => j.wo_number === woNumber);
  if (!job) return;
  jobDetailWo = woNumber;
  jobDraft = {
    closing_notes: [''], payment_method: '', amount_charged: '',
    check_number: '', not_collected_reason: '', not_collected_notes: '', photoFile: null,
  };
  document.getElementById('jobdetail').hidden = false;
  document.getElementById('myjobs').hidden = true;
  hideFloatingIndicators();
  renderJobDetail();
}

function closeJobDetail() {
  const el = document.getElementById('jobdetail');
  el.hidden = true;
  el.innerHTML = '';
  jobDetailWo = null;
  jobDraft = null;
  openMyJobs();
}

function chooseJobPayment(method) {
  jobDraft.payment_method = method;
  renderJobDetail();
}

function onJobPhotoSelected(input) {
  const file = input.files && input.files[0];
  if (file) jobDraft.photoFile = file;
  renderJobDetail();
}

function isJobClosed(job) {
  return job && (job.status === 'captured' || job.status === 'not_collected');
}

function renderJobDetail() {
  const job = jobsToday.find((j) => j.wo_number === jobDetailWo);
  const el = document.getElementById('jobdetail');
  if (!job || !jobDraft) { el.hidden = true; return; }

  const header = `
    <div class="wiz-header">
      <button class="btn back" onclick="closeJobDetail()" aria-label="Back">←</button>
      <div class="wiz-title">WO# ${escapeHtml(job.wo_number)}</div>
      <div style="width:44px"></div>
    </div>
    <div class="job-detail-header">
      <div class="job-detail-address">${escapeHtml(job.address || 'No address on file')}</div>
      <div class="job-detail-meta">${escapeHtml(formatJobWindow(job.window_start, job.window_end))}${job.warranty_company ? ' · ' + escapeHtml(job.warranty_company) : ''}</div>
    </div>`;

  // A closed job is the evidence record — it can only be viewed, never
  // re-edited or re-closed (the server rejects a second close too).
  if (isJobClosed(job)) {
    const rows = [
      ['Status', jobStatusLabel(job.status)],
      ['Job type', job.job_type],
      ['Warranty co.', job.warranty_company],
    ].filter(([, v]) => v);
    el.innerHTML = header + `
      <div class="wiz-body">
        <div class="job-locked-banner">🔒 This job is closed — recorded and locked.</div>
        <div class="review-card">
          ${rows.map(([k, v]) => `
            <div class="review-row">
              <span class="review-label">${escapeHtml(k)}</span>
              <span class="review-value">${escapeHtml(v)}</span>
            </div>`).join('')}
        </div>
        <p class="text-sm" style="color:var(--muted);margin-top:10px">Closing notes and payment details are on the office dashboard. If something needs correcting, contact the office.</p>
      </div>`;
    return;
  }

  const paymentButtons = Object.keys(PAYMENT_METHOD_LABELS).map((m) => `
    <button class="wiz-option payment-option ${jobDraft.payment_method === m ? 'selected' : ''}" onclick="chooseJobPayment('${m}')">${PAYMENT_METHOD_LABELS[m]}</button>
  `).join('');

  let paymentDetail = '';
  if (jobDraft.payment_method === 'card') {
    paymentDetail = `
      <label class="job-field-label mt-sm">Amount charged</label>
      <input type="number" inputmode="decimal" step="0.01" value="${jobDraft.amount_charged}" oninput="jobDraft.amount_charged = this.value" placeholder="0.00">
      <label class="job-field-label mt-sm">Receipt photo (required)</label>
      <input type="file" accept="image/*" onchange="onJobPhotoSelected(this)">
      ${jobDraft.photoFile ? `<div class="job-photo-preview-name">📷 ${escapeHtml(jobDraft.photoFile.name)}</div>` : ''}`;
  } else if (jobDraft.payment_method === 'cash') {
    paymentDetail = `
      <label class="job-field-label mt-sm">Amount charged</label>
      <input type="number" inputmode="decimal" step="0.01" value="${jobDraft.amount_charged}" oninput="jobDraft.amount_charged = this.value" placeholder="0.00">`;
  } else if (jobDraft.payment_method === 'check') {
    paymentDetail = `
      <label class="job-field-label mt-sm">Amount charged</label>
      <input type="number" inputmode="decimal" step="0.01" value="${jobDraft.amount_charged}" oninput="jobDraft.amount_charged = this.value" placeholder="0.00">
      <label class="job-field-label mt-sm">Check number</label>
      <input type="text" value="${escapeHtml(jobDraft.check_number)}" oninput="jobDraft.check_number = this.value">`;
  } else if (jobDraft.payment_method === 'not_collected') {
    paymentDetail = `
      <label class="job-field-label mt-sm">Reason</label>
      <select onchange="jobDraft.not_collected_reason = this.value">
        <option value="">— Select —</option>
        ${NOT_COLLECTED_REASONS.map((r) => `<option value="${escapeHtml(r)}" ${jobDraft.not_collected_reason === r ? 'selected' : ''}>${escapeHtml(r)}</option>`).join('')}
      </select>
      <label class="job-field-label mt-sm">Notes</label>
      <textarea oninput="jobDraft.not_collected_notes = this.value">${escapeHtml(jobDraft.not_collected_notes)}</textarea>`;
  }

  const notesBlocks = jobDraft.closing_notes.map((note, i) => `
    <div class="closing-note">
      <textarea placeholder="${i === 0 ? 'What was done, recommended, declined...' : 'Additional note...'}"
        oninput="updateClosingNote(${i}, this.value)">${escapeHtml(note)}</textarea>
      ${jobDraft.closing_notes.length > 1
        ? `<button type="button" class="closing-note-remove" onclick="removeClosingNote(${i})" aria-label="Remove this note">✕</button>`
        : ''}
    </div>`).join('');

  const body = `
    <div class="wiz-body">
      <label class="job-field-label">Work performed (closing notes)</label>
      ${notesBlocks}
      <button type="button" class="btn add-note-btn" onclick="addClosingNote()">+ Add another note</button>
      <label class="job-field-label mt-sm">Payment</label>
      <div class="payment-grid">${paymentButtons}</div>
      ${paymentDetail}
    </div>
    <div class="wiz-nav">
      <button class="btn confirm" ${jobCaptureReady() ? '' : 'disabled'} onclick="confirmCloseJob()">✓ Close Job</button>
    </div>`;

  el.innerHTML = header + body;
}

// Same double-tap guard as confirmDoorOrder(): async + a real window before
// the DOM is replaced where a second tap could re-run this.
async function confirmCloseJob() {
  if (closeJobInFlight || !jobCaptureReady()) return;
  closeJobInFlight = true;
  const job = jobsToday.find((j) => j.wo_number === jobDetailWo);
  const draft = jobDraft;
  await queueJobCapture(job, draft);
  // A closed WO can take no more parts or door orders — release it so the
  // tech is forced to pick the next job before doing anything else.
  if (activeWo === job.wo_number) clearActiveWo();
  if (navigator.vibrate) { try { navigator.vibrate(200); } catch (e) { /* unsupported */ } }
  document.getElementById('jobdetail').innerHTML = `
    <div class="wiz-body" style="display:flex;flex-direction:column;align-items:center;justify-content:center;flex:1;gap:12px;">
      <div style="font-size:48px;">✓</div>
      <div style="font-size:18px;font-weight:800;">Job closed</div>
      <div style="font-size:14px;opacity:0.75;text-align:center;">Pick your next WO before deducting more parts.</div>
    </div>`;
  setTimeout(() => {
    jobDetailWo = null;
    jobDraft = null;
    closeJobInFlight = false;
    document.getElementById('jobdetail').hidden = true;
    document.getElementById('jobdetail').innerHTML = '';
    openMyJobs();
  }, 1300);
}

/* ─── Job capture queue + sync ────────────────────────────────────────────
 * Own store, own LS mirror, own guard flag — a stuck job-capture sync can
 * never block stock or door-order syncing. Shares the 'deductions' Background
 * Sync tag (same one-tag-for-everything convention as door orders). The
 * receipt photo (a Blob) is IndexedDB-only: localStorage can't hold binary,
 * so the LS mirror carries only the JSON metadata — enough to know a capture
 * is pending, not enough to resubmit the photo from LS alone. */

function lsReadJobCaptureQueue() {
  try {
    const raw = localStorage.getItem(JOB_CAPTURE_QUEUE_LS_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch (e) { return []; }
}
function lsWriteJobCaptureQueue(list) {
  try { localStorage.setItem(JOB_CAPTURE_QUEUE_LS_KEY, JSON.stringify(list)); } catch (e) { /* best effort */ }
}
function lsAddJobCaptureMeta(record) {
  const { photoFile, ...meta } = record;
  lsWriteJobCaptureQueue([...lsReadJobCaptureQueue().filter((r) => r.capture_id !== record.capture_id), meta]);
}
function lsRemoveJobCapture(captureId) {
  lsWriteJobCaptureQueue(lsReadJobCaptureQueue().filter((r) => r.capture_id !== captureId));
}

async function queueJobCapture(job, draft) {
  const captureId = uuid();
  const record = {
    capture_id: captureId,
    batch_id: captureId,
    wo_number: job.wo_number,
    payment_method: draft.payment_method,
    amount_charged: draft.amount_charged ? parseFloat(draft.amount_charged) : null,
    check_number: draft.check_number || '',
    not_collected_reason: draft.not_collected_reason
      ? draft.not_collected_reason + (draft.not_collected_notes ? ` — ${draft.not_collected_notes}` : '')
      : '',
    closing_notes: joinClosingNotes(draft.closing_notes),
    captured_at: new Date().toISOString(),
    photoFile: draft.photoFile || null,
    sync_attempts: 0,
  };
  lsAddJobCaptureMeta(record); // synchronous safety net first, same pattern as queueDeduction/queueDoorOrder
  try {
    await idbSet('job_capture_queue', captureId, record);
  } catch (e) {
    console.error('job_capture_queue IDB write failed', e);
  }
  // Optimistic local status so "My Jobs" doesn't show stale "pending" until
  // the next /tech/jobs/today refresh confirms the server-side status.
  const localJob = jobsToday.find((j) => j.wo_number === job.wo_number);
  if (localJob) localJob.status = draft.payment_method === 'not_collected' ? 'not_collected' : 'captured';
  await updateJobPendingBadge();
  registerBgSync();
  pushJobCaptures();
}

async function pushJobCaptures() {
  if (jobSyncing) return;
  jobSyncing = true;
  try {
    const token = await getToken();
    if (!token) return;
    let pending = [];
    try { pending = await idbAll('job_capture_queue'); } catch (e) { pending = lsReadJobCaptureQueue(); }
    if (!pending.length) return;

    for (const record of pending) {
      try {
        const resp = await fetchWithTimeout('/tech/job-captures', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            token, batch_id: record.batch_id, wo_number: record.wo_number,
            payment_method: record.payment_method, amount_charged: record.amount_charged,
            check_number: record.check_number, not_collected_reason: record.not_collected_reason,
            closing_notes: record.closing_notes, captured_at: record.captured_at,
          }),
        }, FETCH_TIMEOUT_MS);
        if (resp.status === 401) break; // revoked — deduction sync path surfaces this
        if (resp.status === 409) {
          // This WO was already closed (a capture for it exists server-side —
          // e.g. the same close synced from another device). Retrying can
          // never succeed, so drop it instead of retrying forever.
          try { await idbDel('job_capture_queue', record.capture_id); } catch (e) {}
          lsRemoveJobCapture(record.capture_id);
          showSync(`WO ${record.wo_number} was already closed`, 'warn');
          continue;
        }

        if (resp.ok) {
          let photoOk = true;
          if (record.photoFile) {
            try {
              const fd = new FormData();
              fd.append('batch_id', record.batch_id);
              fd.append('file', record.photoFile, record.photoFile.name || 'receipt.jpg');
              const photoResp = await fetchWithTimeout(
                `/tech/job-captures/photo?token=${encodeURIComponent(token)}`,
                { method: 'POST', body: fd }, FETCH_TIMEOUT_MS,
              );
              photoOk = photoResp.ok;
            } catch (e) { photoOk = false; }
          }
          if (photoOk) {
            try { await idbDel('job_capture_queue', record.capture_id); } catch (e) {}
            lsRemoveJobCapture(record.capture_id);
          } else {
            // Capture itself is recorded server-side (idempotent retry won't
            // duplicate it); only the photo needs to be retried next cycle.
            record.sync_attempts = (record.sync_attempts || 0) + 1;
            try { await idbSet('job_capture_queue', record.capture_id, record); } catch (e) {}
          }
        } else {
          record.sync_attempts = (record.sync_attempts || 0) + 1;
          try { await idbSet('job_capture_queue', record.capture_id, record); } catch (e) {}
        }
      } catch (e) {
        record.sync_attempts = (record.sync_attempts || 0) + 1;
        try { await idbSet('job_capture_queue', record.capture_id, record); } catch (e) {}
      }
    }
  } finally {
    jobSyncing = false;
    updateJobPendingBadge();
  }
}

async function updateJobPendingBadge() {
  const el = document.getElementById('jobpending');
  if (!el) return;
  if (jobDetailWo) { el.hidden = true; return; } // hidden while Job Detail itself is open
  let pending = [];
  try { pending = await idbAll('job_capture_queue'); } catch (e) { pending = lsReadJobCaptureQueue(); }
  if (!pending.length) { el.hidden = true; return; }
  const maxAttempts = pending.reduce((m, r) => Math.max(m, r.sync_attempts || 0), 0);
  el.hidden = false;
  if (maxAttempts >= 3) {
    el.textContent = `${pending.length} job sync failed — tap to retry`;
    el.className = 'job-pending-badge danger';
    el.onclick = () => pushJobCaptures();
  } else {
    el.textContent = `${pending.length} job${pending.length > 1 ? 's' : ''} pending sync`;
    el.className = 'job-pending-badge';
    el.onclick = null;
  }
}

/* ─── Web Push subscribe (end-of-day sync reminder, iOS 16.4+) ─────────────
 * Best-effort: a tech who never gets a push notification still has every
 * other sync path (foreground heartbeat, Background Sync on Android). Push
 * is a reminder layered on top, never a requirement. */

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = atob(base64);
  const arr = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; i++) arr[i] = rawData.charCodeAt(i);
  return arr;
}

async function subscribeToPush(token) {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;
  try {
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      const keyResp = await fetchWithTimeout('/tech/push/vapid-public-key', {}, 8000);
      const { key } = await keyResp.json();
      if (!key) return; // push not configured server-side — silent no-op
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(key),
      });
    }
    const json = sub.toJSON();
    await fetchWithTimeout('/tech/push/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token, endpoint: json.endpoint, keys: json.keys }),
    }, 8000);
  } catch (e) {
    console.error('push subscribe failed', e); // never blocks boot
  }
}

/* ─── Install hint (iOS Safari specific) ─────────────────────────────────────
 * iOS treats an installed (home-screen) PWA's storage as meaningfully more
 * persistent than a Safari tab, which is subject to more aggressive
 * IndexedDB/localStorage eviction. Nudge techs running in a plain Safari tab
 * to install, without nagging once dismissed. */

function isStandalone() {
  return window.navigator.standalone === true
    || (typeof matchMedia === 'function' && matchMedia('(display-mode: standalone)').matches);
}

function isIOS() {
  // iPadOS 13+ reports as "MacIntel" in the UA; touch points distinguish it
  // from an actual Mac.
  return /iPad|iPhone|iPod/.test(navigator.userAgent)
    || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
}

const INSTALL_HINT_DISMISSED_KEY = 'doorflex.installHintDismissed';

function maybeShowInstallHint() {
  if (isStandalone() || !isIOS()) return;
  try { if (localStorage.getItem(INSTALL_HINT_DISMISSED_KEY)) return; } catch (e) { /* ignore */ }
  const el = document.getElementById('installhint');
  if (el) el.hidden = false;
}

function dismissInstallHint() {
  const el = document.getElementById('installhint');
  if (el) el.hidden = true;
  try { localStorage.setItem(INSTALL_HINT_DISMISSED_KEY, '1'); } catch (e) { /* ignore */ }
}

/* ─── Boot ───────────────────────────────────────────────────────────────── */

let activeSyncAbort = null;

// iOS fires online/focus/pageshow/visibilitychange in near-simultaneous
// bursts on foreground-return. Only treat a sync as "stuck" (worth aborting)
// once it's been running past this grace period — otherwise a burst of
// recovery events aborts and immediately re-starts a sync that was already
// in flight and about to finish on its own.
const SYNC_STUCK_GRACE_MS = 4000;

function resetSync() {
  clearRetry();
  if (syncing && activeSyncAbort && (Date.now() - syncStartedAt) > SYNC_STUCK_GRACE_MS) {
    try { activeSyncAbort.abort(); } catch (e) { /* already aborted */ }
    syncing = false;
  }
}

function registerBgSync() {
  if (!('serviceWorker' in navigator)) return;
  // Fire-and-forget: keeps the 'deductions' tag registered so Chrome/Android
  // wake the SW and push pending batches even if the app is killed. Must never
  // block or throw.
  navigator.serviceWorker.ready.then((reg) => {
    if (reg && reg.sync) return reg.sync.register('deductions');
    return Promise.resolve();
  }).catch(() => { /* unsupported / registration failed */ });
}

/* Sync on ANY signal-recovery path. These listeners are registered FIRST in
 * boot(), before any await — if IndexedDB or the SW initialization stalls,
 * the app still reacts the moment the tech returns to it: focus, pageshow
 * (back/forward cache), visibilitychange, online, network-info change. */
let requestSyncAttempts = 0;
let dbUnavailable = false;
const REQUEST_SYNC_MAX_ATTEMPTS = 20;

function requestSync() {
  if (dbUnavailable) return;
  if (!db) {
    requestSyncAttempts += 1;
    if (requestSyncAttempts > REQUEST_SYNC_MAX_ATTEMPTS) {
      // IndexedDB never came up (Safari private mode, Lockdown Mode, or an
      // otherwise broken environment). Stop polling every 500ms forever —
      // that just burns battery/CPU with nothing to show for it — and say
      // so once instead.
      dbUnavailable = true;
      const sub = document.getElementById('sub');
      if (sub) sub.textContent = 'Storage unavailable in this browser mode — reload the app, or turn off Private/Lockdown mode.';
      return;
    }
    // Backs off instead of a tight 500ms loop: quick at first (db may be
    // about to finish opening), slower the longer it stays unavailable.
    const delay = Math.min(500 * Math.pow(1.4, requestSyncAttempts), 10000);
    setTimeout(requestSync, delay);
    return;
  }
  requestSyncAttempts = 0;
  syncNow();
  pushDoorOrders();
  pushJobCaptures();
}

async function boot() {
  window.addEventListener('online', () => { resetSync(); requestSync(); });
  window.addEventListener('focus', () => { resetSync(); requestSync(); });
  window.addEventListener('pageshow', (e) => { if (e.persisted) { resetSync(); requestSync(); } });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) { resetSync(); requestSync(); }
  });
  // Network Information API: sync as soon as the connection returns (faster
  // than the `online` event on some iOS builds).
  if (navigator.connection) {
    navigator.connection.addEventListener('change', () => {
      if (navigator.onLine) { resetSync(); requestSync(); }
    });
  }

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('./sw.js').catch(() => {});

    // skipWaiting()+clients.claim() in sw.js hand fetch control to a new SW
    // immediately, but the *already-running* page keeps executing its old
    // in-memory JS/CSS until a real reload happens — without this, a shipped
    // fix (e.g. a scroll or wizard bug) stays invisible in an already-open
    // app until the tech happens to force-quit and reopen it. Reload once,
    // guarded so a flaky double-fire can't loop.
    let reloadedForNewSw = false;
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (reloadedForNewSw) return;
      reloadedForNewSw = true;
      location.reload();
    });

    // Background Sync (Chrome/Android): push pending even if the app was closed.
    // Never await this — `ready` can stall while the SW activates and boot must
    // not depend on it (the foreground listeners above are the iOS safety net).
    registerBgSync();

    // The SW tells us it pushed a batch → refresh badge + mirror the UI state.
    navigator.serviceWorker.addEventListener('message', (e) => {
      if (e.data && e.data.type === 'synced') {
        updatePendingBadge();
        requestSync();
      }
    });
  }

  db = await openDb();
  const token = await getToken();
  if (!token) {
    const sub = document.getElementById('sub');
    sub.textContent = 'No link detected — open your install link.';
    // Standalone (Home Screen) launches are the case the dynamic manifest
    // start_url fix targets (see /tech/pwa/manifest.json in main.py), but
    // that only helps installs made after this fix shipped — an icon added
    // before it can still hit this screen. Give a way out that doesn't
    // require the tech to understand why: dropping back to a plain Safari
    // tab always works, since that's the one context that reliably has the
    // token already in storage from the original install-link visit.
    const standaloneHint = isStandalone()
      ? '<br><br><span style="font-size:13px">Opened from your Home Screen icon? Long-press it → Edit, turn off "Open as Web App" (or remove and re-add it), then open your install link once in Safari directly.</span>'
      : '';
    document.getElementById('grid').innerHTML =
      '<div class="empty">This app needs a one-time install link from your dispatcher.' + standaloneHint + '<br><br>' +
      '<button class="btn" onclick="location.reload()">Re-check</button></div>';
    return;
  }

  // Embed the token into the manifest's start_url (see /tech/pwa/manifest.json
  // in main.py) so a fresh "Add to Home Screen" launch can bootstrap itself
  // even if iOS gives the standalone app a storage partition separate from
  // this Safari tab. Must happen before the tech taps "Add to Home Screen",
  // so this runs on every boot, not just the first ?t= visit.
  try {
    const manifestLink = document.querySelector('link[rel="manifest"]');
    if (manifestLink) manifestLink.href = `./manifest.json?t=${encodeURIComponent(token)}`;
  } catch (e) { /* best effort */ }

  // Fresh pull first (the server is the source of truth); fall back to the
  // cached catalog when offline. After each sync cycle, refreshCatalog() pulls
  // again so the app always reflects the latest server values automatically.
  try {
    const data = await fetchCatalog(token);
    catalog = data.parts;
    const sub = document.getElementById('sub');
    sub.textContent = `${data.tech} · ${data.truck_id} · ${catalog.length} parts`;
  } catch (e) {
    if (e instanceof InvalidTokenError) {
      // Token revoked/rotated server-side: the app must NOT keep showing the
      // cached catalog. Drop the stored token and surface a reinstall state.
      await clearToken();
      const sub = document.getElementById('sub');
      sub.textContent = 'Link revoked — request a new install link.';
      document.getElementById('grid').innerHTML =
        '<div class="empty">This install link is no longer valid.<br><br>' +
        '<button class="btn" onclick="location.reload()">Re-check</button></div>';
      return;
    }
    catalog = await loadCatalog();
    const sub = document.getElementById('sub');
    sub.textContent = catalog.length
      ? 'Offline — cached catalog'
      : 'Offline & no cache';
  }

  // Jobs Today: same fetch-fresh-fall-back-to-cache shape as the catalog
  // above, but never blocks boot or replaces the "sub" status line — this is
  // secondary to the parts catalog on first paint.
  try {
    jobsToday = await fetchJobsToday(token);
  } catch (e) {
    jobsToday = await loadJobsTodayCache();
  }
  subscribeToPush(token); // fire-and-forget, never blocks boot

  // Build the pending-sync shadow BEFORE the first render so the grid shows
  // (server stock - pending) right away, even when reopening the app offline
  // or with unsynced batches in the queue.
  await rebuildPendingShadows();

  document.getElementById('q').addEventListener('input', (e) => {
    filter = e.target.value;
    renderGrid();
  });

  renderGrid();
  renderActiveWoBanner();
  updatePendingBadge();
  updateJobPendingBadge();
  maybeShowInstallHint();

  // A WO the tech closed on another device (or that the office closed) can't
  // stay active here — drop it so they're prompted for a fresh one.
  if (activeWo) {
    const stillOpen = jobsToday.find((j) => j.wo_number === activeWo);
    if (stillOpen && isJobClosed(stillOpen)) clearActiveWo();
  }

  // Sync triggers: explicit reconnect, app returning to foreground, SW push,
  // plus a scheduled retry while items are pending (see syncNow/scheduleRetry).
  //
  // IMPORTANT (iOS): while the page is suspended in the background a fetch
  // that was in flight can hang forever, leaving the `syncing` guard stuck.
  // Every foreground-recovery path resets `syncing = false` first so the sync
  // can actually run again without the tech having to re-enter the app.
  // (Listeners registered at the top of boot() so they exist even if the rest
  // of initialization stalls.)

  // Heartbeats while the app is visible:
  // - every 15s, if there are pending items, push them. iOS can miss the
  //   `online` event while the page was suspended, so this guarantees the
  //   queue drains automatically the moment the app is open again — no tap
  //   needed. (The 2s retry chain in syncNow covers the live-foreground case;
  //   this is the safety net that survives a suspend/resume cycle.)
  // - every 2min, silently refresh the catalog from the server so stock
  //   stays fresh even after a backgrounded connectivity change.
  setInterval(() => {
    if (document.hidden || syncing) return;
    loadPending().then((pending) => {
      if (pending.length) {
        registerBgSync();
        syncNow();
      }
    }).catch(() => { /* best effort */ });
    if (!doorSyncing) {
      idbAll('door_queue').then((pending) => {
        if (pending.length) { registerBgSync(); pushDoorOrders(); }
      }).catch(() => { /* best effort */ });
    }
    if (!jobSyncing) {
      idbAll('job_capture_queue').then((pending) => {
        if (pending.length) { registerBgSync(); pushJobCaptures(); }
      }).catch(() => { /* best effort */ });
    }
  }, 15_000);
  setInterval(() => {
    if (document.hidden) return;
    refreshCatalog();
    refreshJobsToday();
  }, 120_000);

  syncNow();
  pushDoorOrders();
  pushJobCaptures();
  updateDoorPendingBadge();
  updateJobPendingBadge();
}

boot().catch((e) => {
  console.error(e);
  document.getElementById('sub').textContent = 'App error — reload';
});
