/** Git-style line diff for the note-history "View change" panel.
 *
 * Plain LCS on lines (after trimming the common prefix/suffix, so typical
 * localized edits stay cheap), folded like `git diff`: long unchanged runs
 * collapse to a "N lines hidden" marker with `context` lines kept around
 * each change.
 */

export type DiffLine =
  | { type: "added" | "removed" | "context"; text: string }
  | { type: "skip"; count: number };

/** Guard for the O(n·m) LCS table on pathological inputs (notes are ≤100k chars,
 * so after prefix/suffix trimming this is effectively never hit). */
const MAX_LCS_CELLS = 4_000_000;

function splitLines(text: string): string[] {
  if (text === "") return [];
  // A trailing newline should not produce a phantom empty last line.
  const lines = text.split("\n");
  if (lines[lines.length - 1] === "") lines.pop();
  return lines;
}

/** Raw per-line ops (no folding): the LCS walk over before → after. */
function diffOps(
  before: string[],
  after: string[],
): Exclude<DiffLine, { type: "skip" }>[] {
  // Trim the common prefix/suffix — edits are usually localized, and this keeps
  // the DP table small.
  let start = 0;
  while (
    start < before.length &&
    start < after.length &&
    before[start] === after[start]
  ) {
    start++;
  }
  let endB = before.length;
  let endA = after.length;
  while (endB > start && endA > start && before[endB - 1] === after[endA - 1]) {
    endB--;
    endA--;
  }
  const midB = before.slice(start, endB);
  const midA = after.slice(start, endA);

  const ops: Exclude<DiffLine, { type: "skip" }>[] = before
    .slice(0, start)
    .map((text) => ({ type: "context" as const, text }));

  if ((midB.length + 1) * (midA.length + 1) > MAX_LCS_CELLS) {
    // Too large to align line-by-line: show the middle as replaced wholesale.
    ops.push(...midB.map((text) => ({ type: "removed" as const, text })));
    ops.push(...midA.map((text) => ({ type: "added" as const, text })));
  } else {
    // LCS lengths table over the (small) middle.
    const rows = midB.length + 1;
    const cols = midA.length + 1;
    const lcs = new Int32Array(rows * cols);
    for (let i = midB.length - 1; i >= 0; i--) {
      for (let j = midA.length - 1; j >= 0; j--) {
        lcs[i * cols + j] =
          midB[i] === midA[j]
            ? lcs[(i + 1) * cols + j + 1] + 1
            : Math.max(lcs[(i + 1) * cols + j], lcs[i * cols + j + 1]);
      }
    }
    let i = 0;
    let j = 0;
    while (i < midB.length && j < midA.length) {
      if (midB[i] === midA[j]) {
        ops.push({ type: "context", text: midB[i] });
        i++;
        j++;
      } else if (lcs[(i + 1) * cols + j] >= lcs[i * cols + j + 1]) {
        ops.push({ type: "removed", text: midB[i] });
        i++;
      } else {
        ops.push({ type: "added", text: midA[j] });
        j++;
      }
    }
    while (i < midB.length) ops.push({ type: "removed", text: midB[i++] });
    while (j < midA.length) ops.push({ type: "added", text: midA[j++] });
  }

  ops.push(
    ...before.slice(endB).map((text) => ({ type: "context" as const, text })),
  );
  return ops;
}

/**
 * Diff two texts line-by-line, git-style: changed lines as added/removed,
 * `context` unchanged lines kept around each change, and longer unchanged runs
 * folded into a `skip` marker. Identical texts yield a single skip (or nothing
 * when empty).
 */
export function diffLines(
  before: string,
  after: string,
  context = 3,
): DiffLine[] {
  const ops = diffOps(splitLines(before), splitLines(after));

  const out: DiffLine[] = [];
  let run: string[] = [];
  let seenChange = false;
  const flushRun = (isLast: boolean) => {
    const lead = seenChange ? Math.min(context, run.length) : 0;
    const tail = isLast ? 0 : Math.min(context, run.length - lead);
    const hidden = run.length - lead - tail;
    run.slice(0, lead).forEach((text) => out.push({ type: "context", text }));
    // Only fold when the marker actually saves space.
    if (hidden > 1) {
      out.push({ type: "skip", count: hidden });
    } else {
      run
        .slice(lead, run.length - tail)
        .forEach((text) => out.push({ type: "context", text }));
    }
    run.slice(run.length - tail).forEach((text) => {
      out.push({ type: "context", text });
    });
    run = [];
  };

  for (const op of ops) {
    if (op.type === "context") {
      run.push(op.text);
    } else {
      flushRun(false);
      out.push(op);
      seenChange = true;
    }
  }
  flushRun(true);
  return out;
}
