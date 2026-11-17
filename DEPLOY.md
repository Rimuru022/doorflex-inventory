# Deploy en Coolify

Flujo recomendado: **desarrollar en local → push a GitHub → Coolify despliega automáticamente**.

## Requisitos

- Repo en GitHub (ya existe `doorflexInventorySystem`)
- Coolify corriendo en tu servidor
- Dominio `otomatelabs.com` en Cloudflare

## 1. Configurar Coolify (una vez)

1. Coolify → **New Resource → GitHub App** → conectar el repo
2. Seleccionar el repo, base branch `main`, y **Build Pack: Dockerfile** (ya existe en el repo)
3. **Deploy type**: `docker compose` **o** usar el Dockerfile directamente (Coolify detecta ambos)
4. **Persistent Storage**: añadir un **Docker volume** montado en `/app/data` (¡crítico! ahí vive la DB SQLite)
5. **Environment Variables** (en Coolify):

```env
ADMIN_PASSWORD=tu-password-fuerte
SECRET_KEY=tu-secret-random
DATABASE_PATH=data/inventory.db
WEBHOOK_SECRET=tu-webhook-secret-random
RESEND_API_KEY=tu-resend-key
NICK_EMAIL=nick@doorflexaz.com
# Remitente del reporte mensual. DEBE ser un dominio verificado en Resend y autorizado
# por tu API key (si es key restringida, usa el dominio al que está acotada).
# Ej: REPORT_FROM="DoorFlex Inventory <reports@mudominio.com>"
REPORT_FROM=DoorFlex Inventory <reports@otomatelabs.com>
# Opcionales:
# REPORT_RECIPIENTS=tu@email.com,otro@email.com
# BACKUP_RETENTION=13
# BACKUP_PATH=/app/data/backups
# Owner panel: secret path + required password. Si no los pones, el panel está desactivado (404).
# ADMIN_ACCESS_PASSWORD=tu-contrasena-admin-unica
# ADMIN_PATH=/owner-<random-hash>   # genera uno:  openssl rand -hex 6
# QuickBooks Online (DoorFlex invoice → automatic stock deduction). Los 3
# valores vienen del Intuit Developer app (developer.intuit.com). Si falta
# cualquiera, el webhook devuelve 501 y el owner panel muestra "not configured".
# Ver "## QuickBooks Online" más abajo para el setup completo.
# QBO_CLIENT_ID=<intuit client id>
# QBO_CLIENT_SECRET=<intuit client secret>
# QBO_WEBHOOK_VERIFIER_TOKEN=<generado en la pestaña Webhooks del developer app>
# QBO_ENVIRONMENT=sandbox      # cambia a "production" cuando conectes el QBO real
# QBO_REDIRECT_URI se autogenera: {PUBLIC_URL}{ADMIN_PATH}/qbo/callback
```

> `WEBHOOK_SECRET` debe ser **exactamente el mismo** que pongas en el Worker de Cloudflare.

### Persistencia de datos (¡crítico!)

La base de datos SQLite vive en `data/inventory.db`. En un redeploy sin volumen, el filesystem del
contenedor se recrea y **todos los datos se pierden** (quedando solo los valores por defecto).

1. Coolify → resource → **Persistent Storage** → añadir un **Docker volume** con:
   - **Name**: `doorflexdata` (solo alfanuméricos, guiones, puntos o guiones bajos)
   - **Destination path**: `/app/data` ← la ruta DENTRO del contenedor donde vive la DB.
     No uses `/var/lib/coolify/volumes/...` ahí: esa es la ruta del host y Coolify la crea solo.
2. Verificar que `DATABASE_PATH=data/inventory.db` (resuelve a `/app/data/inventory.db`).
3. Tras montar el volumen, hacer un redeploy para que se cree y monte el volumen.

> **Restart vs Redeploy:** un *restart* conserva los datos (mismo filesystem) pero NO aplica código
> nuevo; un *redeploy* sí aplica código nuevo pero borra los datos **si el volumen no está montado**.

### Backups automáticos

