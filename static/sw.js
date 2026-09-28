/*
 * Brydl service worker — offline resilience for annotators.
 *
 * Caching policy (deliberately conservative):
 * - POST requests are NEVER intercepted (CSRF + submissions must hit
 *   the server; a failed POST surfaces as a failed navigation and the
 *   page's draft autosave already kept the annotator's typing).
 * - Page navigations are network-first and HTML is NEVER cached: a
 *   stale authenticated page could mislead the person using it (or
 *   leak data on a shared device after logout).
 * - The pinned jsDelivr CDN assets are cache-first: their URLs are
 *   SRI-pinned and immutable, so "cached forever" is safe, and pages
 *   stay fully styled on a dead connection.
 * - Same-origin /static/ assets: stale-while-revalidate.
 * - Everything else: untouched (default network behaviour).
 *
 * Update procedure: bump CACHE_VERSION — the activate handler deletes
 * every older cache. Kill switch: stop registering the worker (see
 * static/js/main.js) or serve 404 for /sw.js.
 */
const CACHE_VERSION = "brydl-static-v2";
const OFFLINE_URL = "/offline";
const PRECACHE_URLS = [
  OFFLINE_URL,
  "/static/brand/logo-192.png",
  "/static/brand/logo-64.png",
  "/static/vendor/bootstrap.min.css",
  "/static/vendor/bootstrap-icons.min.css",
  "/static/vendor/bootstrap.bundle.min.js",
  "/static/vendor/fonts/bootstrap-icons.woff2",
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE_VERSION)
      .then(function (cache) { return cache.addAll(PRECACHE_URLS); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys()
      .then(function (keys) {
        return Promise.all(keys
          .filter(function (key) { return key !== CACHE_VERSION; })
          .map(function (key) { return caches.delete(key); }));
      })
      .then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  const request = event.request;
  if (request.method !== "GET") {
    return; // submissions, logouts, admin mutations: never intercepted
  }
  const url = new URL(request.url);
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    return;
  }

  // Page loads: network-first, branded fallback when offline.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(function () {
        return caches.match(OFFLINE_URL);
      })
    );
    return;
  }

  // Immutable self-hosted vendor assets: cache-first.
  if (url.origin === self.location.origin &&
      url.pathname.indexOf("/static/vendor/") === 0) {
    event.respondWith(
      caches.match(request).then(function (cached) {
        if (cached) {
          return cached;
        }
        return fetch(request).then(function (response) {
          const copy = response.clone();
          caches.open(CACHE_VERSION).then(function (cache) {
            cache.put(request, copy);
          });
          return response;
        });
      })
    );
    return;
  }

  // Same-origin static files: stale-while-revalidate.
  if (url.origin === self.location.origin &&
      url.pathname.indexOf("/static/") === 0) {
    event.respondWith(
      caches.match(request).then(function (cached) {
        const network = fetch(request)
          .then(function (response) {
            const copy = response.clone();
            caches.open(CACHE_VERSION).then(function (cache) {
              cache.put(request, copy);
            });
            return response;
          })
          .catch(function () { return cached; });
        return cached || network;
      })
    );
    return;
  }

  // Everything else: default browser behaviour, no caching.
});
