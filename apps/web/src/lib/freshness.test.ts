import { describe, expect, it } from "vitest";
import { isNewContent, NEW_CONTENT_WINDOW_MS } from "@/lib/freshness";

const NOW = Date.parse("2026-07-17T00:00:00Z");

describe("isNewContent", () => {
  it("marks content created within 24 hours as new", () => {
    expect(isNewContent("2026-07-16T00:00:01Z", NOW)).toBe(true);
  });

  it("expires exactly at the 24 hour boundary", () => {
    const createdAt = new Date(NOW - NEW_CONTENT_WINDOW_MS).toISOString();
    expect(isNewContent(createdAt, NOW)).toBe(false);
  });

  it("rejects missing, invalid, and implausibly future timestamps", () => {
    expect(isNewContent(null, NOW)).toBe(false);
    expect(isNewContent("not-a-date", NOW)).toBe(false);
    expect(isNewContent("2026-07-17T01:00:00Z", NOW)).toBe(false);
  });

  it("allows small server and browser clock skew", () => {
    expect(isNewContent("2026-07-17T00:03:00Z", NOW)).toBe(true);
  });
});