Los backups son **completamente automáticos** y viven en `data/backups/` — **dentro del mismo
volumen**, así que persisten en redeploys. El personal del cliente no tiene acceso a la página
de Backups (es admin-only); tú los gestionas a través del servidor u owner panel.

Cada backup es un **snapshot completo**: la base de datos *y* los archivos en disco que la DB
solo referencia por ruta (fotos de recibo de job captures, imágenes de reportes Winston) —
un backup de solo la DB perdería esas fotos silenciosamente, así que se empaquetan juntos en
un único `.zip`.

**Cuándo se crean:**
- En cada arranque del servidor (cubre cada `git push` / redeploy, ya que Coolify crea un
  contenedor nuevo).
- Diariamente a las **00:00 hora Arizona** (cron en background, corre mientras el server está vivo).
- Bajo demanda desde el panel Owner (botón "Backup Now") o borrando uno existente ("Delete").

**Retención:** se conservan los **últimos 13** (ajustable con `BACKUP_RETENTION`; la ruta con
`BACKUP_PATH`). El conteo es sobre el total de backups (incluye cualquier backup viejo formato
`.db` que haya quedado de antes de este cambio).

**Cómo acceder a ellos (3 opciones):**

1. **Servidor (recomendado para ti):** accede al volumen directamente:
   ```bash
   # Desde el host de Coolify, o vía docker exec
   ls /var/lib/docker/volumes/doorflexdata/_data/backups/
   # o dentro del contenedor:
   docker exec -it <container> ls /app/data/backups/
   ```
   Cada archivo es `doorflex-backup-YYYYMMDD-HHMMSS.zip` (contiene `inventory.db` +
   `job_captures/` + `winston_reports/`). Backups viejos con nombre
   `inventory.backup-YYYYMMDD-HHMMSS.db` siguen siendo válidos (solo-DB, sin fotos).

2. **Panel Owner** (`ADMIN_PATH`): página con Download/Delete de cada backup y botón Restore
   (valida el zip o el header SQLite + `PRAGMA quick_check` antes de reemplazar). Ver "Panel
   Owner" abajo.

3. **API directa** (si prefieres scriptear): el `.zip` es un zip estándar (ábrelo con
   cualquier herramienta); adentro, `inventory.db` es SQLite estándar.

**Cómo recuperar datos desde un backup:**

- *Opción rápida (owner):* entra al panel Owner → **Backups** → "Restore from Backup", sube el
  `.zip` (restaura DB + fotos) o un `.db` viejo (restaura solo la DB). El sistema hace backup
  del estado actual antes de reemplazar.
- *Opción directa (servidor):* detén el container (`docker stop`), reemplaza `data/inventory.db`
  y las carpetas `data/job_captures/` / `data/winston_reports/` dentro del volumen con el
  contenido extraído del `.zip`, reinicia.
- *Importar a otra instancia:* descarga el `.zip` (vía el panel Owner o `docker cp`), y súbelo
  por el panel Owner de la instancia destino, o extráelo manualmente sobre `data/` antes del
  primer arranque (`ensure_db` restaura automáticamente el backup más reciente si no encuentra
  una DB existente).

No hay "merge parcial" desde un backup — restaurar reemplaza la DB completa (log, stock,
técnicos, todo) y, si el backup lo incluye, también reemplaza las carpetas de fotos completas
(nunca mezcla archivo por archivo).

### Monthly Report (PDF mensual)

Los reportes los controlas tú (no aparecen en el dashboard del personal). Se genera un PDF
profesional de **una página** con los datos vitales del mes anterior (key metrics, stock
health, unresolved queue, top movers, top low stock) — sin fluff.

**Cómo se envía:**
- **Automático:** el 1ro de cada mes a las 00:05 hora Arizona, a los recipients en
  `REPORT_RECIPIENTS` (comma-separated). Solo se activa si esa variable y `RESEND_API_KEY`
  están configuradas.
- **Manual:** en el **panel Owner → Reports**. Ahí ves un preview de los datos y un form:
  ingresa emails (o deja vacío para usar `REPORT_RECIPIENTS`) y dale al botón
  "Generate PDF & Send".

