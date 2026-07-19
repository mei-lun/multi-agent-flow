import { get, post } from "../../api/client";
export const getRepositoryChange = (runId: string) => get(`/api/v1/runs/${runId}/repository-change`);
export const mergeRepositoryChange = (id: string, expectedHead: string) => post(`/api/v1/repository-changes/${id}:merge`, { expected_head: expectedHead });
