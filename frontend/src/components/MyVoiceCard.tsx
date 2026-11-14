import { CardHeader, CardTitle, CardContent, Card } from "@/components/ui/card";
import { FieldDescription } from "@/components/ui/field";
import { Button } from "@/components/ui/button";

import { useState } from "react";
import { useTranslation } from "react-i18next";

import { getErrorMessage } from "@/api/client";
import { ConfirmDeleteModal } from "@/components/ConfirmDeleteModal";
import { useToast } from "@/components/toast/useToast";
import { VoiceEnrollModal } from "@/components/VoiceEnrollModal";
import { useConfig } from "@/hooks/useConfig";
import {
  useDeleteMyVoiceprints,
  useEnrollMyVoiceprint,
  useMySpeaker,
} from "@/hooks/useSpeakers";

export function MyVoiceCard() {
  const { t } = useTranslation(["meetings", "users", "common"]);
  const toast = useToast();
  const { data: config } = useConfig();
  const enabled = Boolean(config?.meetings_enabled);
  // Always read the own profile — it is a DSAR-grade, ungated endpoint. After
  // the feature is switched off a user who already enrolled must still be able
  // to withdraw consent and erase their voiceprints (issue #14 review), so the
  // card stays visible whenever data exists, not only while the feature is on.
  const mine = useMySpeaker(true);
  const enrollMine = useEnrollMyVoiceprint();
  const deleteMine = useDeleteMyVoiceprints();
  const [enrollOpen, setEnrollOpen] = useState(false);
  const [confirmEraseOpen, setConfirmEraseOpen] = useState(false);

  const myCount = mine.data?.voiceprint_count ?? 0;
  // A sample uploaded but not yet processed into a voiceprint (issue #14
  // review): erasable while pending, so the erase control must consider it even
  // though voiceprint_count is still 0.
  const pendingCount = mine.data?.pending_enrollment_count ?? 0;
  const erasable = myCount > 0 || pendingCount > 0;
  // Rights (erasure, consent withdrawal) survive feature-off wherever data or a
  // live grant exists; enrollment and new grants do not.
  if (!enabled && !erasable) return null;
  // A deactivated profile can't enroll — the API refuses it (issue #14 review).
  const revoked = mine.data?.status === "revoked";

  const erase = () =>
    deleteMine.mutate(undefined, {
      onSuccess: () => {
        setConfirmEraseOpen(false);
        toast.success(t("users:profile.voice.deleted"));
      },
      onError: (error) => {
        setConfirmEraseOpen(false);
        toast.error(
          t("users:profile.voice.deleteError"),
          getErrorMessage(error),
        );
      },
    });

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("speakers.myVoice")}</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="flex flex-col gap-4">
          <div>
            <p className="app-copy">{t("speakers.myVoiceSubtitle")}</p>
          </div>
          <div>
            <p className="app-copy">
              {myCount > 0
                ? t("speakers.myVoiceCount", { count: myCount })
                : pendingCount > 0
                  ? t("speakers.myVoicePending")
                  : t("speakers.myVoiceNone")}
            </p>
          </div>
          {revoked && (
            <div>
              <div>
                <FieldDescription>
                  {t("speakers.profileDeactivated")}
                </FieldDescription>
              </div>
            </div>
          )}
          {((enabled && !revoked) || erasable) && (
            <div>
              <div className="flex flex-wrap items-center gap-2">
                {enabled && !revoked && (
                  <div>
                    <Button
                      onClick={() => setEnrollOpen(true)}
                      variant="outline"
                    >
                      {t("speakers.enrollMine")}
                    </Button>
                  </div>
                )}
                {erasable && (
                  <div>
                    <Button
                      onClick={() => setConfirmEraseOpen(true)}
                      variant="destructive"
                    >
                      {t("speakers.deleteMine")}
                    </Button>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
        {enrollOpen && (
          <VoiceEnrollModal
            isOpen={true}
            title={t("speakers.enrollMine")}
            requireConsent={true}
            consentLabel={t("speakers.consentMine")}
            onClose={() => setEnrollOpen(false)}
            onSubmit={async (blob, filename, consent) => {
              await enrollMine.mutateAsync({ file: blob, filename, consent });
              toast.success(t("speakers.enrolledToast"));
            }}
          />
        )}
        <ConfirmDeleteModal
          isOpen={confirmEraseOpen}
          itemName={t("users:profile.voice.itemName")}
          title={t("users:profile.voice.deleteTitle")}
          consequence={t("users:profile.voice.deleteConsequence")}
          isDeleting={deleteMine.isPending}
          onConfirm={erase}
          onClose={() => setConfirmEraseOpen(false)}
        />
      </CardContent>
    </Card>
  );
}
