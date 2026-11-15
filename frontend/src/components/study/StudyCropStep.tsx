/**
 * Choose which part of the shared tab a note keeps: the whole frame, or the
 * video area (to leave out recommendations, comments and page controls).
 *
 * The area can be dragged, moved and resized with the arrow keys, or typed as
 * percentages. It is stored in normalized coordinates of the captured frame.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { KeyboardEvent, PointerEvent as ReactPointerEvent } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import {
  clampCrop,
  type Crop,
  FULL_FRAME,
  isFullFrame,
} from "@/lib/study/frame";

/** A sensible first guess: a centred 16:9-ish player without the page chrome. */
const DEFAULT_AREA: Crop = { x: 0.05, y: 0.1, width: 0.65, height: 0.65 };
const STEP = 0.01;

type Props = {
  getPreview: () => Promise<Blob>;
  initial: Crop;
  onChange: (crop: Crop) => void;
};

type Drag = {
  kind: "move" | "resize";
  startX: number;
  startY: number;
  crop: Crop;
  width: number;
  height: number;
};

export function StudyCropStep({ getPreview, initial, onChange }: Props) {
  const { t } = useTranslation("study");
  const [preview, setPreview] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [mode, setMode] = useState<"full" | "area">(
    isFullFrame(initial) ? "full" : "area",
  );
  const [area, setArea] = useState<Crop>(
    isFullFrame(initial) ? DEFAULT_AREA : initial,
  );
  const frameRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<Drag | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const blob = await getPreview();
      setPreview((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return URL.createObjectURL(blob);
      });
    } catch {
      setPreview(null);
    } finally {
      setLoading(false);
    }
  }, [getPreview]);

  useEffect(() => {
    void load();
  }, [load]);
  useEffect(
    () => () => {
      setPreview((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return null;
      });
    },
    [],
  );

  const apply = (nextMode: "full" | "area", nextArea: Crop) => {
    const safe = clampCrop(nextArea);
    setMode(nextMode);
    setArea(safe);
    onChange(nextMode === "full" ? FULL_FRAME : safe);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    const delta: Record<string, [number, number]> = {
      ArrowLeft: [-STEP, 0],
      ArrowRight: [STEP, 0],
      ArrowUp: [0, -STEP],
      ArrowDown: [0, STEP],
    };
    const move = delta[event.key];
    if (!move) return;
    event.preventDefault();
    apply(
      "area",
      event.shiftKey
        ? {
            ...area,
            width: area.width + move[0],
            height: area.height + move[1],
          }
        : { ...area, x: area.x + move[0], y: area.y + move[1] },
    );
  };

  const startDrag = (kind: Drag["kind"]) => (event: ReactPointerEvent) => {
    const box = frameRef.current?.getBoundingClientRect();
    if (!box) return;
    event.preventDefault();
    event.stopPropagation();
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    dragRef.current = {
      kind,
      startX: event.clientX,
      startY: event.clientY,
      crop: area,
      width: box.width,
      height: box.height,
    };
  };
  const onDrag = (event: ReactPointerEvent) => {
    const drag = dragRef.current;
    if (!drag) return;
    const dx = (event.clientX - drag.startX) / drag.width;
    const dy = (event.clientY - drag.startY) / drag.height;
    apply(
      "area",
      drag.kind === "move"
        ? { ...drag.crop, x: drag.crop.x + dx, y: drag.crop.y + dy }
        : {
            ...drag.crop,
            width: drag.crop.width + dx,
            height: drag.crop.height + dy,
          },
    );
  };
  const endDrag = () => {
    dragRef.current = null;
  };

  const percent = (value: number) => Math.round(value * 100);
  const numberField = (name: "x" | "y" | "width" | "height", label: string) => (
    <Field>
      <FieldLabel htmlFor={`crop-${name}`}>{label}</FieldLabel>
      <Input
        id={`crop-${name}`}
        type="number"
        min={0}
        max={100}
        step={1}
        value={percent(area[name])}
        onChange={(event) => {
          const value = Number(event.currentTarget.value);
          if (Number.isFinite(value))
            apply("area", { ...area, [name]: value / 100 });
        }}
      />
    </Field>
  );

  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-muted-foreground">{t("crop.intro")}</p>
      <ToggleGroup
        type="single"
        variant="outline"
        className="flex-wrap justify-start"
        value={mode}
        onValueChange={(value) => {
          if (value) apply(value as "full" | "area", area);
        }}
      >
        <ToggleGroupItem value="full">{t("crop.fullFrame")}</ToggleGroupItem>
        <ToggleGroupItem value="area">{t("crop.selectArea")}</ToggleGroupItem>
      </ToggleGroup>
      <div
        ref={frameRef}
        className="relative w-full overflow-hidden rounded-md border bg-muted select-none"
      >
        {preview ? (
          <img
            src={preview}
            alt={t("crop.previewAlt")}
            className="block h-auto w-full"
            draggable={false}
          />
        ) : (
          <div className="flex aspect-video items-center justify-center p-4 text-center text-sm text-muted-foreground">
            {loading ? <Spinner /> : t("crop.previewFailed")}
          </div>
        )}
        {preview && mode === "area" && (
          <div
            className="absolute"
            style={{
              left: `${area.x * 100}%`,
              top: `${area.y * 100}%`,
              width: `${area.width * 100}%`,
              height: `${area.height * 100}%`,
            }}
          >
            <button
              type="button"
              aria-label={t("crop.area")}
              aria-describedby="crop-area-help"
              data-testid="crop-area"
              className="absolute inset-0 cursor-move touch-none border-2 border-primary bg-primary/10 outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
              onKeyDown={onKeyDown}
              onPointerDown={startDrag("move")}
              onPointerMove={onDrag}
              onPointerUp={endDrag}
              onPointerCancel={endDrag}
            />
            <span
              role="presentation"
              className="absolute -right-1.5 -bottom-1.5 size-4 cursor-nwse-resize touch-none rounded-sm border-2 border-primary bg-background"
              onPointerDown={startDrag("resize")}
              onPointerMove={onDrag}
              onPointerUp={endDrag}
              onPointerCancel={endDrag}
            />
          </div>
        )}
      </div>
      {mode === "area" ? (
        <>
          <FieldDescription id="crop-area-help">
            {t("crop.areaHelp")}
          </FieldDescription>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {numberField("x", t("crop.left"))}
            {numberField("y", t("crop.top"))}
            {numberField("width", t("crop.width"))}
            {numberField("height", t("crop.height"))}
          </div>
        </>
      ) : (
        <FieldDescription>{t("crop.fullscreenHint")}</FieldDescription>
      )}
      <div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => void load()}
          disabled={loading}
        >
          {t("crop.refresh")}
        </Button>
      </div>
    </div>
  );
}
