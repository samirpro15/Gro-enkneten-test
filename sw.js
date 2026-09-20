// Terminradar Großenkneten – einfacher Service Worker
// Cached nur die App-Hülle (HTML/CSS/JS/Icons), damit die App installierbar
// ist und offline zumindest ihr Grundgerüst anzeigt. status.json wird
// bewusst NICHT gecacht, damit immer der aktuelle Stand angezeigt wird.

const CACHE_NAME = 'terminradar-shell-v9';
const SHELL_FILES = [
  './',
  './index.html',
  './manifest.json',
  './icons/icon-192.png',
  './icons/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // status.json: immer aus dem Netz laden, nie aus dem Cache.
  if (url.pathname.endsWith('status.json')) {
    return; // Browser macht einen normalen Netzwerk-Request.
  }

  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});
