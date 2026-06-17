// Thin fetch helpers. Every call uses a relative path (/api/...) so the Vite
// proxy (00 §26.4) routes it to the backend — never a hardcoded host/port.

const RETRYABLE_STATUSES = new Set([500, 502, 503, 504]);
const GET_RETRY_DELAYS_MS = [250, 500, 1000];

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function requestOnce<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    throw new Error(`${init?.method ?? "GET"} ${path} -> ${res.status}`);
  }
  const text = await res.text();
  return (text ? JSON.parse(text) : null) as T;
}

async function request<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const method = init?.method ?? "GET";
  const canRetry = method === "GET";
  let lastError: unknown;

  for (let attempt = 0; attempt <= GET_RETRY_DELAYS_MS.length; attempt += 1) {
    try {
      return await requestOnce<T>(path, init);
    } catch (err) {
      lastError = err;
      const message = err instanceof Error ? err.message : "";
      const status = Number(message.match(/->\s+(\d+)/)?.[1] ?? 0);
      if (
        !canRetry ||
        attempt >= GET_RETRY_DELAYS_MS.length ||
        (status > 0 && !RETRYABLE_STATUSES.has(status))
      ) {
        throw err;
      }
      await sleep(GET_RETRY_DELAYS_MS[attempt]);
    }
  }

  throw lastError instanceof Error ? lastError : new Error(`GET ${path} failed`);
}

export function apiGet<T>(path: string): Promise<T> {
  return request<T>(path);
}

export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export function apiPatch<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function apiDelete<T>(path: string): Promise<T> {
  return request<T>(path, { method: "DELETE" });
}
