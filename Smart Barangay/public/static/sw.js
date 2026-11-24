const CACHE_NAME = 'smartbarangay-shell-v2';
const APP_SHELL_ASSETS = [
    '/',
    '/login',
    '/static/style.css',
    '/static/live-clock.js',
    '/static/offline-sync.js'
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return Promise.allSettled(APP_SHELL_ASSETS.map((url) => cache.add(url)));
        }).then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((keys) => Promise.all(
            keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
        )).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    const req = event.request;
    if (req.method !== 'GET') return;

    const url = new URL(req.url);
    // Let API requests pass directly to network/offline-sync.js
    if (url.pathname.startsWith('/api/')) return;

    // Network-first with cache fallback for HTML pages, local static files, and icon/font CDNs
    const isCacheable = (
        url.origin === self.location.origin
        || url.hostname.includes('unpkg.com')
        || url.hostname.includes('fonts.googleapis.com')
        || url.hostname.includes('fonts.gstatic.com')
    );
    if (!isCacheable) return;

    event.respondWith(
        fetch(req)
            .then((networkRes) => {
                if (networkRes && (networkRes.status === 200 || networkRes.type === 'opaque')) {
                    const copy = networkRes.clone();
                    caches.open(CACHE_NAME).then((cache) => cache.put(req, copy)).catch(() => {});
                }
                return networkRes;
            })
            .catch(() => caches.match(req).then((cached) => {
                if (cached) return cached;
                if (req.mode === 'navigate') return caches.match('/');
                return new Response('', { status: 503, statusText: 'Offline' });
            }))
    );
});
