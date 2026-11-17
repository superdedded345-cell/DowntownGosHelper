// Service Worker: кеширует страницу, чтобы сайт открывался без интернета.
// Файлы manifest.json, icon.svg, sw.js и сам html (лучше назвать index.html) лежат в одной папке на https-хостинге.
const CACHE = "gosHelper-v1";
const FILES = ["./", "index.html", "config.js", "manifest.json", "icon.svg"];
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => Promise.all(FILES.map((f) => c.add(f).catch(() => null)))).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET" || new URL(req.url).origin !== location.origin) return;   // запросы к ИИ не трогаем
  e.respondWith(
    fetch(req).then((res) => { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); return res; })
      .catch(() => caches.match(req).then((r) => r || caches.match("index.html")))
  );
});
