// Service Worker for Mamma Mi Radio PWA
// Bump CACHE_NAME on any visual/asset change. Old cache is purged on activate.
const CACHE_NAME = 'radio-itali-v6';
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

function fetchAndCache(request) {
  return fetch(request).then((response) => {
    if (response && response.status === 200 && response.type === 'basic') {
      const clone = response.clone();
      caches.open(CACHE_NAME).then((cache) => cache.put(request, clone));
    }
    return response;
  });
}

// Fetch: network-first for app shell/CSS/JS, cache-first only for stable install assets.
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  const path = url.pathname;

  // Never cache the audio stream or API calls
  // Use endsWith/includes to handle HA Ingress prefixed paths
  if (path.endsWith('/stream') || path.includes('/api/') ||
      path.endsWith('/status') || path.endsWith('/public-status')) {
    return;
  }

  const isFreshAsset =
    path.endsWith('/listen') ||
    path.endsWith('.css') ||
    path.endsWith('.js') ||
    path.endsWith('/sw.js');
  if (isFreshAsset) {
    event.respondWith(fetchAndCache(event.request).catch(() => caches.match(event.request)));
    return;
  }

  const isStableInstallAsset =
    path.endsWith('/static/manifest.json') ||
    path.endsWith('/static/icon-192.svg') ||
    path.endsWith('/static/icon-512.svg');
  if (isStableInstallAsset) {
    event.respondWith(caches.match(event.request).then((cached) => cached || fetchAndCache(event.request)));
    return;
  }

  // Catch-all for any other same-origin GET (logo, fonts, future static images):
  // network-with-cache-fallback so offline visits still render brand assets.
  if (event.request.method === 'GET' && url.origin === self.location.origin) {
    event.respondWith(
      fetchAndCache(event.request).catch(() => caches.match(event.request))
    );
  }
});
