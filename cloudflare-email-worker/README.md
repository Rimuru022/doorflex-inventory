# DoorFlex Email Ingest Worker

Recibe emails en `doorflexinvoices@otomatelabs.com` (Email Routing de Cloudflare) y los reenvía como MIME crudo al backend para procesar facturas.

## Deploy (dashboard — ya hecho)

1. Cloudflare Dashboard → Workers & Pages → `doorflex-invoice-ingest` (ya creado)
2. Pega el contenido de `src/index.js` en "Edit Code" → Save & Deploy
3. Settings → Variables and Secrets:
   - `BACKEND_URL` = URL pública del backend en Coolify = `https://dooflexinventory.otomatelabs.com` (el worker añade `/api/ingest/email`)
   - `WEBHOOK_SECRET` = el mismo valor que `WEBHOOK_SECRET` en el backend
   - `BACKUP_EMAIL` = (opcional) correo de respaldo si el backend está caído
4. Email Routing → Routing rules → confirmar que `doorflexinvoices@otomatelabs.com` apunta al worker

## Deploy (CLI, alternativa)

```bash
cd cloudflare-email-worker
npm i wrangler --save-dev
npx wrangler deploy
```

## Probar

1. Enviar un email con una factura PDF a `doorflexinvoices@otomatelabs.com`
2. Ver logs del worker en el dashboard (Workers → doorflex-invoice-ingest → Logs)
3. Confirmar que la factura aparece en el dashboard del sistema (Log / Stock)

## Nota

El worker NO parsea el email (no necesita postal-mime). Envía el MIME crudo completo al backend, que ya tiene toda la lógica de extracción, matching y deducción.
