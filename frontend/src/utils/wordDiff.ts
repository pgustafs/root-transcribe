/** Word-level diff of one transcript line, for showing a correction.
 *
 * Plain LCS on whitespace-separated words (a line is a speaker turn: tens of
 * words, not thousands). Adjacent words of the same kind are joined, so
 * "Bok Demo" → "Boka demo" is one removed run and one added run.
 */

export type WordRun = { type: "same" | "removed" | "added"; text: string };

export function diffWords(before: string, after: string): WordRun[] {
  const a = before.split(/\s+/).filter(Boolean);
  const b = after.split(/\s+/).filter(Boolean);
  // lcs[i][j] = length of the LCS of a[i:] and b[j:].
  const lcs: number[][] = Array.from({ length: a.length + 1 }, () =>
    new Array<number>(b.length + 1).fill(0),
  );
  for (let i = a.length - 1; i >= 0; i--)
    for (let j = b.length - 1; j >= 0; j--)
      lcs[i][j] =
        a[i] === b[j]
          ? lcs[i + 1][j + 1] + 1
          : Math.max(lcs[i + 1][j], lcs[i][j + 1]);

  const runs: WordRun[] = [];
  const push = (type: WordRun["type"], word: string) => {
    const last = runs[runs.length - 1];
    if (last && last.type === type) last.text += ` ${word}`;
    else runs.push({ type, text: word });
  };
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      push("same", a[i]);
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) push("removed", a[i++]);
    else push("added", b[j++]);
  }
  while (i < a.length) push("removed", a[i++]);
  while (j < b.length) push("added", b[j++]);
  return runs;
}
