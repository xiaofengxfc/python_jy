/** Thin fetch wrapper. Every failure becomes an Error with the server's
 * Chinese `detail` text, because that is what the panels display verbatim. */
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }
  if (!response.ok) {
    const detail =
      body && typeof body === "object" && "detail" in body
        ? String((body as { detail: unknown }).detail)
        : `请求失败（HTTP ${response.status}）`;
    throw new Error(detail);
  }
  return body as T;
}

export function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  return search.toString();
}
