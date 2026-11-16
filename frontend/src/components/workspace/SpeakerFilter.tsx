import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { cn } from "@/lib/utils";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import type { MeetingSegment } from "@/api/types";
import { speakerLabel } from "@/utils/speakerLabel";

/** Filter detected voices without changing transcript labels or enrolled identities. */
export function SpeakerFilter({
  segments,
  selected,
  onSelect,
}: {
  segments: MeetingSegment[];
  selected: string | null;
  onSelect: (speaker: string | null) => void;
}) {
  const { t } = useTranslation("meetings");
  const speakers = useMemo(() => {
    const groups = new Map<string, MeetingSegment[]>();
    for (const segment of segments) {
      const group = groups.get(segment.speaker_label) ?? [];
      group.push(segment);
      groups.set(segment.speaker_label, group);
    }
    return [...groups].map(([label, turns]) => ({
      label,
      count: turns.length,
      // A single-turn correction may give one detected voice multiple names.
      // Only show a name for the whole group when every turn agrees.
      name: turns.every((turn) => turn.speaker_name === turns[0].speaker_name)
        ? turns[0].speaker_name
        : null,
    }));
  }, [segments]);
  return (
    <ToggleGroup
      variant="default"
      size="sm"
      spacing={1}
      className="flex-wrap"
      value={[selected ?? "__all__"]}
      onValueChange={([value]) =>
        onSelect(value && value !== "__all__" ? value : null)
      }
      aria-label={t("review.filterSpeakers")}
    >
      <ToggleGroupItem value="__all__">
        {t("review.allSpeakers", { count: speakers.length })}
      </ToggleGroupItem>
      {speakers.map((speaker, index) => (
        <ToggleGroupItem key={speaker.label} value={speaker.label}>
          <span
            className={cn("speaker-dot", `speaker-color-${index % 6}`)}
            aria-hidden="true"
          />
          {speaker.name ??
            speakerLabel(speaker.label, (number) =>
              t("review.speaker", { number }),
            )}
          <span className="speaker-turn-count">
            {t("review.turns", { count: speaker.count })}
          </span>
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  );
}
