export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}
export async function api<T>(
  path: string,
  body?: unknown,
  method?: string,
): Promise<T> {
  const response = await fetch("/api/v1" + path, {
    method: method || (body ? "POST" : "GET"),
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    let message = "Request failed";
    try {
      const json = await response.json();
      message =
        typeof json.detail === "string"
          ? json.detail
          : json.detail?.message || JSON.stringify(json.detail);
    } catch {
      message = response.statusText;
    }
    throw new ApiError(response.status, message);
  }
  return response.json();
}
export const commandId = () =>
  crypto.randomUUID?.() ||
  Array.from(crypto.getRandomValues(new Uint8Array(16)), (n) =>
    n.toString(16).padStart(2, "0"),
  ).join("");
