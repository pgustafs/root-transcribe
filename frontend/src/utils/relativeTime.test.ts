import { describe, expect, it } from "vitest";

import { formatRelativeTime } from "./relativeTime";

describe("formatRelativeTime", () => {
  const now = new Date("2026-07-05T12:00:00Z");

  it("formats moments ago as now/minutes", () => {
    expect(formatRelativeTime("2026-07-05T11:59:40Z", "en", now)).toMatch(
      /now|minute/,
    );
    expect(formatRelativeTime("2026-07-05T11:15:00Z", "en", now)).toBe(
      "45 minutes ago",
    );
  });

  it("formats hours and days ago", () => {
    expect(formatRelativeTime("2026-07-05T09:00:00Z", "en", now)).toBe(
      "3 hours ago",
    );
    expect(formatRelativeTime("2026-07-03T12:00:00Z", "en", now)).toBe(
      "2 days ago",
    );
  });

  it("falls back to a short date beyond a week", () => {
    expect(formatRelativeTime("2026-06-05T12:00:00Z", "en", now)).toBe("Jun 5");
  });

  it("adds the year for other years", () => {
    expect(formatRelativeTime("2025-06-05T12:00:00Z", "en", now)).toBe(
      "Jun 5, 2025",
    );
  });

  it("localizes (Swedish)", () => {
    expect(formatRelativeTime("2026-07-02T12:00:00Z", "sv", now)).toBe(
      "för 3 dagar sedan",
    );
  });

  it("returns the input for an unparseable timestamp", () => {
    expect(formatRelativeTime("not-a-date", "en", now)).toBe("not-a-date");
  });
});
