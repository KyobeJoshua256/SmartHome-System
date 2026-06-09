// /static/sw.js
const CACHE_NAME = 'electro-nora-v1';
const urlsToCache = [
  '/',
  '/static/css/userdashboard.css',
  '/static/js/userdashboard.js',
  '/static/js/UserMessaging.js',
  '/static/js/UserDevices.js'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(urlsToCache))
  );
});

self.addEventListener('fetch', event => {
  event.respondWith(
    caches.match(event.request)
      .then(response => response || fetch(event.request))
  );
});