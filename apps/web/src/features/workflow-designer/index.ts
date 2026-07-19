import { get, post, put } from "../../api/client";
export const createWorkflow = (input: unknown) => post("/api/v1/workflows", input);
export const createWorkflowVersion = (id: string, input: { based_on_version_id?: string | null; change_summary?: string; idempotency_key: string }) => post(`/api/v1/workflows/${id}/versions`, input);
export const saveWorkflowGraph = (id: string, graph: unknown, expectedVersion?: number) => put(`/api/v1/workflow-versions/${id}/graph`, { ...(graph as object), expected_version: expectedVersion, idempotency_key: crypto.randomUUID() });
export const validateWorkflow = (id: string) => post(`/api/v1/workflow-versions/${id}/validate`);
export const publishWorkflow = (id: string, expectedVersion?: number) => post(`/api/v1/workflow-versions/${id}/publish`, { expected_version: expectedVersion, idempotency_key: crypto.randomUUID() });
export const diffWorkflow = (id: string, other: string) => get(`/api/v1/workflow-versions/${id}/diff?other=${encodeURIComponent(other)}`);