**Configurar recipients:**
```env
REPORT_RECIPIENTS=tu@email.com,gerente@email.com
```

### Reordering automático + alertas semanales (viernes 08:00 AZ)

Un solo cron (viernes 08:00 hora Arizona) revisa el **stock de warehouse**
(`warehouse_stock.quantity <= min_threshold`) y, en la misma pasada:

1. **C.1 — Reorder por distribuidor:** agrupa los productos bajos por su
   distribuidor activo (los 5 toggles en el catálogo de Parts) y envía un
   correo a cada distribuidor con SKU, nombre, cantidad (`min*2 − current`) y
   warehouse destino.
2. **C.2+C.4 — Resumen interno:** un solo correo a `ALERT_RECIPIENTS` con las
   órdenes enviadas + los productos a comprar en Amazon (links manuales).
3. **A — Alerta semanal:** correo a `ALERT_RECIPIENTS` con la tabla completa de
   stock bajo, agrupado por distribuidor, incluida la sección "Sin distribuidor".

Reglas clave:
- **Solo se envía si hay stock bajo** — si no hay alertas, no hay correo.
- **Distribuidor sin email = no se envía su reorder** (los productos van al
  resumen semanal). Se configura en Parts → "Distributor Emails".
- **Idempotencia:** un reorder pendiente no se reenvía. Se limpia con el botón
  **Orders → Mark as Ready** o automáticamente cuando el warehouse sube a `>= mínimo`.
- **Envíos espaciados** (`EMAIL_SEND_DELAY`, default 60s) para no disparar
  filtros anti-spam.
- **Retry:** si un envío falla, 2 reintentos (5/30 min) y luego se marca
  `failed` + notifica a `ALERT_RECIPIENTS` con el error.

**Configurar recipients y distribuidores:**
```env
ALERT_RECIPIENTS=ops@example.com,otro@example.com
EMAIL_SEND_DELAY=60
```

Los **emails de los distribuidores** no van en env vars: se editan en el
dashboard (Parts → sección "Distributor Emails"). El mapeo parte × distribuidor
(SKU/nombre de cada distribuidor) se carga por producto (panel inline) o
importando un CSV por distribuidor con headers `Product SKU, Distributor SKU,
Distributor Part Name`.

El campo **Amazon URL** de cada parte se edita en el catálogo (Create Part o
inline) o se importa con la columna `Amazon URL` del CSV de partes.


### PWA del técnico (Winston — deducciones desde el teléfono)

Una PWA offline-first vive en `/tech/pwa/` (servida por FastAPI desde
`tech_pwa/`). El técnico de Winston **no procesa emails**: escanea/descuenta
partes desde la app y las deducciones van al mismo motor de stock.

**Instalación:**
1. Generar el token: `python -m modules.demo` (crea el truck `T-DEMO`, el tech
   `Winston` con `deduction_source=pwa` y 5 partes sintéticas, e imprime el
   link). Para un tech existente, rotar el token desde el dashboard owner.
2. El link de instalación es `/tech/pwa/?t=<token>`. **iPhone/iOS:** abrirlo en
   Safari → **Compartir → Agregar a pantalla de inicio**. El manifest no define
   `start_url` a propósito, para que iOS use la URL actual (que trae el token) al
   crear el acceso directo; el token se guarda en IndexedDB + mirror en
   `localStorage` (iOS puede correr el home-screen app en storage particionado).
   El app es full-screen (meta `apple-mobile-web-app-capable`).

**Cómo funciona:**
- `GET /tech/catalog?token=…` — devuelve SOLO el stock del truck del tech
  (nunca otros trucks ni warehouse agregado). Es la fuente del cache offline.
- `POST /tech/deductions` — batch `{batch_id, items:[{sku, qty, ts}]}`.
  Idempotente: el mismo `batch_id` se procesa una sola vez (los retries de red
  nunca duplican). Descucenta **solo stock del truck**, escribe en el log con
  `source=pwa` y permite stock negativo (el consumo es la realidad; se genera
  una alerta de reconciliación).
