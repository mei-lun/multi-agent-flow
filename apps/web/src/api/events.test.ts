import { describe, expect, it } from "vitest";
import { eventStreamUrl } from "./events";

describe("event stream resume", () => {
  it("carries the persisted Last-Event-ID cursor", () => {
    expect(eventStreamUrl("/api/v1/runs/r/events", "evt-42", "http://localhost")).toContain("last_event_id=evt-42");
  });
});
