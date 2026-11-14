import { CardHeader, CardTitle, CardContent, Card } from "@/components/ui/card";
import { Avatar, AvatarImage, AvatarFallback } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import { CopyValue } from "@/components/CopyValue";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { updateCurrentUser } from "@/api/users";
import { getErrorMessage } from "@/api/client";
import { useAuth } from "@/auth/useAuth";
import userAvatarFallback from "@/assets/user-avatar.svg";
import { Alert } from "@/components/Alert";
import { manageIdentity } from "@/auth/keycloak";
import { MyVoiceCard } from "@/components/MyVoiceCard";
import { PageHeader } from "@/components/PageHeader";
import { useToast } from "@/components/toast/useToast";

// Limit uploaded avatars so the base64 data URL stays small (server caps too).
const MAX_AVATAR_BYTES = 256 * 1024;

function readFileAsDataUrl(file: File, readError: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(new Error(readError));
    reader.readAsDataURL(file);
  });
}

export function ProfilePage() {
  const { t } = useTranslation(["users", "common", "validation"]);
  const { user, refreshUser } = useAuth();
  const toast = useToast();
  const [error, setError] = useState<string | null>(null);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const [avatarError, setAvatarError] = useState<string | null>(null);
  const [avatarBusy, setAvatarBusy] = useState(false);

  const saveAvatar = async (avatar_url: string | null) => {
    setAvatarError(null);
    setAvatarBusy(true);
    try {
      await updateCurrentUser({ avatar_url });
      await refreshUser();
      toast.success(
        avatar_url
          ? t("users:profile.avatarUpdated")
          : t("users:profile.avatarRemoved"),
      );
    } catch (err) {
      setAvatarError(
        getErrorMessage(err, t("users:profile.avatarUpdateError")),
      );
    } finally {
      setAvatarBusy(false);
    }
  };

  const handleAvatarFile = async (file: File | undefined) => {
    if (!file) {
      return;
    }
    if (file.size > MAX_AVATAR_BYTES) {
      setAvatarError(t("users:profile.tooLarge"));
      return;
    }
    try {
      const dataUrl = await readFileAsDataUrl(
        file,
        t("users:profile.readError"),
      );
      await saveAvatar(dataUrl);
    } catch (err) {
      setAvatarError(getErrorMessage(err, t("users:profile.readError")));
    }
  };

  return (
    <>
      <PageHeader
        title={t("users:profile.title")}
        description={t("users:profile.description")}
      />
      <section className="page-section flex-1 min-h-0">
        <div className="flex flex-col gap-4">
          <div>
            <Card>
              <CardHeader>
                <CardTitle>{t("users:profile.pictureTitle")}</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="flex flex-col gap-4">
                  {avatarError && (
                    <div>
                      <Alert
                        variant="danger"
                        isInline={true}
                        isLiveRegion={true}
                        title={avatarError}
                      />
                    </div>
                  )}
                  <div>
                    <div className="flex flex-wrap items-center gap-4">
                      <div>
                        <Avatar>
                          <AvatarImage
                            src={user?.avatar_url || userAvatarFallback}
                            alt={t("users:profile.avatarAlt")}
                          />
                          <AvatarFallback>U</AvatarFallback>
                        </Avatar>
                      </div>
                      <div>
                        <div className="flex flex-col gap-4">
                          <div>
                            <p className="app-copy">
                              {t("users:profile.pictureHelp")}
                            </p>
                          </div>
                          <div>
                            <div className="flex flex-wrap items-center gap-2">
                              <div>
                                <Button
                                  onClick={() => fileInputRef.current?.click()}
                                  disabled={avatarBusy}
                                  variant="outline"
                                >
                                  {t("users:profile.uploadPicture")}
                                </Button>
                              </div>
                              {user?.avatar_url && (
                                <div>
                                  <Button
                                    onClick={() => void saveAvatar(null)}
                                    disabled={avatarBusy}
                                    variant="destructive"
                                  >
                                    {t("users:profile.remove")}
                                  </Button>
                                </div>
                              )}
                            </div>
                          </div>
                        </div>
                        {/* The native picker is driven by the Upload button. */}
                        <input
                          ref={fileInputRef}
                          type="file"
                          accept="image/png,image/jpeg"
                          aria-label={t("users:profile.uploadAria")}
                          hidden={true}
                          onChange={(event) => {
                            void handleAvatarFile(event.target.files?.[0]);
                            event.target.value = "";
                          }}
                        />
                      </div>
                    </div>
                  </div>
                </div>
              </CardContent>
            </Card>
          </div>
          <div>
            <Card>
              <CardHeader>
                <CardTitle>{t("users:profile.detailsTitle")}</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="flex flex-col gap-4">
                  {error && (
                    <div>
                      <Alert
                        variant="danger"
                        isInline={true}
                        isLiveRegion={true}
                        title={error}
                      />
                    </div>
                  )}
                  <div>
                    <p className="app-copy">
                      {t("users:identity.profileHelp")}
                    </p>
                  </div>
                  <div>
                    <p className="app-copy">
                      {user?.full_name}· {user?.email}
                    </p>
                  </div>
                  <div>
                    <CopyValue aria-label={t("users:profile.copyUserId")}>
                      {user?.id ?? ""}
                    </CopyValue>
                  </div>
                  <div>
                    <Button
                      onClick={() => {
                        setError(null);
                        void manageIdentity().catch(() =>
                          setError(t("users:identity.openError")),
                        );
                      }}
                      variant="default"
                    >
                      {t("users:identity.manageAccount")}
                    </Button>
                  </div>
                </div>
              </CardContent>
            </Card>
          </div>
          {/* Voiceprint enrollment for meeting speaker identification (hidden
              when the meetings feature is disabled on this deployment). */}
          <div>
            <MyVoiceCard />
          </div>
        </div>
      </section>
    </>
  );
}
