/**
 * Noticing that the slide in a shared tab changed.
 *
 * A frame is reduced to a small grey picture (its "signature"). Two
 * signatures are compared by the share of cells that differ clearly. A slide
 * is something that stays the same from one sample to the next; a change is
 * a new still picture that differs from the last one in a large part of the
 * frame. Moving video never becomes still, so it never counts as a slide.
 */

export const SIGNATURE_WIDTH = 32;
export const SIGNATURE_HEIGHT = 18;
/** How much brighter or darker a cell must be to count as different (0–255). */
const CELL_DELTA = 28;
/** Two samples this alike show the same still picture. */
export const SAME_PICTURE = 0.04;
/** A still picture this different from the last one is a new slide. */
export const NEW_SLIDE = 0.2;
/** How often the shared tab is looked at. */
export const SAMPLE_MS = 2000;
/** How long an offer to keep the previous slide stays. */
export const OFFER_MS = 20_000;

/** The share (0..1) of cells that differ clearly between two signatures. */
export function difference(a: Uint8Array, b: Uint8Array): number {
  const length = Math.min(a.length, b.length);
  if (length === 0) return 1;
  let changed = 0;
  for (let i = 0; i < length; i += 1)
    if (Math.abs(a[i] - b[i]) > CELL_DELTA) changed += 1;
  return changed / length;
}

/** A frame as 32 × 18 grey values. */
export function frameSignature(
  source: ImageBitmap | HTMLVideoElement,
): Uint8Array {
  const width = SIGNATURE_WIDTH;
  const height = SIGNATURE_HEIGHT;
  let context:
    OffscreenCanvasRenderingContext2D | CanvasRenderingContext2D | null;
  if (typeof OffscreenCanvas !== "undefined") {
    context = new OffscreenCanvas(width, height).getContext("2d", {
      willReadFrequently: true,
    });
  } else {
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    context = canvas.getContext("2d", { willReadFrequently: true });
  }
  if (!context) return new Uint8Array(0);
  context.drawImage(source, 0, 0, width, height);
  const pixels = context.getImageData(0, 0, width, height).data;
  const grey = new Uint8Array(width * height);
  for (let i = 0; i < grey.length; i += 1) {
    const p = i * 4;
    grey[i] = Math.round(
      0.299 * pixels[p] + 0.587 * pixels[p + 1] + 0.114 * pixels[p + 2],
    );
  }
  return grey;
}

export type SlideVerdict =
  /** Nothing to act on (moving picture, or the same slide). */
  | "same"
  /** The held slide is no longer showing (the talk about it ends here). */
  | "leaving"
  /** A new still picture has replaced the held slide. */
  | "changed";

/**
 * Feed it one signature per sample. `hold` says whether the sample is a still
 * picture worth holding on to as "the slide that is showing now".
 */
export class SlideDetector {
  private previous: Uint8Array | null = null;
  private held: Uint8Array | null = null;
  private left = false;

  reset(): void {
    this.previous = null;
    this.held = null;
    this.left = false;
  }

  feed(signature: Uint8Array): { verdict: SlideVerdict; hold: boolean } {
    const previous = this.previous;
    this.previous = signature;
    const away =
      this.held !== null && difference(signature, this.held) > NEW_SLIDE;
    const still =
      previous !== null && difference(signature, previous) < SAME_PICTURE;
    if (!still) {
      if (away && !this.left) {
        this.left = true;
        return { verdict: "leaving", hold: false };
      }
      return { verdict: "same", hold: false };
    }
    const changed = away;
    this.held = signature;
    this.left = false;
    return { verdict: changed ? "changed" : "same", hold: true };
  }
}
