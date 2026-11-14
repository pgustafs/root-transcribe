import { useTranslation } from "react-i18next";

/** First-use guidance for real recordings; no sample data or model settings. */
export function RecordingGuide() {
  const { t } = useTranslation("meetings");
  return (
    <section className="recording-guide" aria-label={t("guide.title")}>
      <h2>{t("guide.title")}</h2>
      <ol>
        {["capture", "review", "export"].map((step, index) => (
          <li key={step}>
            <span className="recording-guide-number" aria-hidden="true">
              {index + 1}
            </span>
            <div>
              <h3>{t(`guide.${step}.title`)}</h3>
              <p>{t(`guide.${step}.body`)}</p>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
