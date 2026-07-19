import { get } from "../../api/client";
import type { Page, Project, Run } from "../../api/contracts";
export const dashboardQueries = { projects: () => get<Page<Project> | Project[]>("/api/v1/projects"), runs: (projectId: string) => get<Page<Run> | Run[]>(`/api/v1/projects/${projectId}/runs`) };
