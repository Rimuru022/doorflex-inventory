# DoorFlex Inventory System — Implementation Plan

> Basado en análisis de Graphify (210 nodos, 465 edges, 22 comunidades) + respuestas del cliente.

---

## Fase 1 — Independiente (no necesita datos de facturas)

### 1.1 Renombrar Warehouses

| Actual ID | Nuevo Display Name |
|---|---|
| `wh-warranty` | DoorFlex |
| `wh-sub` | Total Garage Solutions |

- **Archivos**: `database.py` (seed data), todas las templates que rendericen `w.name`
- **Prueba**: Al resetear DB o en seed, los nombres aparecen como "DoorFlex" y "Total Garage Solutions" en Stock, Alerts, Audit, etc.

### 1.2 Navbar Toggle (Warehouse Selector)

- Dropdown en `dashboard_base.html` con opciones dinámicas: `[DoorFlex | Total Garage Solutions | Combined]`
- Filtra visualmente stock, alerts, top movers, log, unmatched por warehouse
- Ruta base `main.py` inyecta `warehouses` + `selected_warehouse` a todos los templates
- **Archivos**: `dashboard_base.html`, `main.py` (pasar `warehouses` al contexto global)
- **Prueba**: 
  - Seleccionar "DoorFlex" → solo stock/alerts de wh-warranty visibles
  - Seleccionar "Total Garage Solutions" → solo wh-sub
  - "Combined" → ambos (comportamiento actual)
  - Si se agrega warehouse nueva, aparece sola en el dropdown

### 1.3 Remover Streams Page

- Eliminar ruta `GET /dashboard/warehouses`, `POST /dashboard/warehouses/create`, `POST /dashboard/warehouses/update`
- Eliminar link del navbar
- Eliminar template `dashboard_warehouses.html`
- **Archivos**: `main.py`, `dashboard_base.html` (navbar), borrar template
- **Prueba**: `/dashboard/warehouses` devuelve 404. Navbar no muestra "Streams".

### 1.4 Mostrar Warehouse en Nombres de Trucks

- En todas las vistas, un truck se muestra como `"Truck 1 — DoorFlex"` en lugar de solo `"Truck 1"`
- **Archivos**: `main.py` (rutas que listan trucks), `dashboard_trucks.html`, `dashboard_stock.html`, `dashboard_audit.html`
- **Prueba**: En Trucks page, Stock page, Audit page — cada truck muestra su warehouse asignada

### 1.5 Importar Lista de Partes de Nick

- Usar la funcionalidad existente `bulk_import_csv()` / `bulk_import_xlsx()` en `/dashboard/parts`
- Las partes importadas aparecen como "Unassigned Parts" en Stock page
- **Archivos**: Ninguno (todo existe)
- **Prueba**: Subir CSV de Nick → partes aparecen en catálogo + en Unassigned Parts

### 1.6 Restock Rápido por Truck ("Restock All Low")

- Botón "Restock All Low" por truck en Stock page
- Deduce de la warehouse asignada al truck
- Solo restockea partes donde `quantity < min_threshold`
- Nueva función batch en `inventory.py`
- **Archivos**: `inventory.py` (`bulk_restock_truck`), `main.py` (ruta POST), `dashboard_stock.html`
- **Prueba**:
  - Truck con 3 partes bajo mínimo → click "Restock All Low" → las 3 se actualizan
  - Log registra source=`bulk_restock` con cada parte
  - Warehouse stock se descuenta correctamente

### 1.7 Print Restock List por Truck

- Botón "Print Restock List" por truck en `dashboard_trucks.html`
- Nueva ruta `GET /dashboard/audit/truck/{truck_id}` (HTML imprimible)
- Columnas: Part Name, Current Stock, Min Required, Qty Needed (`min - current`)
- **Archivos**: `main.py` (nueva ruta GET), `dashboard_trucks.html`, `restock_list.html` (nueva template)
- **Prueba**:
  - Click en "Print Restock List" → abre página imprimible
  - `Qty Needed` = `max(0, min_threshold - current_stock)`
  - Sin stock → muestra 0, no negativos

### 1.8 Slow Movers Toggle

- En `/dashboard/top-movers`, toggle entre "Top Movers" y "Slow Movers"
- Slow = `ORDER BY total_used ASC` (menos usados primero, últimos 30 días)
- Misma tabla, mismo filtro por warehouse
- **Archivos**: `main.py` (parámetro `order=asc`), `dashboard_top_movers.html`
- **Prueba**:
  - Toggle a "Slow Movers" → tabla ordenada ascendente
  - Partes con 0 usos aparecen primero
  - Filtro por warehouse sigue funcionando

---

## Fase 2 — UI construible ahora (necesita datos de Feature A)

### 2.1 Usage Tracking Page

