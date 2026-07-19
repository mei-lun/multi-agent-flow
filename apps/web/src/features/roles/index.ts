import { post } from "../../api/client";
export const createRole = (input: unknown) => post("/api/v1/roles", input);
export const dryRunRole = (id: string, input?: unknown) => post(`/api/v1/role-versions/${id}/dry-run`, input);
export const publishRole = (id: string) => post(`/api/v1/role-versions/${id}/publish`);
