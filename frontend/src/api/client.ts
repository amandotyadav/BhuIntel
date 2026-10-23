/** Thin fetch wrapper: bearer auth, structured API errors, JSON/multipart/form helpers. */

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";
const TOKEN_KEY = "bhuintel.token";

export class ApiError extends Error {
  status: number;
  code: string;
  details: unknown;
  requestId?: string;
  constructor(status: number, code: string, message: string, details?: unknown, requestId?: string) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
    this.requestId = requestId;
  }
}

// The token lives in sessionStorage (cleared when the tab closes). For production deployments behind
// SSO, replace with an httpOnly cookie session; see docs/SECURITY.md.
export const tokenStore = {
  get: (): string | null => {
    try {
      return sessionStorage.getItem(TOKEN_KEY);
    } catch {
      return null;
    }
  },
  set: (t: string | null) => {
    try {
      if (t) sessionStorage.setItem(TOKEN_KEY, t);
      else sessionStorage.removeItem(TOKEN_KEY);
    } catch {
      /* storage unavailable: token stays in memory only via AuthContext */
    }
  },
};

let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

async function parse(res: Response) {
  const text = await res.text();
  const body = text ? safeJson(text) : null;
  if (!res.ok) {
    const err = (body && typeof body === "object" && "error" in body ? (body as { error: Record<string, unknown> }).error : null) ?? {};
    if (res.status === 401 && onUnauthorized) onUnauthorized();
    throw new ApiError(
      res.status,
      String(err.code ?? "http_error"),
      String(err.message ?? (res.statusText || `HTTP ${res.status}`)),
      err.details,
      err.request_id ? String(err.request_id) : undefined,
    );
  }
  return body;
}

function safeJson(text: string) {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function headers(extra?: Record<string, string>) {
  const h: Record<string, string> = { ...(extra ?? {}) };
  const t = tokenStore.get();
  if (t) h.Authorization = `Bearer ${t}`;
  return h;
}

export const api = {
  async get<T>(path: string, params?: Record<string, string | number | boolean | undefined | null>): Promise<T> {
    const qs = params
      ? "?" +
        Object.entries(params)
          .filter(([, v]) => v !== undefined && v !== null && v !== "")
          .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
          .join("&")
      : "";
    return parse(await fetch(`${BASE}${path}${qs}`, { headers: headers() })) as Promise<T>;
  },
  async send<T>(method: "POST" | "PATCH" | "PUT" | "DELETE", path: string, body?: unknown): Promise<T> {
    const res = await fetch(`${BASE}${path}`, {
      method,
      headers: headers(body === undefined ? undefined : { "Content-Type": "application/json" }),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return parse(res) as Promise<T>;
  },
  async upload<T>(path: string, form: FormData): Promise<T> {
    return parse(await fetch(`${BASE}${path}`, { method: "POST", headers: headers(), body: form })) as Promise<T>;
  },
  async login(email: string, password: string) {
    const form = new URLSearchParams({ username: email, password });
    const res = await fetch(`${BASE}/api/v1/auth/token`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: form,
    });
    return parse(res);
  },
  async download(path: string, fallbackName: string) {
    const res = await fetch(`${BASE}${path}`, { headers: headers() });
    if (!res.ok) await parse(res);
    const blob = await res.blob();
    const cd = res.headers.get("Content-Disposition") ?? "";
    const m = /filename="?([^";]+)"?/.exec(cd);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = m?.[1] ?? fallbackName;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
};

export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    const det = Array.isArray(e.details)
      ? e.details.map((d: { loc?: unknown[]; msg?: string }) => `${(d.loc ?? []).slice(1).join(".")}: ${d.msg}`).join("; ")
      : "";
    return det ? `${e.message} (${det})` : e.message;
  }
  return e instanceof Error ? e.message : String(e);
}
