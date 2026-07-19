import { del, get, patch, post } from "../../api/client";
import type { ModelConnection, Page } from "../../api/contracts";
export const listModelConnections = () => get<Page<ModelConnection> | ModelConnection[]>("/api/v1/model-connections");
export const createModelConnection = (input: Omit<ModelConnection, "id"> & { secret?: string }) => post<ModelConnection>("/api/v1/model-connections", input);
export const updateModelConnection = (id: string, input: Partial<ModelConnection> & { secret?: string; expected_version?: number }) => patch<ModelConnection>(`/api/v1/model-connections/${id}`, input);
export const testModelConnection = (id: string) => post<Record<string, unknown>>(`/api/v1/model-connections/${id}/test`);
export const deleteModelConnection = (id: string) => del(`/api/v1/model-connections/${id}`);