- Nueva ruta `GET /dashboard/usage`
- Jerarquía:
  1. Selector de Warehouse (DoorFlex / Total Garage Solutions)
  2. Tabla de trucks asignados + total de partes usadas por truck
  3. Click en truck → detalle: Part Name, Quantity Used, Date, con fechas
- Period toggle: Day / Week / Month / Quarter (filtra `timestamp` en queries)
- Muestra **quantities usadas** (del `log`), NO stock on hand
- **Archivos**: `main.py` (nueva ruta), `modules/analytics.py` (nuevas queries: `usage_by_warehouse`, `usage_by_truck`, `usage_by_part`), `dashboard_usage.html` (nueva template)
- **Prueba**:
  - Selector de warehouse cambia trucks listados
  - Click en truck expande/muestra partes con fechas
  - Toggle de período cambia los datos
  - Sin datos → mensaje "No data yet. Process invoices first."

### 2.2 Monthly Report Manual

- Botón "Generate & Email Report" en Home page
- POST a `/dashboard/report/monthly`
- Calcula: Part Name, Quantity Used Last Month, Current Stock, Minimum, Recommended Reorder Qty
- Diferencia por warehouse (DoorFlex + Total Garage Solutions)
- Alerta partes bajo mínimo
- Envía a `NICK_EMAIL` via Resend API
- **Archivos**: `main.py` (nueva ruta POST), `modules/email_reporter.py` (nuevo módulo con `generate_monthly_report` + `send_report`)
- **Prueba**:
  - Click "Generate & Email Report" → 200 OK
  - Email llega a nick@doorflexaz.com con datos correctos
  - Sin datos → email igual se envía con "No usage data available"
  - `RESEND_API_KEY` vacío → error message en UI (no crash)

---

## Fase 3 — Pendiente de OAuth (acceso en ~2 días)

### 3.1 Re-habilitar OAuth Client

1. Ir a https://console.cloud.google.com/apis/credentials
2. Buscar cliente `713825638797-i0f9np9hdio2ctccc9aiir5j1v38qkfe`
3. Click → "Enable"
4. Ejecutar: `uv run python3 get_gmail_token.py`
5. Esperar a que guarde `.gmail-token.json`
6. Reiniciar servidor

### 3.2 Verificar Polling

- **Prueba**:
  - Servidor inicia → log: `[poller] Using Gmail API (token: ...)`
  - Forward email con PDF → procesado en <60 seg
  - Format A y B detectados correctamente
  - Técnico extraído de firma
  - Partes con match ≥85% → deducción automática
  - Match 70-84% → a unmatched_parts
  - Match <70% → a unmatched_parts
  - Invoice duplicada → log: `skipping duplicate_invoice`

---

## Fase 4 — Futuro

### 4.1 Automatic Monthly Email Reports

- Evolucionar el reporte manual a background task
- Usar `apscheduler` o similar
- Ejecutar día 1 de cada mes a las 8:00 AM (America/Phoenix)
- Mismo contenido que el manual
- **Archivos**: `main.py` (lifespan scheduler), `modules/email_reporter.py` (reuse)

### 4.2 Predictive Ordering System

- No empezar hasta tener **3+ meses de datos** en la tabla `log`
- Análisis por parte:
  - Usage promedio mensual
  - Desviación estándar
  - Current stock en warehouse
  - Sugerencia: "Based on last X months, avg usage = Y. You have Z. Recommend ordering A."
- **Archivos**: `modules/analytics.py` (nuevas funciones), `main.py`, `dashboard_predictive.html`
- **Prueba**: con datos mock de 3 meses, las sugerencias son coherentes

### 4.3 Logo Rectangular

- Cuando Nick/Maria provean el asset
- Reemplazar texto "DOORFLEX" en navbar
- Responsive: se ve bien en mobile y desktop
- Fallback: texto si no hay logo
- **Archivos**: `dashboard_base.html`

---

## Dependencias Completas

```
Fase 1 ─── completamente independiente
  │
Fase 2 ─── necesita fase 3.2 funcionando para tener datos reales
  │         (pero UI se construye y prueba sin datos)
  │
Fase 3.1 ── necesita acceso a Google Cloud Console (2 días)
  │
Fase 3.2 ── necesita Gmail API activa + facturas reales
  │
Fase 4.1 ── necesita Fase 2.2 funcionando con datos
  │
Fase 4.2 ── necesita 3+ meses de datos procesados
  │
Fase 4.3 ── necesita asset de Nick/Maria
```

## Testing General

| Tipo | Cómo |
|---|---|
| Unitario | `pytest` en módulos individuales (si existe) |
| Smoke test | Arrancar servidor, visitar cada página |
| Email test | Usar `get_gmail_token.py` + forward de factura real |
| Formato A | Factura con 1 línea + qty=1 + package en DB |
| Formato B | Factura con múltiples líneas itemizadas |
| Sin datos | Páginas no se rompen, muestran mensaje "no data" |
| Warehouse toggle | Cada opción filtra correctamente, combined muestra todos |
