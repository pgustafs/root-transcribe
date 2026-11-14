/** Friendly display only: preserve the diarizer's original label in API writes. */
export function speakerLabel(
  label: string,
  translate: (number: number) => string,
): string {
  const match = /^SPEAKER_(\d+)$/i.exec(label);
  return match ? translate(Number(match[1]) + 1) : label;
}
