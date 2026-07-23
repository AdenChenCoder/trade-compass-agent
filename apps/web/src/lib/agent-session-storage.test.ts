import { afterEach, describe, expect, it } from "vitest";
import {
  AGENT_SESSION_STORAGE_KEY,
  clearStoredSessionId,
  getStoredSessionId,
  setStoredSessionId,
} from "@/lib/agent-session-storage";

describe("agent-session-storage", () => {
  afterEach(() => {
    clearStoredSessionId();
  });

  it("returns null when nothing is stored", () => {
    expect(getStoredSessionId()).toBeNull();
  });

  it("round-trips a stored session id", () => {
    setStoredSessionId("session-42");
    expect(getStoredSessionId()).toBe("session-42");
    expect(localStorage.getItem(AGENT_SESSION_STORAGE_KEY)).toBe("session-42");
  });

  it("clears stored session id", () => {
    setStoredSessionId("session-42");
    clearStoredSessionId();
    expect(getStoredSessionId()).toBeNull();
  });

  it("ignores empty values when reading", () => {
    localStorage.setItem(AGENT_SESSION_STORAGE_KEY, "   ");
    expect(getStoredSessionId()).toBeNull();
  });
});