- Offline: catálogo cacheado por el Service Worker + cola persistente en
  IndexedDB. Al volver señal, sincroniza los batches.
- **Sync robusto (tech_pwa v2):**
  - El sync procesa **todos** los batches pendientes; el fallo de uno nunca
    bloquea al resto (cada batch se reintenta por separado).
  - Reintentos programados (cada ~20 s) mientras haya items pendientes, además
    de los disparadores `online`, `focus`, `pageshow` y `visibilitychange`.
  - **Background Sync** (Chrome/Android): el Service Worker lee la cola desde
    IndexedDB y empuja los batches aunque la app esté cerrada. En iOS Safari
    (sin Background Sync) el sync corre al abrir/enfocar la app.
  - Indicador "N pending" + botón `↻` de sync manual en la barra de búsqueda.
  - Volver del detalle a la lista ya **no recarga la página** (overlay in-place,
    sin depender de red).
- **Regla de fuentes:** si un email llega con el nombre de un tech que usa PWA
  (`deduction_source=pwa`), NO se deduce — se marca en Unmatched → *PWA Source
  Flags* con la nota "tech subcontratista: usar PWA" (anti doble-deducción).
- Las deducciones PWA se ven en el **Log** (filtro Source = **PWA**, con badge
  azul).

**Regenerar/revocar token:** el dashboard owner permite rotar o limpiar el
token de un tech (el raw nunca se almacena — solo su hash SHA-256).

> El SKU canónico es `parts.id` (la UI lo llama SKU): la PWA referencia ese ID,
> el fuzzy match resuelve hacia él y `product_distributors.distributor_sku` lo
> mapea al SKU del distribuidor. No existe un campo `sku` separado.

### Door Order Capture (botón "+ Door Order" en la PWA)

Captura de venta de puertas — **no es una deducción de stock**, tabla y flujo
separados (`door_orders`). El tech llena un wizard (marca → modelo → tamaño →
etc.) y se manda un email a `NICK_EMAIL` + `MARIA_EMAIL` avisando a la oficina
que debe ordenar la puerta. Catálogo de Coachman/Avante en `door_catalog/*.json`
(extraído de los price books oficiales de Clopay); cualquier otro modelo usa un
formulario genérico de respaldo (checkboxes, sin item number calculado).

**⚠️ Antes de pasar esto a producción real (no solo pruebas):**
1. Poner `MARIA_EMAIL` en el `.env` de Coolify con su correo real.
2. Regresar `NICK_EMAIL` al correo real de Nick (`nick@doorflexaz.com`) — mientras
   se prueba la funcionalidad está apuntando temporalmente a un correo de test
   para no mandar avisos reales todavía.
3. Si consigues los price books de Canyon Ridge o AMAR, agrégalos como
   `door_catalog/<modelo>.json` (mismo formato que `coachman.json`) para que
   dejen de usar el formulario genérico.


### Panel Owner (path secreto + contraseña)

Todo lo que es solo del dueño (DevTools/reset, Backups, Reports) vive bajo **un path secreto**
con **su propia contraseña**, siempre activo en producción — sin redeploys para habilitarlo.
El personal del cliente no sabe que existe: la UI del staff no lo menciona, y sin sesión de
owner todos los paths devuelven 404.

```env
ADMIN_ACCESS_PASSWORD=tu-contrasena-admin-unica
ADMIN_PATH=/owner-<random-hash>
```

- **ADMIN_PATH** es la URL secreta. Hazla in-adi-vinable (ej: `openssl rand -hex 6` → úsalo como
  `/owner-9f3a2b8c...`). Solo tú la conoces. No uses la palabra "admin" ni algo predecible.
- **ADMIN_ACCESS_PASSWORD** es la contraseña del panel. Distinta de `ADMIN_PASSWORD` (la del staff).
- Si falta cualquiera de las dos, el panel queda **desactivado**: todo devuelve 404 y se registra
  un warning al arrancar.

