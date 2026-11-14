import { describe, expect, it } from "vitest";

import { formatDuration } from "@/utils/formatDuration";

describe("formatDuration", () => {
  it("floors instead of rounding (regression: 119.6s once rendered '1:60')", () => {
    expect(formatDuration(119.6)).toBe("1:59");
    expect(formatDuration(120)).toBe("2:00");
  });

  it("pads seconds and handles edge inputs", () => {
    expect(formatDuration(0)).toBe("0:00");
    expect(formatDuration(61)).toBe("1:01");
    expect(formatDuration(468.4)).toBe("7:48");
    expect(formatDuration(-3)).toBe("0:00");
  });
});
