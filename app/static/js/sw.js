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
// Das Aendern dieser SW-Logik selbst ist im Token bereits drin (js/sw.js ist
// Teil des Hashes), also keinen v2/v3-Bump noetig.
const CACHE = "wg-static-v1";
const OFFLINE_URL = "/offline";
// Wie lange (ms) maximal aufs Netz warten, bevor wir bei einer Navigation
// auf Cache/Offline-Fallback ausweichen. Auf langsamen Mobile-Verbindungen
// blockt der network-first-Wait sonst den Browser ewig.
const NAV_TIMEOUT_MS = 2000;

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

  // Seitenaufrufe: network-first MIT Timeout, damit der Browser auf langsamen
  // Mobile-Verbindungen nicht ewig wartet. Wenn das Netz nicht innerhalb
  // NAV_TIMEOUT_MS antwortet, fallen wir auf die Offline-Seite zurueck.
  // Authentifizierte HTML-Seiten werden bewusst NICHT gecacht (pro-User-
  // Inhalt) — daher kein stale-while-revalidate hier.
  if (req.mode === "navigate") {
    event.respondWith(
      (async () => {
        const networkP = fetch(req).catch(() => null);
        const timeoutP = new Promise((res) =>
          setTimeout(() => res(null), NAV_TIMEOUT_MS)
        );
        const resp = await Promise.race([networkP, timeoutP]);
        if (resp) return resp;
        // Timeout oder Netz-Fehler -> Offline-Seite. Wenn auch die nicht im
        // Cache ist (Erst-Besuch ohne Install), notgedrungen Response.error.
        const offline = await caches.match(OFFLINE_URL, { ignoreSearch: true });
        return offline || Response.error();
      })()
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
