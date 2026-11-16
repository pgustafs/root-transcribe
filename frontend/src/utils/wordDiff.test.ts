import { expect, it } from "vitest";

import { diffWords } from "./wordDiff";

it("shows a correction as the words that went and the words that came", () => {
  expect(
    diffWords(
      "Ska vi ha kvar Bok Demo och läsa om plattformen?",
      "Ska vi ha kvar Boka demo och läsa om plattformen?",
    ),
  ).toEqual([
    { type: "same", text: "Ska vi ha kvar" },
    { type: "removed", text: "Bok Demo" },
    { type: "added", text: "Boka demo" },
    { type: "same", text: "och läsa om plattformen?" },
  ]);
});

it("handles a change at either end and no change at all", () => {
  expect(diffWords("Gärna, Thomas. Tja.", "Tjena, Thomas. Tja.")).toEqual([
    { type: "removed", text: "Gärna," },
    { type: "added", text: "Tjena," },
    { type: "same", text: "Thomas. Tja." },
  ]);
  expect(diffWords("som en oneminer.", "som en oneliner.")).toEqual([
    { type: "same", text: "som en" },
    { type: "removed", text: "oneminer." },
    { type: "added", text: "oneliner." },
  ]);
  expect(diffWords("Ja, exakt.", "Ja,  exakt.")).toEqual([
    { type: "same", text: "Ja, exakt." },
  ]);
  expect(diffWords("", "")).toEqual([]);
});
