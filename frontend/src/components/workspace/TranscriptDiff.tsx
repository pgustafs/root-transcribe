/** Git-style unified diff of a transcript change: +/− lines with folded unchanged runs.
 * Used when reviewing transcript revisions. */

import { useTranslation } from "react-i18next";

import { diffLines } from "@/utils/lineDiff";

export function TranscriptDiff({
  before,
  after,
}: {
  before: string;
  after: string;
}) {
  const { t } = useTranslation(["meetings"]);
  const lines = diffLines(before, after);
  return (
    <div
      className="ws-diff"
      role="group"
      // Long lines scroll horizontally: keyboard users must be able to
      // focus the region to scroll it (WCAG 2.1.1).
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
      tabIndex={0}
      aria-label={t("meetings:review.diff.aria")}
    >
      {lines.map((line, i) =>
        line.type === "skip" ? (
          <div key={i} className="ws-diff-line ws-diff-line--skip">
            {t("meetings:review.diff.hiddenLines", {
              count: line.count,
            })}
          </div>
        ) : (
          <div key={i} className={`ws-diff-line ws-diff-line--${line.type}`}>
            <span className="ws-diff-sign" aria-hidden="true">
              {line.type === "added"
                ? "+"
                : line.type === "removed"
                  ? "−"
                  : " "}
            </span>
            {/* The sign is visual only; screen readers hear the change type. */}
            {line.type !== "context" && (
              <span className="sr-only">
                {line.type === "added"
                  ? t("meetings:review.diff.addedLine")
                  : t("meetings:review.diff.removedLine")}
              </span>
            )}
            {/* Keep empty lines visible in the pre-formatted column. */}
            {line.text || " "}
          </div>
        ),
      )}
    </div>
  );
}
