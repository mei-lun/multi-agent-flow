import { get, post } from "../../api/client";
import type { InboxItem, Page } from "../../api/contracts";
export const listInbox = (query = "") => get<Page<InboxItem> | InboxItem[]>(`/api/v1/inbox${query ? `?${query}` : ""}`);
export const decideInbox = (id: string, decision: "APPROVE" | "REJECT" | "REQUEST_CHANGES", comment?: string, subjectVersion?: number) => post<InboxItem>(`/api/v1/inbox/${id}:decide`, { decision, comment, subject_version: subjectVersion });
