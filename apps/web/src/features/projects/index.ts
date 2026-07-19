import { del, get, patch, post } from "../../api/client";
import type { Page, Project } from "../../api/contracts";
export const listProjects = () => get<Page<Project> | Project[]>("/api/v1/projects");
export const createProject = (input: Pick<Project, "name"> & Partial<Project>) => post<Project>("/api/v1/projects", input);
export const updateProject = (id: string, input: Partial<Project> & { expected_version?: number }) => patch<Project>(`/api/v1/projects/${id}`, input);
export const deleteProject = (id: string) => del(`/api/v1/projects/${id}`);
