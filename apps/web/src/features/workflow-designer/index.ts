import { get, post, put } from "../../api/client";
export const createWorkflow = (input: unknown) => post("/api/v1/workflows", input);
export const createWorkflowVersion = (id: string) => post(`/api/v1/workflows/${id}/versions`);
export const saveWorkflowGraph = (id: string, graph: unknown) => put(`/api/v1/workflow-versions/${id}/graph`, graph);
export const validateWorkflow = (id: string) => post(`/api/v1/workflow-versions/${id}/validate`);
export const publishWorkflow = (id: string) => post(`/api/v1/workflow-versions/${id}/publish`);
export const diffWorkflow = (id: string, other: string) => get(`/api/v1/workflow-versions/${id}/diff?other=${encodeURIComponent(other)}`);
