/**
 * Cloudflare Worker — RFC 8058 One-Click Unsubscribe Handler
 *
 * Handles:
 *   POST /u/<token>  → One-Click unsubscribe (Gmail/Yahoo auto-POST)
 *   GET  /u/<token>  → Landing page confirmation
 *
 * Stores unsubscribes in KV namespace (binding: UNSUB_KV)
 *
 * Deploy via Cloudflare API or wrangler:
 *   wrangler publish
 */

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;

    // Health check
    if (path === "/health") {
      return new Response(JSON.stringify({ status: "ok" }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    // Unsubscribe endpoint: /u/<token>
    const match = path.match(/^\/u\/(.+)$/);
    if (!match) {
      return new Response("Not Found", { status: 404 });
    }

    const token = match[1];

    if (request.method === "POST") {
      // RFC 8058 One-Click: Gmail/Yahoo send a POST automatically
      await env.UNSUB_KV.put(`unsub:${token}`, JSON.stringify({
        token: token,
        method: "one-click",
        timestamp: new Date().toISOString(),
        ip: request.headers.get("CF-Connecting-IP") || "",
      }));

      return new Response("Unsubscribed", {
        status: 200,
        headers: { "Content-Type": "text/plain" },
      });
    }

    if (request.method === "GET") {
      // Landing page for manual unsubscribe
      const existing = await env.UNSUB_KV.get(`unsub:${token}`);

      if (existing) {
        return new Response(ALREADY_PAGE, {
          headers: { "Content-Type": "text/html; charset=utf-8" },
        });
      }

      // Show confirmation page
      return new Response(CONFIRM_PAGE.replace("{{TOKEN}}", token), {
        headers: { "Content-Type": "text/html; charset=utf-8" },
      });
    }

    return new Response("Method Not Allowed", { status: 405 });
  },
};

const CONFIRM_PAGE = `<!DOCTYPE html>
<html lang="de">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Abmeldung</title>
<style>body{font-family:Arial,sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;background:#f5f5f5}
.card{background:#fff;padding:40px;border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,.1);text-align:center;max-width:400px}
h1{font-size:20px;margin-bottom:15px}p{color:#666;margin-bottom:25px}
button{background:#dc3545;color:#fff;border:none;padding:12px 30px;border-radius:5px;font-size:16px;cursor:pointer}
button:hover{background:#c82333}</style></head>
<body><div class="card">
<h1>Newsletter abmelden</h1>
<p>Möchten Sie sich wirklich abmelden?</p>
<form method="POST" action="/u/{{TOKEN}}">
<button type="submit">Ja, abmelden</button>
</form></div></body></html>`;

const ALREADY_PAGE = `<!DOCTYPE html>
<html lang="de">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Abmeldung</title>
<style>body{font-family:Arial,sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;background:#f5f5f5}
.card{background:#fff;padding:40px;border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,.1);text-align:center;max-width:400px}
h1{font-size:20px;color:#28a745}</style></head>
<body><div class="card">
<h1>Sie wurden bereits abgemeldet.</h1>
</div></body></html>`;
