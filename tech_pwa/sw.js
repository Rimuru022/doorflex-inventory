// Single source of truth for the asset cache-busting query string — SHELL
// below derives from it so a version bump can never drift between this list
// and index.html's own ?v=NN links again (that drift is what previously left
// SHELL pinned to stale ?v=25 URLs while VERSION had already moved to v27).
const ASSET_V = '31';
const VERSION = `v${ASSET_V}`;
const SHELL = [
  './',
  './index.html',
  `./app.css?v=${ASSET_V}`,
  `./app.js?v=${ASSET_V}`,
  `./manifest.json?v=${ASSET_V}`,
  './favicon.ico?v=7',
  './icons/favicon-16.png?v=7',
  './icons/favicon-32.png?v=7',
  './icons/icon-180.png?v=7',
  './icons/icon-192.png?v=7',
  './icons/icon-512.png?v=7',
];

const DB_NAME = 'doorflex-tech';
const DB_VERSION = 4;

let swDbPromise = null;

function swOpenDb() {
  if (swDbPromise) return swDbPromise;
  swDbPromise = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = (e) => {
      const d = e.target.result;
      if (!d.objectStoreNames.contains('kv')) d.createObjectStore('kv');
      // v2: queue store WITHOUT keyPath. Out-of-line keys are more portable
      // across iOS Safari builds and remove any ambiguity around
      // put(value, key) when keyPath is set.
      if (e.oldVersion < 2 && d.objectStoreNames.contains('queue')) {
        d.deleteObjectStore('queue');
      }
      if (!d.objectStoreNames.contains('queue')) d.createObjectStore('queue');
      // v3: must mirror app.js's schema exactly — whichever context (page or
      // SW) opens the DB first after a version bump runs this upgrade.
      if (!d.objectStoreNames.contains('door_queue')) d.createObjectStore('door_queue');
      if (!d.objectStoreNames.contains('lookup_cache')) d.createObjectStore('lookup_cache');
      // v4: must mirror app.js's schema exactly (see that file's comment).
      if (!d.objectStoreNames.contains('jobs_today')) d.createObjectStore('jobs_today');
      if (!d.objectStoreNames.contains('job_capture_queue')) d.createObjectStore('job_capture_queue');
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  return swDbPromise;
}

function idbGet(store, key) {
  return swOpenDb().then((db) => new Promise((resolve, reject) => {
    const tx = db.transaction(store, 'readonly');
    const r = tx.objectStore(store).get(key);
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  }));
}

function idbAll(store) {
  return swOpenDb().then((db) => new Promise((resolve, reject) => {
    const tx = db.transaction(store, 'readonly');
    const r = tx.objectStore(store).getAll();
    r.onsuccess = () => resolve(r.result || []);
    r.onerror = () => reject(r.error);
  }));
}

function idbDel(store, key) {
  return swOpenDb().then((db) => new Promise((resolve, reject) => {
    const tx = db.transaction(store, 'readwrite');
    tx.objectStore(store).delete(key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  }));
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(VERSION).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// Network-first for catalog, cache-first for the shell (ignoreSearch so the
// install link /tech/pwa/?t=... resolves offline too).
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith('/tech/catalog')) {
    event.respondWith(
      // Timeout, not a bare fetch: Safari can hang a request instead of
      // failing it, which would otherwise strand this response and never
      // fall back to the cache.
      swFetchWithTimeout(event.request, {}).then((resp) => {
        const copy = resp.clone();
        caches.open(VERSION).then((c) => c.put(event.request, copy));
        return resp;
      }).catch(() => caches.match(event.request))
    );
    return;
  }
  if (url.pathname.startsWith('/tech/pwa') || url.pathname.endsWith('/tech/pwa/')) {
    event.respondWith(
      caches.match(event.request, { ignoreSearch: true }).then(
        (hit) => hit || swFetchWithTimeout(event.request, {}).then((resp) => {
          const copy = resp.clone();
          caches.open(VERSION).then((c) => c.put(event.request, copy));
          return resp;
        })
      )
    );
    return;
  }
  // Everything else (API writes): don't cache, let it go to network.
});

// Background Sync (Chrome/Android): the SW reads the queue directly from
// IndexedDB and pushes pending batches even if the app was closed. iOS Safari
// has no Background Sync — there the client syncs on open/focus/online.
self.addEventListener('sync', (event) => {
  if (event.tag === 'deductions') {
    event.waitUntil(pushPending().then((anyFailed) => {
      if (anyFailed) {
        // Re-register so the browser fires the sync event again on the next
        // connectivity change, even if our rejection didn't trigger it.
        return self.registration.sync.register('deductions').catch(() => {});
      }
    }));
  }
});

