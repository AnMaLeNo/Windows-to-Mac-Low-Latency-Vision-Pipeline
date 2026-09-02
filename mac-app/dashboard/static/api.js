// The JSON routes. A non-2xx becomes an Error carrying the server's `error` field when
// the body is JSON, else the status line - panels show that text inline, never alert().
async function request(path, init) {
  const res = await fetch(path, init);
  const text = await res.text();
  let body, isJSON = false;
  try { body = JSON.parse(text); isJSON = true; } catch { /* not JSON */ }
  if (!res.ok) {
    const msg = isJSON && body && typeof body === "object" && body.error != null
      ? String(body.error) : `${res.status} ${res.statusText}`.trim();
    throw new Error(msg);
  }
  if (!isJSON) throw new Error(`${path}: response is not JSON`);
  return body;
}

export function getJSON(path) {
  return request(path, { headers: { accept: "application/json" } });
}

export function postJSON(path, body = {}) {
  return request(path, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(body),
  });
}
