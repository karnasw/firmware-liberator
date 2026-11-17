// The admin view's page (admin/index.js) from made-up figures, for seeing it
// before a deploy: sample-stats.mjs's database, read by the view's own code.
// Access is left out, and with it every change the page offers (its forms
// answer 404 here), and Cloudflare's analytics, which need a token. Run by
// preview.sh; never deployed.
//   node website/scripts/admin-preview.mjs ADDRESS PORT
import http from "node:http";
import { page } from "../admin/index.js";
import { sampleDatabase } from "./sample-stats.mjs";

// What the preview answers a request: the page for GET /, else 404.
export async function previewer(now = new Date()) {
  const { db } = await sampleDatabase(now);
  return async (method, pathname) => (method === "GET" && pathname === "/"
    ? { status: 200, body: await page({ DB: db }, { email: "owner@example.com" }, now) }
    : { status: 404, body: "Not in the preview: it shows the page, and changes nothing." });
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const [address, port] = process.argv.slice(2);
  const answer = await previewer();
  http.createServer(async (request, response) => {
    const { status, body } = await answer(request.method, new URL(request.url, "http://preview").pathname);
    response.writeHead(status, { "Content-Type": status === 200 ? "text/html; charset=utf-8" : "text/plain" });
    response.end(body);
  }).listen(Number(port), address, () => console.log(`Admin view on ${address}:${port}`));
}
