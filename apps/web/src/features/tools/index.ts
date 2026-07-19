import { del, get, post } from "../../api/client";
export const listTools = () => get("/api/v1/tools");
export const registerTool = (input: unknown) => post("/api/v1/tools", input);
export const removeTool = (name: string, version: string) => del(`/api/v1/tools/${encodeURIComponent(name)}/${encodeURIComponent(version)}`);
export const simulatePolicy = (input: unknown) => post("/api/v1/policies/simulate", input);
