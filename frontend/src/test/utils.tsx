import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi } from "vitest";
import { AuthProvider } from "../auth/AuthContext";

export type Handler = (url: string, init?: RequestInit) => { status?: number; body: unknown } | undefined;

/** Route fetch() calls to handlers; unmatched calls fail the test loudly. */
export function mockFetch(handler: Handler) {
  const calls: { url: string; init?: RequestInit }[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    calls.push({ url, init });
    const r = handler(url, init);
    if (!r) throw new Error(`Unhandled fetch ${init?.method ?? "GET"} ${url}`);
    return new Response(JSON.stringify(r.body), { status: r.status ?? 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  return calls;
}

export const USER = { id: 1, email: "rev@example.org", full_name: "Rev", role: "reviewer", is_active: true };

export function renderApp(ui: ReactNode, { path = "/", route = "/" }: { path?: string; route?: string } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthProvider>
        <MemoryRouter initialEntries={[route]}>
          <Routes><Route path={path} element={ui} /></Routes>
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}
