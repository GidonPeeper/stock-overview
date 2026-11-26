/* Service worker: opens the dashboard at once, even while the server is asleep
   (Render's free plan takes about a minute to wake up) or the phone is offline.

   Only the page itself ("/") is cached. No API data is stored here: the page
   keeps its own last snapshot in localStorage and marks it "updating…".

   Network first: whenever the server answers within WAIT_MS with this app's
   page (it carries the X-Stock-Overview header), that fresh page is used and
   cached. Otherwise the cached page opens and fills itself in when the server
   is up. Redirects (e.g. to /login when signed out) always pass through. */
const CACHE = "so-shell-v4";   // bump when index.html changes shape, so phones get the new page
const WAIT_MS = 1500;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (key !== CACHE) await caches.delete(key);
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET" || req.mode !== "navigate") return;
  if (new URL(req.url).pathname !== "/") return;
  event.respondWith(shell(req));
});

async function shell(req) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match("/");
  const network = fetch(req).then((res) => {
    const ours = res.ok && res.type === "basic" && !!res.headers.get("x-stock-overview");
    if (ours) cache.put("/", res.clone()).catch(() => {});
    return { res, pass: ours || res.type === "opaqueredirect" };
  });
  if (!cached) return network.then((x) => x.res);
  const timeout = new Promise((resolve) => setTimeout(() => resolve(cached), WAIT_MS));
  return Promise.race([
    network.then((x) => (x.pass ? x.res : cached), () => cached),
    timeout,
  ]);
}
