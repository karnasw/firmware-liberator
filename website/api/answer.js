// A JSON answer, never cached, and never read by a browser as anything else.
export function answer(status, body, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
      ...headers },
  });
}
