import { get, put } from "../../api/client";
export const getSetting = <T = unknown>(key: string) => get<T>(`/api/v1/settings/${encodeURIComponent(key)}`);
export const updateSetting = <T = unknown>(key: string, value: T, expectedVersion?: number) => put(`/api/v1/settings/${encodeURIComponent(key)}`, { value, expected_version: expectedVersion });
