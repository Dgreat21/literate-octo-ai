/* Hearth service worker.
 *
 * The shell is cached so the app opens instantly. State is NEVER cached:
 * a cached /api response would show yesterday's numbers as today's, which is
 * exactly the lie the whole design exists to prevent (INFRA-7 §3, INFRA-11 DoD).
 */
const SHELL = 'hearth-shell-v5';
const FILES = [
  './index.html', './styles.css', './app.js', './manifest.webmanifest',
  './fonts/caprasimo-latin.woff2', './fonts/caprasimo-latin-ext.woff2',
  './fonts/figtree-latin.woff2', './fonts/figtree-latin-ext.woff2',
  './icons/icon-180.png', './icons/icon-192.png', './icons/icon-512.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  // Network-only, no fallback. Offline must surface as a failed fetch so the
  // client can say "unreachable" instead of rendering a cached answer.
  if (url.pathname.startsWith('/api/')) return;

  if (e.request.method !== 'GET') return;
  e.respondWith(
    caches.match(e.request, { ignoreSearch: true })
      .then((hit) => hit || fetch(e.request))
  );
});
