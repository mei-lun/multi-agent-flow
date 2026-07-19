import { get, post } from "../../api/client";
import type { Page, Run, Task } from "../../api/contracts";
export const getRun = (id: string) => get<Run>(`/api/v1/runs/${id}`);
export const getRunTasks = (id: string) => get<Page<Task> | Task[]>(`/api/v1/runs/${id}/tasks`);
export const pauseRun = (id: string) => post(`/api/v1/runs/${id}:pause`);
export const resumeRun = (id: string) => post(`/api/v1/runs/${id}:resume`);
export const cancelRun = (id: string) => post(`/api/v1/runs/${id}:cancel`);
export const retryTask = (id: string) => post(`/api/v1/tasks/${id}:retry`);
