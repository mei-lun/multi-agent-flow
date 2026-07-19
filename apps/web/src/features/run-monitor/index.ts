import { get, post } from "../../api/client";
import type { Page, Run, Task } from "../../api/contracts";
export const getRun = (id: string) => get<Run>(`/api/v1/runs/${id}`);
export const getRunTasks = (id: string) => get<Page<Task> | Task[]>(`/api/v1/runs/${id}/tasks`);
const command = (id: string, reason: string, expectedVersion?: number) => ({ reason, expected_version: expectedVersion ?? 0, idempotency_key: crypto.randomUUID() });
export const pauseRun = (id: string, expectedVersion?: number) => post(`/api/v1/runs/${id}:pause`, command(id, "user_pause", expectedVersion));
export const resumeRun = (id: string, expectedVersion?: number) => post(`/api/v1/runs/${id}:resume`, command(id, "user_resume", expectedVersion));
export const cancelRun = (id: string, expectedVersion?: number) => post(`/api/v1/runs/${id}:cancel`, command(id, "user_cancel", expectedVersion));
export const retryTask = (id: string, expectedTaskVersion?: number) => post(`/api/v1/tasks/${id}:retry`, { reason: "user_retry", expected_task_version: expectedTaskVersion ?? 0, reset_to_artifact_version_ids: [], idempotency_key: crypto.randomUUID() });
