/**
 * DoorFlex Inventory — Email Ingest Worker
 *
 * Recibe emails enviados a doorflexinvoices@otomatelabs.com via Cloudflare
 * Email Routing, y reenvía el MIME crudo al backend (Coolify) para procesar
 * las facturas adjuntas. No parsea nada — el backend ya sabe hacerlo.
 *
 * Configuración en el dashboard de Cloudflare (Worker > Settings > Variables):
 *   BACKEND_URL    = https://dooflexinventory.otomatelabs.com
 *   WEBHOOK_SECRET = (mismo valor que WEBHOOK_SECRET en el backend)
 *   BACKUP_EMAIL   = (opcional) si el backend está caído, el email se reenvía aquí
 */

export default {
  async email(message, env, ctx) {
    const start = Date.now();
    console.log(`[ingest] from=${message.from} to=${message.to} size=${message.rawSize}`);

    // Buffer the raw MIME (message.raw is single-use)
    const raw = await new Response(message.raw).arrayBuffer();

    try {
      const resp = await fetch(`${env.BACKEND_URL}/api/ingest/email`, {
        method: "POST",
        headers: {
          "Content-Type": "application/octet-stream",
          "X-Webhook-Secret": env.WEBHOOK_SECRET,
        },
        body: raw,
      });

      const text = await resp.text();
      console.log(`[ingest] backend responded ${resp.status} in ${Date.now() - start}ms: ${text.slice(0, 200)}`);

      if (!resp.ok && env.BACKUP_EMAIL) {
        console.log(`[ingest] backend error (${resp.status}), forwarding backup to ${env.BACKUP_EMAIL}`);
        await message.forward(env.BACKUP_EMAIL);
      }
    } catch (e) {
      console.error(`[ingest] fetch failed: ${e.message}`);
      if (env.BACKUP_EMAIL) {
        console.log(`[ingest] forwarding backup to ${env.BACKUP_EMAIL}`);
        await message.forward(env.BACKUP_EMAIL);
      } else {
        console.error("[ingest] no BACKUP_EMAIL set — email is lost (backend was unreachable)");
      }
    }
  },
};