function swFetchWithTimeout(url, opts, ms) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), ms || 12000);
  return fetch(url, { ...opts, signal: ctrl.signal }).finally(() => clearTimeout(t));
}

async function pushPending() {
  let token;
  try {
    token = await idbGet('kv', 'token');
  } catch (e) {
    return;
  }
  if (!token) return;
  let pending;
  try {
    pending = await idbAll('queue');
  } catch (e) {
    pending = [];
  }

  let anySynced = false;
  let anyFailed = false;
  for (const batch of pending) {
    try {
      const resp = await swFetchWithTimeout('/tech/deductions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token, batch_id: batch.batch_id, items: batch.items }),
      });
      if (resp.ok) {
        await idbDel('queue', batch.batch_id);
        anySynced = true;
      } else {
        anyFailed = true;
      }
    } catch (e) {
      anyFailed = true;
    }
  }

  // Door orders — separate store, separate endpoint, never touches stock.
  // Pushed under the same 'deductions' Background Sync tag (no need for a
  // second tag registration) so the app is killed → reopened flow still
  // drains both queues automatically.
  let doorPending;
  try {
    doorPending = await idbAll('door_queue');
  } catch (e) {
    doorPending = [];
  }
  for (const order of doorPending) {
    try {
      const resp = await swFetchWithTimeout('/tech/door-orders', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...order, token }),
      });
      if (resp.ok) {
        await idbDel('door_queue', order.job_wo);
        anySynced = true;
      } else if (resp.status === 409) {
        // WO already closed server-side — a retry can never succeed, so drop
        // it rather than retry forever (the page surfaces this too).
        await idbDel('door_queue', order.job_wo);
      } else if (resp.status !== 401) {
        anyFailed = true;
      }
    } catch (e) {
      anyFailed = true;
    }
  }

  // Job captures — independent evidence record, own store. Pushed under the
  // same 'deductions' Background Sync tag (no second tag needed).
  let jobCapturePending;
  try {
    jobCapturePending = await idbAll('job_capture_queue');
  } catch (e) {
    jobCapturePending = [];
  }
  for (const record of jobCapturePending) {
    try {
      const resp = await swFetchWithTimeout('/tech/job-captures', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          token, batch_id: record.batch_id, wo_number: record.wo_number,
          payment_method: record.payment_method, amount_charged: record.amount_charged,
          check_number: record.check_number, not_collected_reason: record.not_collected_reason,
          closing_notes: record.closing_notes, captured_at: record.captured_at,
        }),
      });
      if (resp.status === 401) break;
      if (resp.status === 409) {
        // Already closed server-side (e.g. the same close synced from another
        // device) — permanent, so drop it instead of retrying forever.
        await idbDel('job_capture_queue', record.capture_id);
        continue;
      }
      if (resp.ok) {
        let photoOk = true;
        if (record.photoFile) {
          try {
            const fd = new FormData();
            fd.append('batch_id', record.batch_id);
            fd.append('file', record.photoFile, record.photoFile.name || 'receipt.jpg');
            const photoResp = await swFetchWithTimeout(
              `/tech/job-captures/photo?token=${encodeURIComponent(token)}`,
              { method: 'POST', body: fd },
            );
            photoOk = photoResp.ok;
          } catch (e) { photoOk = false; }
        }
        if (photoOk) {
          await idbDel('job_capture_queue', record.capture_id);
          anySynced = true;
        } else {
          anyFailed = true; // capture itself is recorded; only the photo retries
        }
      } else {
        anyFailed = true;
      }
    } catch (e) {
      anyFailed = true;
    }
  }

  if (anySynced) {
    try {
      const clients = await self.clients.matchAll();
      for (const client of clients) {
        client.postMessage({ type: 'synced' });
      }
    } catch (e) { /* no clients */ }
  }
  return anyFailed;
}

// Web Push (VAPID) — end-of-day sync reminder, iOS 16.4+ installed PWAs.
self.addEventListener('push', (event) => {
  let data = { title: 'DoorFlex', body: 'You have updates.' };
  try { if (event.data) data = event.data.json(); } catch (e) { /* non-JSON payload */ }
  event.waitUntil(
    self.registration.showNotification(data.title || 'DoorFlex', {
      body: data.body || '',
      icon: './icons/icon-192.png',
      badge: './icons/icon-192.png',
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: 'window' }).then((clients) => {
      for (const client of clients) {
        if ('focus' in client) return client.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow('./');
    })
  );
});
