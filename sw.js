// Terminradar Großenkneten – einfacher Service Worker
// Cached nur die App-Hülle (HTML/CSS/JS/Icons), damit die App installierbar
// ist und offline zumindest ihr Grundgerüst anzeigt. status.json und externe
// Dienste (Karte, Suche) werden bewusst NICHT gecacht.
//
// Die Seite selbst wird "network first" geladen: Ist man online, kommt immer
// die neueste index.html – der Cache greift nur offline. So erscheinen
// Updates sofort, ohne dass man CACHE_NAME hochzählen muss.

const CACHE_NAME = 'terminradar-shell-v10';
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
  const req = event.request;
  const url = new URL(req.url);

  // Nur GET-Anfragen der eigenen Seite behandeln, alles andere (Overpass,
  // Photon, Kartenkacheln, status.json) geht direkt ins Netz.
  if (req.method !== 'GET' || url.origin !== self.location.origin) return;
  if (url.pathname.endsWith('status.json')) return;

  // Seite: erst Netz, bei Fehler Cache.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put('./index.html', copy));
          return res;
        })
        .catch(() => caches.match('./index.html'))
    );
    return;
  }

  // Icons, Manifest: erst Cache, dann Netz.
  event.respondWith(
    caches.match(req).then((cached) => cached || fetch(req))
  );
});
