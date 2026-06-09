/*
 * WG-Organisation — Service Worker.
 *
 * Wird unter /sw.js (Root) ausgeliefert -> Scope = ganze App ("/").
 *
 * Strategie (bewusst defensiv, damit nichts Dynamisches kaputtgeht):
 *   - Nur GET + same-origin werden angefasst. POST/PUT/DELETE (Abhaken,
 *     Toggle, alle HTMX-Mutationen) gehen IMMER ungefiltert ans Netz.
 *   - /auth/* wird komplett durchgereicht -> OAuth/Login nie cachen oder
 *     offline abfangen.
 *   - Navigationen (Seitenaufrufe): network-first. Offline -> /offline-Seite.
 *     Authentifizierte HTML-Seiten werden NICHT gecacht (pro-User-Inhalt,
 *     sonst Daten-Leak zwischen Nutzern).
 *   - Statische Assets unter /static/: stale-while-revalidate -> sofort aus
 *     dem Cache, Update läuft im Hintergrund (output.css ist nicht gehasht).
 */

// "wg-static-v1" ist nur ein Platzhalter: die /sw.js-Route (app/blueprints/pwa)
// ersetzt ihn beim Ausliefern durch einen aus dem Asset-Inhalt abgeleiteten
// Token, damit ein Deploy mit geänderter output.css den Cache sauber erneuert.
const CACHE = "wg-static-v1";
const OFFLINE_URL = "/offline";

// Beim Install vorgeladen, damit die App-Hülle auch offline steht.
const PRECACHE = [
  OFFLINE_URL,
  "/static/css/output.css",
  "/static/js/htmx.min.js",
  "/static/img/icons/icon-192.png",
  "/static/img/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
      )
      .then(() => self.clients.claim())
  );
});

// Erlaubt der Seite, ein wartendes Update sofort zu aktivieren.
self.addEventListener("message", (event) => {
  if (event.data === "SKIP_WAITING") self.skipWaiting();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;

  // Mutationen nie abfangen.
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // Fremde Origins (Google Fonts, OAuth-Redirect-Ziel ...) durchreichen.
  if (url.origin !== self.location.origin) return;

  // Auth-Flow komplett in Ruhe lassen.
  if (url.pathname.startsWith("/auth/")) return;

  // Seitenaufrufe: network-first mit Offline-Fallback.
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).catch(() =>
        caches
          .match(OFFLINE_URL, { ignoreSearch: true })
          .then((cached) => cached || Response.error())
      )
    );
    return;
  }

  // Statische Assets: stale-while-revalidate.
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.open(CACHE).then(async (cache) => {
        const cached = await cache.match(req);
        const network = fetch(req)
          .then((resp) => {
            if (resp && resp.ok) cache.put(req, resp.clone());
            return resp;
          })
          .catch(() => cached);
        return cached || network;
      })
    );
    return;
  }

  // Alles andere (z.B. /manifest.webmanifest, dynamische GETs): nicht anfassen.
});
