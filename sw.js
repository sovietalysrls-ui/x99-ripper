/* Keep catalog and signed film links fresh; show one clear offline message. */
self.addEventListener('install', event => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', event => {
  if (event.request.mode !== 'navigate') return;
  event.respondWith(fetch(event.request).catch(() => new Response(
    '<!doctype html><html lang="it"><meta name="viewport" content="width=device-width,initial-scale=1"><title>DiscFlow non disponibile</title><body style="font:16px system-ui;background:#f4f6f3;color:#192e3b;padding:24px"><h1>DiscFlow non disponibile</h1><p>Controlla la connessione e riprova.</p></body></html>',
    {headers: {'Content-Type': 'text/html; charset=utf-8'}})));
});
