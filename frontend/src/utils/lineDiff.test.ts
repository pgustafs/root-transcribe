import { describe, expect, it } from "vitest";

import { diffLines } from "./lineDiff";

describe("diffLines", () => {
  it("marks a changed line as removed + added", () => {
    expect(diffLines("a\nold\nz", "a\nnew\nz")).toEqual([
      { type: "context", text: "a" },
      { type: "removed", text: "old" },
      { type: "added", text: "new" },
      { type: "context", text: "z" },
    ]);
  });

  it("marks pure additions and removals", () => {
    expect(diffLines("a\nb", "a\nb\nc")).toEqual([
      { type: "context", text: "a" },
      { type: "context", text: "b" },
      { type: "added", text: "c" },
    ]);
    expect(diffLines("a\nb\nc", "a\nc")).toEqual([
      { type: "context", text: "a" },
      { type: "removed", text: "b" },
      { type: "context", text: "c" },
    ]);
  });

  it("folds long unchanged runs around a change, git-style", () => {
    const unchanged = Array.from({ length: 20 }, (_, i) => `line ${i}`);
    const before = [...unchanged, "old", ...unchanged].join("\n");
    const after = [...unchanged, "new", ...unchanged].join("\n");
    const out = diffLines(before, after, 3);

    // Leading run: fold to a skip + 3 trailing context lines.
    expect(out[0]).toEqual({ type: "skip", count: 17 });
    expect(out.slice(1, 4).every((l) => l.type === "context")).toBe(true);
    expect(out[4]).toEqual({ type: "removed", text: "old" });
    expect(out[5]).toEqual({ type: "added", text: "new" });
    // Trailing run: 3 context lines + a fold.
    expect(out.slice(6, 9).every((l) => l.type === "context")).toBe(true);
    expect(out[9]).toEqual({ type: "skip", count: 17 });
  });

  it("collapses identical texts to a single skip", () => {
    const text = Array.from({ length: 10 }, (_, i) => `l${i}`).join("\n");
    expect(diffLines(text, text)).toEqual([{ type: "skip", count: 10 }]);
  });

  it("does not fold when the hidden run would be a single line", () => {
    // 1 hidden line saves nothing — keep it as context instead of a marker.
    const out = diffLines("a\nb\nc\nd\nold", "a\nb\nc\nd\nnew", 3);
    expect(out.filter((l) => l.type === "skip")).toHaveLength(0);
    expect(out).toHaveLength(6);
  });

  it("ignores a trailing newline", () => {
    // Identical after normalization; a single unchanged line is not folded.
    expect(diffLines("a\n", "a")).toEqual([{ type: "context", text: "a" }]);
  });

  it("handles empty sides", () => {
    expect(diffLines("", "a\nb")).toEqual([
      { type: "added", text: "a" },
      { type: "added", text: "b" },
    ]);
    expect(diffLines("a", "")).toEqual([{ type: "removed", text: "a" }]);
  });
});
