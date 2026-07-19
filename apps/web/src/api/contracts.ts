/** Small, stable DTOs used by the console. Server OpenAPI generation can replace this file. */
export type Role = "ADMIN" | "DESIGNER" | "OPERATOR" | "REVIEWER" | "VIEWER";
export interface ApiErrorBody { code?: string; message?: string; trace_id?: string; detail?: string; }
export interface User { id: string; username: string; display_name?: string; roles?: Role[]; permissions?: string[]; }
export interface LoginResponse { access_token?: string; token_type?: string; expires_at?: string; user?: User; }
export interface Project { id: string; name: string; description?: string; status?: string; repository_count?: number; updated_at?: string; }
export interface Run { id: string; project_id?: string; status: string; control_commit?: string; created_at?: string; completed_at?: string; }
export interface Task { id: string; name?: string; status: string; owner?: string; epoch?: number; progress?: number; }
export interface InboxItem { id: string; type?: string; title?: string; status: string; project_id?: string; subject_version?: number; created_at?: string; }
export interface ModelConnection { id: string; name: string; provider?: string; status?: string; model_count?: number; }
export interface Page<T> { items: T[]; next_cursor?: string | null; total?: number; }
