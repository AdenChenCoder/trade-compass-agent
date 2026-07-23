import { describe, expect, it } from "vitest";
import { getActivityDisplayMode } from "@/lib/activity-trace";

describe("getActivityDisplayMode", () => {
  it("keeps activity expanded while tools are still producing the answer", () => {
    expect(getActivityDisplayMode(true, false, true)).toBe("expanded");
  });

  it("collapses activity as soon as the final answer starts streaming", () => {
    expect(getActivityDisplayMode(true, true, true)).toBe("collapsed");
  });

  it("keeps activity collapsed after streaming completes", () => {
    expect(getActivityDisplayMode(false, true, true)).toBe("collapsed");
  });

  it("does not render an empty collapsed trace", () => {
    expect(getActivityDisplayMode(true, true, false)).toBe("hidden");
  });
});
