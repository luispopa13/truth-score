// TruthScore Service Worker — offline fallback + PWA shell caching
const CACHE = 'ts-v4';
const OFFLINE_URL = '/';

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll([OFFLINE_URL])).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  // Only handle same-origin GET requests
  if (e.request.method !== 'GET' || !e.request.url.startsWith(self.location.origin)) return;
  // Network-first for API calls
  if (e.request.url.includes('/verify') || e.request.url.includes('/analyze')) return;
  e.respondWith(
    fetch(e.request).catch(() => caches.match(OFFLINE_URL))
  );
});

self.addEventListener('push', function(e) {
  let data = {title: 'TruthScore', body: 'New fact-check update', url: '/'};
  try { data = e.data ? JSON.parse(e.data.text()) : data; } catch(_) {}
  e.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: '/static/icon-192.png',
      badge: '/static/icon-192.png',
      data: { url: data.url },
      actions: [{ action: 'open', title: 'Open' }]
    })
  );
});
self.addEventListener('notificationclick', function(e) {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(clients.openWindow(url));
});
