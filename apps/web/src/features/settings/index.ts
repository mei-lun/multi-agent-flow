import { get, put } from "../../api/client";
export const listNodes = () => get<Array<Record<string, unknown>>>("/api/v1/nodes");
export const getProjectorStatus = () => get<Record<string, unknown>>("/api/v1/git/projector");
export const getSetting = <T = unknown>(key: string) => get<T>(`/api/v1/settings/${encodeURIComponent(key)}`);
export const updateSetting = <T = unknown>(key: string, value: T, expectedVersion?: number) => put(`/api/v1/settings/${encodeURIComponent(key)}`, { value, expected_version: expectedVersion });
