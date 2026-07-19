/** Small, stable DTOs used by the console. Server OpenAPI generation can replace this file. */
export type Role = "ADMIN" | "DESIGNER" | "OPERATOR" | "REVIEWER" | "VIEWER";
export interface ApiErrorBody { code?: string; message?: string; trace_id?: string; detail?: string; }
export interface User { id: string; username: string; display_name?: string; roles?: Role[]; permissions?: string[]; }
export interface LoginResponse { token?: string; access_token?: string; token_type?: string; expires_at?: string; user?: User; }
export interface Project { id: string; name: string; description?: string; status?: string; repository_count?: number; updated_at?: string; version?: number; default_workflow_version_id?: string | null; }
export interface RepositoryBinding { id: string; project_id: string; repository_url: string; branch: string; credential_type: string; credential_configured: boolean; verified: boolean; verified_at?: string | null; version?: number; }
export interface Run { id: string; project_id?: string; status: string; version?: number; control_commit?: string; base_commit?: string; branch?: string; created_at?: string; completed_at?: string; progress?: number; total_tasks?: number; completed_tasks?: number; blocked_tasks?: number; current_node?: string; }
export interface Task { id: string; name?: string; status: string; owner?: string; epoch?: number; progress?: number; branch?: string; issue?: string; started_at?: string; completed_at?: string; }
export interface InboxItem { id: string; type?: string; title?: string; description?: string; status: string; project_id?: string; subject_version?: number; created_at?: string; assignee_id?: string | null; metadata?: Record<string, unknown>; }
export interface ModelConnection { id: string; name: string; provider?: string; model_id?: string; api_base?: string; status?: string; credential_type?: string; model_count?: number; version?: number; capabilities?: Record<string, unknown>; }
export interface Artifact { id: string; name?: string; artifact_type?: string; size_bytes?: number; content_url?: string; preview?: string; created_at?: string; checksum?: string; }
export interface RepositoryChange { id: string; run_id?: string; repository_url?: string; branch?: string; base_commit?: string; head_commit?: string; checks?: Array<{ name: string; status: string; url?: string }>; final_gate?: string; mergeable?: boolean; }
export interface AuditEvent { id: string; event_type: string; actor_id?: string; resource_type?: string; resource_id?: string; created_at?: string; summary?: string; }
export interface WorkflowNode { id: string; type: string; label?: string; x?: number; y?: number; config?: Record<string, unknown>; }
export interface WorkflowEdge { id?: string; source: string; target: string; label?: string; }
export interface WorkflowVersion { id: string; workflow_id?: string; version_no?: number; status?: string; expected_version?: number; nodes?: WorkflowNode[]; edges?: WorkflowEdge[]; }
export interface ValidationReport { valid?: boolean; reachable_node_keys?: string[]; errors?: Array<{ code?: string; message: string; node_id?: string; edge_id?: string }>; warnings?: Array<{ code?: string; message: string; node_id?: string; edge_id?: string }>; }
export interface Page<T> { items: T[]; next_cursor?: string | null; total?: number; }
