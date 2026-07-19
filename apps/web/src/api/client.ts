/** Typed HTTP transport with credentials, timeout, request id and trace-id aware errors. */
import type { ApiErrorBody } from "./contracts";

export class ApiError extends Error {
  readonly status: number; readonly code?: string; readonly traceId?: string;
  constructor(message: string, status: number, body: ApiErrorBody = {}) {
    super(message); this.name = "ApiError"; this.status = status; this.code = body.code;
    this.traceId = body.trace_id ?? (body as { traceId?: string }).traceId;
  }
}

const baseUrl = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ?? "";
const tokenKey = "maf.access_token";
export const authToken = () => sessionStorage.getItem(tokenKey);
export const setAuthToken = (token: string | null) => token ? sessionStorage.setItem(tokenKey, token) : sessionStorage.removeItem(tokenKey);

export async function request<T>(path: string, init: RequestInit = {}, timeoutMs = 15_000): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const token = authToken(); if (token) headers.set("Authorization", `Bearer ${token}`);
  headers.set("X-Request-ID", crypto.randomUUID());
  try {
    const response = await fetch(`${baseUrl}${path}`, { ...init, headers, credentials: "include", signal: controller.signal });
    const text = await response.text();
    let body: ApiErrorBody & T = {} as ApiErrorBody & T;
    if (text) { try { body = JSON.parse(text) as ApiErrorBody & T; } catch { body = text as ApiErrorBody & T; } }
    if (!response.ok) {
      const data = body as ApiErrorBody;
      const message = response.status === 409
        ? `${data.message ?? data.detail ?? "资源已被其他用户更新"}。请刷新后重试。`
        : data.message ?? data.detail ?? `Request failed (${response.status})`;
      throw new ApiError(message, response.status, data);
    }
    return body as T;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw new ApiError("请求超时，请稍后重试", 408);
    throw error;
  } finally { window.clearTimeout(timer); }
}

export const get = <T>(path: string) => request<T>(path);
export const post = <T>(path: string, body?: unknown) => request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
export const patch = <T>(path: string, body: unknown) => request<T>(path, { method: "PATCH", body: JSON.stringify(body) });
export const put = <T>(path: string, body: unknown) => request<T>(path, { method: "PUT", body: JSON.stringify(body) });
export const del = <T = void>(path: string) => request<T>(path, { method: "DELETE" });
