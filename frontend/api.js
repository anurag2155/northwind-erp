// Transport. No DOM and no rendering, so it imports in the browser and in Node.

export class ApiError extends Error {
  constructor(status, body) {
    super(body.error || `HTTP_${status}`);
    this.status = status;
    this.code = body.error || `HTTP_${status}`;
    this.detail = body;
  }
}

export function createApi(base, token) {
  async function request(method, path, body) {
    let res;
    try {
      res = await fetch(base + path, {
        method,
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (cause) {   // backend down is a UI state, not a console error
      throw new ApiError(0, { error: "BACKEND_UNREACHABLE", detail: String(cause) });
    }
    const text = await res.text();
    let parsed = {};
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        throw new ApiError(res.status, { error: "MALFORMED_RESPONSE", raw: text.slice(0, 200) });
      }
    }
    if (!res.ok) throw new ApiError(res.status, parsed);
    return parsed;
  }

  return {
    token,
    request,
    get: (path) => request("GET", path),
    post: (path, body) => request("POST", path, body || {}),
    availability: (sku, wh) => request("GET", `/inventory/skus/${encodeURIComponent(sku)}` +
      `/availability?warehouse_id=${encodeURIComponent(wh)}`),
  };
}

// The UI never hard-codes RBAC: `permissions` comes from GET /me, which the
// backend derives from the ROUTES list it enforces, so a server-side role
// change is followed with no frontend edit.
export function can(session, method, path) {
  if (!session) return false;
  return session.permissions.some((p) => p.method === method && p.path === path);
}
