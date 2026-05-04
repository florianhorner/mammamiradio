// Service Worker for Mamma Mi Radio PWA
// Bump CACHE_NAME on any visual/asset change. Old cache is purged on activate.
const CACHE_NAME = 'radio-itali-v4';
const PRECACHE_URLS = [
  '/listen',
  '/static/manifest.json',
  '/static/icon-192.svg',
  '/static/icon-512.svg',
];

// Install: precache shell assets
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE_URLS))
  );
  self.skipWaiting();
});

// Activate: clean up old caches
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Fetch: network-first for API/stream, cache-first for static assets
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Never cache the audio stream or API calls
  // Use endsWith/includes to handle HA Ingress prefixed paths
  if (url.pathname.endsWith('/stream') || url.pathname.includes('/api/') ||
      url.pathname.endsWith('/status') || url.pathname.endsWith('/public-status')) {
    return;
  }

  // Cache-first for static assets and app shell
  event.respondWith(
    caches.match(event.request).then((cached) => {
      const fetchPromise = fetch(event.request).then((response) => {
        if (response && response.status === 200 && response.type === 'basic') {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        }
        return response;
      }).catch(() => cached);

      return cached || fetchPromise;
    })
  );
});
