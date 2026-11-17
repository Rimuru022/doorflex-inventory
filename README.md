# DoorFlex Inventory

Inventario por camión, captura de trabajos en campo y conciliación de pagos para una empresa de reparación de puertas de garaje en Arizona.
Construido con [Claude Code](https://claude.com/claude-code) para un cliente real.

**Demo en vivo:** `https://<dominio-del-demo>` (datos ficticios, se reinicia cada noche, sin registro).

## El problema

Una aseguradora asigna los trabajos por Dispatch.me, el técnico cobra con la terminal de la aseguradora y cada semana llega una foto de una hoja de cálculo con lo que se va a pagar.
DoorFlex no tiene API de ninguno de esos sistemas.
Si un técnico olvida las notas de cierre, la aseguradora borra el trabajo y no lo paga: en una sola semana faltaron 6 trabajos, sin evidencia para reclamarlos.
La conciliación la hacía una persona a tiempo completo, y nadie sabía qué pieza había en qué camión.

## Qué hace

| Flujo | Cómo |
|---|---|
| Inventario | Las facturas PDF llegan por correo (Cloudflare Email Worker), un parser determinista extrae técnico, piezas y cantidades, y el stock se descuenta del camión correcto. Lo que no se reconoce con seguridad va a una cola de revisión humana. |
| Reabastecimiento | Alertas de stock bajo y pedido semanal a cada distribuidor por correo. |
| Evidencia en campo | PWA offline para el técnico: ruta del día, cierre del trabajo (cobro, notas, foto del recibo) y piezas usadas. |
| Conciliación | Cada tarde cruza trabajos esperados contra capturados. Si la aseguradora borra un trabajo, se abre una disputa con la evidencia adjunta. |
| Pago semanal | La foto del reporte se lee con visión de IA, se valida (números de orden raros, totales que no cuadran) y un humano confirma antes de que cuente. |

La IA se usa solo donde no hay otra salida, una foto, y nunca decide sola sobre dinero.

## Stack

FastAPI, SQLite (WAL), Jinja2, PWA con service worker e IndexedDB, visión vía API compatible con OpenAI (OpenRouter), Resend para correo, Cloudflare Email Workers, QuickBooks Online (webhooks + OAuth), Docker en Coolify.

## Correr local

```bash
uv sync
cp .env.example .env            # edita lo necesario
uv run uvicorn main:app --reload --port 8001
uv run pytest                   # suite completa
```

Modo demo (datos sintéticos, nada sale del servidor):

```bash
DEMO_MODE=1 SECRET_KEY=dev uv run uvicorn main:app --port 8001
# abre http://localhost:8001
```

`modules/demo_seed.py` genera 6 meses de historia pasando datos inventados por los mismos pipelines de la app (parser, matcher, PWA, conciliación, reportes), así que el demo ejercita la lógica real.

## Cómo se construyó

- `AGENT_CONTEXT.md`: el documento de contexto que escribí para el agente después de entrevistar al cliente (personas, modelo de negocio, reglas de dinero, preguntas abiertas).
- `docs/superpowers/specs/`: specs de diseño de funciones antes de implementarlas.
- `tests/`: más de 200 pruebas; los bugs se reproducen primero como prueba (por ejemplo `tests/test_name_matcher.py`).
- `DEPLOY.md`: despliegue en Coolify, persistencia, backups y QuickBooks.
