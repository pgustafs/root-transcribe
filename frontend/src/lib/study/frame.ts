/**
 * The visible frame of the shared tab, for study notes.
 *
 * A crop is stored in normalized capture coordinates (0..1 of the frame), so
 * it stays right when the shared tab is resized or goes fullscreen.
 */

export type Crop = { x: number; y: number; width: number; height: number };

export const FULL_FRAME: Crop = { x: 0, y: 0, width: 1, height: 1 };
const MIN_SIZE = 0.05;

export function isFullFrame(crop: Crop): boolean {
  return crop.x <= 0 && crop.y <= 0 && crop.width >= 1 && crop.height >= 1;
}

/** Keep a crop inside the frame and above a usable minimum size. */
export function clampCrop(crop: Crop): Crop {
  const width = Math.min(1, Math.max(MIN_SIZE, crop.width));
  const height = Math.min(1, Math.max(MIN_SIZE, crop.height));
  const x = Math.min(1 - width, Math.max(0, crop.x));
  const y = Math.min(1 - height, Math.max(0, crop.y));
  const round = (value: number) => Math.round(value * 10000) / 10000;
  return {
    x: round(x),
    y: round(y),
    width: round(width),
    height: round(height),
  };
}

/** The crop in whole pixels of a frame (never empty, never outside). */
export function cropPixels(
  crop: Crop,
  frameWidth: number,
  frameHeight: number,
): { sx: number; sy: number; sw: number; sh: number } {
  const safe = clampCrop(crop);
  const sx = Math.min(frameWidth - 1, Math.round(safe.x * frameWidth));
  const sy = Math.min(frameHeight - 1, Math.round(safe.y * frameHeight));
  const sw = Math.max(
    1,
    Math.min(frameWidth - sx, Math.round(safe.width * frameWidth)),
  );
  const sh = Math.max(
    1,
    Math.min(frameHeight - sy, Math.round(safe.height * frameHeight)),
  );
  return { sx, sy, sw, sh };
}

type FrameSource = ImageBitmap | HTMLVideoElement;

function sourceSize(source: FrameSource): { width: number; height: number } {
  return "videoWidth" in source
    ? { width: source.videoWidth, height: source.videoHeight }
    : { width: source.width, height: source.height };
}

type GrabbingCapture = { grabFrame: () => Promise<ImageBitmap> };
declare const ImageCapture:
  { new (track: MediaStreamTrack): GrabbingCapture } | undefined;

/**
 * Pins the newest frame of a shared tab. `ImageCapture` reads the track
 * directly, so it works while this page is in a background tab; the hidden
 * video element is the fallback where that API is missing or stalls (a tab
 * that shows a still picture produces no new frames).
 */
export class FrameGrabber {
  private capture: GrabbingCapture | null = null;
  private video: HTMLVideoElement | null = null;

  constructor(private readonly track: MediaStreamTrack) {
    if (typeof ImageCapture !== "undefined") {
      try {
        this.capture = new ImageCapture(track);
      } catch {
        this.capture = null;
      }
    }
    if (typeof document !== "undefined") {
      const video = document.createElement("video");
      video.muted = true;
      video.playsInline = true;
      video.srcObject = new MediaStream([track]);
      void video.play().catch(() => {});
      this.video = video;
    }
  }

  get live(): boolean {
    return this.track.readyState === "live";
  }

  /** The newest frame at native resolution (sharp on Retina displays). */
  async grab(timeoutMs = 400): Promise<ImageBitmap> {
    if (!this.live) throw new Error("The shared tab is no longer available");
    if (this.capture) {
      try {
        return await Promise.race([
          this.capture.grabFrame(),
          new Promise<never>((_, reject) =>
            setTimeout(() => reject(new Error("timeout")), timeoutMs),
          ),
        ]);
      } catch {
        // Fall through to the video element.
      }
    }
    if (this.video && this.video.videoWidth > 0)
      return createImageBitmap(this.video);
    throw new Error("No frame is available from the shared tab");
  }

  close(): void {
    if (this.video) {
      this.video.pause();
      this.video.srcObject = null;
      this.video = null;
    }
    this.capture = null;
  }
}

/** Encode a pinned frame (cropped) as PNG. */
export async function frameToPng(
  source: FrameSource,
  crop: Crop,
): Promise<Blob> {
  const { width, height } = sourceSize(source);
  const { sx, sy, sw, sh } = cropPixels(crop, width, height);
  if (typeof OffscreenCanvas !== "undefined") {
    const canvas = new OffscreenCanvas(sw, sh);
    const context = canvas.getContext("2d");
    if (!context) throw new Error("Canvas is unavailable");
    context.drawImage(source, sx, sy, sw, sh, 0, 0, sw, sh);
    return canvas.convertToBlob({ type: "image/png" });
  }
  const canvas = document.createElement("canvas");
  canvas.width = sw;
  canvas.height = sh;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("Canvas is unavailable");
  context.drawImage(source, sx, sy, sw, sh, 0, 0, sw, sh);
  return new Promise((resolve, reject) =>
    canvas.toBlob(
      (blob) =>
        blob ? resolve(blob) : reject(new Error("PNG encoding failed")),
      "image/png",
    ),
  );
}
