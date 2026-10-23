/* StudiRadar Service Worker: Offline-Hülle + Push-Benachrichtigungen */
const CACHE = "studiradar-v1";
const SHELL = ["/", "/static/style.css", "/static/app.js", "/static/icons/icon-192.png", "/manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  // API immer frisch (bei Funkloch: letzte Antwort aus dem Cache)
  if (url.pathname.startsWith("/api/")) {
    if (url.pathname === "/api/overview") {
      e.respondWith(
        fetch(e.request).then((res) => {
          if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
          return res;
        }).catch(() => caches.match(e.request))
      );
    }
    return;
  }
  // App-Hülle: Netz zuerst, sonst Cache
  e.respondWith(
    fetch(e.request).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
      return res;
    }).catch(() => caches.match(e.request).then((r) => r || caches.match("/")))
  );
});

self.addEventListener("push", (e) => {
  let data = {};
  try { data = e.data ? e.data.json() : {}; } catch { data = { title: "StudiRadar", body: e.data && e.data.text() }; }
  const title = data.title || "StudiRadar";
  e.waitUntil(self.registration.showNotification(title, {
    body: data.body || "",
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/badge-96.png",
    tag: data.tag || undefined,
    renotify: !!data.tag,
    data: { url: data.url || "/" },
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of all) {
      if (new URL(client.url).origin === location.origin) {
        await client.focus();
        client.postMessage({ type: "navigate", url: target });
        return;
      }
    }
    await self.clients.openWindow(target);
  })());
});
