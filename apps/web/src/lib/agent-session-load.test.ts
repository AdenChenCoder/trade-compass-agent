import { describe, expect, it } from "vitest";
import {
  isNearMessageListBottom,
  prependUniqueMessages,
  shouldFetchStoredSessionOnMount,
} from "@/lib/agent-session-load";

describe("shouldFetchStoredSessionOnMount", () => {
  it("returns false when there is no stored session id (no GET on mount)", () => {
    expect(shouldFetchStoredSessionOnMount(null)).toBe(false);
    expect(shouldFetchStoredSessionOnMount(undefined)).toBe(false);
    expect(shouldFetchStoredSessionOnMount("")).toBe(false);
    expect(shouldFetchStoredSessionOnMount("   ")).toBe(false);
  });

  it("returns true when a non-empty session id is stored (GET once on mount)", () => {
    expect(shouldFetchStoredSessionOnMount("abc-123")).toBe(true);
    expect(shouldFetchStoredSessionOnMount("  abc-123  ")).toBe(true);
  });

  it("encodes mount scenarios: blank session never triggers GET", () => {
    const noGetOnMount: Array<string | null | undefined> = [
      null,
      undefined,
      "",
      "   ",
    ];
    for (const stored of noGetOnMount) {
      expect(shouldFetchStoredSessionOnMount(stored)).toBe(false);
    }
  });

  it("encodes mount scenarios: remount with prior session triggers GET", () => {
    expect(shouldFetchStoredSessionOnMount("session-from-first-turn")).toBe(
      true,
    );
  });
});

describe("paginated message helpers", () => {
  it("prepends older messages without duplicating an overlapping cursor item", () => {
    const current = [{ id: "2" }, { id: "3" }];
    const older = [{ id: "1" }, { id: "2" }];
    expect(prependUniqueMessages(current, older).map((item) => item.id)).toEqual([
      "1",
      "2",
      "3",
    ]);
  });

  it("only follows new messages while the reader remains near the bottom", () => {
    expect(isNearMessageListBottom(1000, 780, 200)).toBe(true);
    expect(isNearMessageListBottom(1000, 300, 200)).toBe(false);
  });
});
