/** Public SSO entry point. Credentials are entered at the identity provider. */
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Navigate, useLocation } from "react-router-dom";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
} from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import { BrandMark, Lockup } from "@/components/BrandMark";
import { useConfig } from "@/hooks/useConfig";

import { useAuth } from "@/auth/useAuth";
import { Alert } from "@/components/Alert";
import { usePageTitle } from "@/hooks/usePageTitle";

export function LoginPage() {
  const { login, isAuthenticated, isLoading, sessionError } = useAuth();
  const location = useLocation();
  const { data: config } = useConfig();
  const { t } = useTranslation("auth");
  const [failed, setFailed] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  usePageTitle(t("documentTitle"));
  const from =
    (location.state as { from?: { pathname?: string } } | null)?.from
      ?.pathname ?? "/workspaces";
  if (isAuthenticated) return <Navigate to={from} replace />;

  const startLogin = async () => {
    setFailed(false);
    setSubmitting(true);
    try {
      await login(from);
    } catch {
      setFailed(true);
    } finally {
      setSubmitting(false);
    }
  };
  return (
    <main className="login-shell">
      <Card className="glass-card w-full max-w-md rounded-xl">
        <CardHeader>
          <div className="mb-6 flex items-center justify-between gap-3">
            <Lockup to="/login" />
            <BrandMark className="size-12" />
          </div>
          <p className="eyebrow px-0">
            {config?.project_name ?? t("brandAlt")}
          </p>
          <CardTitle className="text-2xl font-semibold">{t("title")}</CardTitle>
          <CardDescription>{t("subtitle")}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {sessionError && (
            <Alert
              variant="danger"
              isLiveRegion
              title={t(`session.${sessionError}`)}
            />
          )}
          {failed && (
            <Alert variant="danger" isLiveRegion title={t("loginFailed")} />
          )}
          <Button
            className="w-full"
            disabled={isLoading || submitting}
            onClick={() => void startLogin()}
          >
            {submitting && (
              <Spinner data-icon="inline-start" aria-label={t("connecting")} />
            )}{" "}
            {t("logIn")}
          </Button>
        </CardContent>
      </Card>
    </main>
  );
}
