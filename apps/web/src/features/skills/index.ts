import { post } from "../../api/client";
export const importSkill = (input: unknown) => post("/api/v1/skills/import", input);
export const testSkillVersion = (id: string, input?: unknown) => post(`/api/v1/skill-versions/${id}/test`, input);
export const publishSkillVersion = (id: string) => post(`/api/v1/skill-versions/${id}/publish`);