**Uso:** navega a `https://<tu-dominio><ADMIN_PATH>/login`, mete la contraseña de owner y tendrás:
- **Dev Tools** → botón "Reset Database" (tipear `RESET`; hace backup antes de borrar).
- **Backups** → Download / Restore.
- **Reports** → preview + enviar el PDF mensual manualmente.

**Seguridad:** login con bloqueo de **5 intentos por 5 minutos** por IP (tras 5 fallos, te pide
esperar aunque la contraseña sea correcta). No hay rate-limit en el login del staff.

## 2. Exponer el backend públicamente

Coolify → resource → **Domains** → añadir `dooflexinventory.otomatelabs.com` → SSL automático (Let's Encrypt).

En Cloudflare: registrar el DNS `dooflexinventory.otomatelabs.com` apuntando a tu servidor (record A, o usar Cloudflare Tunnel). El Worker llamará a `https://dooflexinventory.otomatelabs.com` (no hay subdominio `api.` — el worker añade `/api/ingest/email`).

## 3. Auto-deploy

- Coolify → resource → **Auto Deploy: ON**
- A partir de ahora: `git push origin main` → Coolify construye y despliega solo

## 4. Conectar el Worker de Cloudflare

En `cloudflare-email-worker/src/index.js`:
- `BACKEND_URL` = `https://dooflexinventory.otomatelabs.com`
- `WEBHOOK_SECRET` = el mismo de arriba
- `BACKUP_EMAIL` = tu correo personal (si el backend está caído, las facturas se reenvían ahí)

## 5. Verificar

1. `curl https://dooflexinventory.otomatelabs.com/health` → `{"status":"ok"}`
2. Enviar email con factura a `doorflexinvoices@otomatelabs.com`
3. Revisar logs del worker en Cloudflare → worker → Logs
4. Confirmar la factura en el dashboard (`/dashboard/log`)

## 6. QuickBooks Online (DoorFlex invoice → automatic stock deduction)

DoorFlex usa Dispatch como su tool de campo, y Dispatch ya sincroniza cada factura a QuickBooks Online (add-on de Dispatch, $42/mes en su cuenta). Este flujo aprovecha eso: **cuando un tech cierra una factura en Dispatch, QBO recibe la sync, dispara un webhook a este servidor, el servidor descarga la factura completa, resuelve los line items contra el catálogo DoorFlex, y descuenta stock del truck del tech asignado.** Cero comportamiento nuevo para el tech, cero email intermedio.

### 6.1 Crear el Intuit Developer app (una vez)

1. Ir a **https://developer.intuit.com** y crear cuenta (o iniciar sesión).
2. **My Apps → Create an app → QuickBooks Online and Payments** → dale un nombre (ej. "DoorFlex Inventory").
3. Bajo **Keys & credentials → Development** copia:
   - `Client ID` → `QBO_CLIENT_ID`
   - `Client Secret` → `QBO_CLIENT_SECRET`
4. Bajo **Redirect URIs**, agrega **exactamente**: `https://<TU_DOMINIO>/<ADMIN_PATH>/qbo/callback`
   (mismo esquema/host/path que tu `PUBLIC_URL` + `ADMIN_PATH`; case-sensitive).
5. Bajo **Webhooks** genera un `Verifier Token` → `QBO_WEBHOOK_VERIFIER_TOKEN`.
6. Bajo **Webhooks → Endpoint URL**, registra: `https://<TU_DOMINIO>/webhooks/qbo/invoice` y suscribir a la entidad **Invoice** con las operaciones **Create** y **Update** como mínimo (Delete es opcional).

### 6.2 Configurar entorno

- **Testing controlado (sandbox)**: `QBO_ENVIRONMENT=sandbox`. Intuit provee una empresa QBO ficticia gratis — la puedes ver bajo "Sandbox" en el developer portal. Todo funciona igual que producción; sirve para validar el pipeline end-to-end antes de tocar la cuenta real de Nick.
- **Producción**: `QBO_ENVIRONMENT=production`. Requiere que la app pase el proceso de review de Intuit para operar contra cuentas QBO reales (formulario básico, generalmente <24h para apps privadas de un solo cliente).

### 6.3 Conectar la cuenta

1. Owner panel → **QuickBooks Online** → **Connect QuickBooks Online**.
2. Intuit muestra su pantalla de consentimiento — aprobar acceso read-only a Invoice/Customer.
3. Regresa a nuestra app con status "Connected" + realm id + tiempo de expiración del access token.
4. Los tokens (access + refresh) quedan guardados en `qbo_tokens` (tabla nueva, dentro del backup completo estándar). El access token se refresca automáticamente cada vez que expira; el refresh token rota cada ~100 días y se persiste solo también.

### 6.4 Verificar el flujo

1. En la QBO sandbox (o producción), crear una factura con 1-3 line items que tengan nombres que coincidan (o parecidos) a partes del catálogo DoorFlex.
2. Dentro de segundos, el webhook llega — check `Owner panel → QuickBooks Online → Last webhook received`.
3. Stock del truck del tech asignado debe descontar; log row en `/dashboard/log` con `invoice_id=qbo:<intuit_invoice_id>`; ítems no reconocidos aparecen en `/dashboard/unmatched` para review manual.

### 6.5 Desconectar

Owner panel → **QuickBooks Online → Disconnect**. Borra los tokens; el webhook seguirá aceptando POST pero ninguna factura se procesará hasta reconectar.

## Troubleshooting

| Problema | Solución |
|---|---|
| "database is locked" | Normal con SQLite + concurrencia baja; ya hay `busy_timeout=10000`. Sube el volumen de Coolify si persiste |
| Datos perdidos al redeploy | El volumen en `/app/data` no está montado — verificar Persistent Storage |
| Worker responde 401 | `WEBHOOK_SECRET` distinto entre backend y worker |
| Worker responde 1010/403 | Bot Fight Mode de Cloudflare bloquea el fetch del worker — desactivarlo o añadir regla WAF skip para `/api/` |
| Worker no recibe emails | Verificar Email Routing → `doorflexinvoices@otomatelabs.com` → apunta al worker |
| Import Excel: "Warehouse 'X' not found" | El Excel tiene un nombre/ID que no existe. El error lista los válidos. Usa el ID (`wh-warranty`) o el nombre exacto (`DoorFlex`). |
| Import Excel: "Incorrect number of bindings" | Bug antiguo ya corregido — re-importa el archivo. |
| Reporte mensual no se envía | Falta `REPORT_RECIPIENTS` o `RESEND_API_KEY` (placeholder). Revisa los logs del server. |
| Reporte da "403 Forbidden" de Resend | El dominio del `from` no está autorizado por tu API key. Resend restringe keys a un dominio; ajusta `REPORT_FROM` al dominio verificado (p.ej. `otomatelabs.com`). El mensaje de error de Resend dice qué dominio espera. |
| Panel Owner no carga (404) | Faltan `ADMIN_ACCESS_PASSWORD` y/o `ADMIN_PATH` (o el path es distinto). Revisa el log: "owner panel DISABLED". |
| "Too many attempts" en el panel Owner | 5 fallos en 5 min por IP. Espera 5 min o reinicia el server. |

## Demo público (DEMO_MODE)

Un segundo recurso en Coolify, del mismo repo, con estas variables:

```env
DEMO_MODE=1
SECRET_KEY=<random>
DATABASE_PATH=data/inventory.db
PUBLIC_URL=https://<dominio-del-demo>
# Opcional, para que la lectura de fotos con IA funcione en el demo:
# OPENAI_API_KEY=<key de OpenRouter>
# OPENAI_BASE_URL=https://openrouter.ai/api/v1
# DEMO_VISION_DAILY_LIMIT=40
```

- No necesita volumen persistente: la base se regenera al arrancar y cada noche a las `DEMO_RESET_HOUR` (03:00 por defecto).
- Nada sale del servidor: los correos quedan registrados como envíos simulados; push, QuickBooks, el webhook de correo y el panel owner están apagados.
- Nunca reutilizar el `DATABASE_PATH` o el volumen del deploy del cliente: el modo demo reemplaza la base completa.
