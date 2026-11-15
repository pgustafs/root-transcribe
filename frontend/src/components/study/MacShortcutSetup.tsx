/**
 * Pair a Mac keyboard shortcut with this account: create the key, show the
 * exact request the Shortcuts app must send, and test it.
 *
 * The key is shown once (only its hash is stored on the server) and can do
 * one thing: start or stop a note in the owner's running recording.
 */

import { CheckIcon, CopyIcon } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { hotkeyUrl, sendHotkey } from "@/api/study";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import { Spinner } from "@/components/ui/spinner";
import {
  useCreateHotkeyCredential,
  useHotkeyCredential,
  useRevokeHotkeyCredential,
  useShortcutHint,
  writeShortcutHint,
} from "@/hooks/useStudy";
import { formatRelativeTime } from "@/utils/relativeTime";

type Props = {
  isOpen: boolean;
  onClose: () => void;
  /** Milliseconds from the last shortcut press to its frame, when known. */
  lastDelayMs?: number | null;
};

/** A value to copy, shown whole (it wraps) with a copy button. */
function Copyable({ label, value }: { label: string; value: string }) {
  const { t } = useTranslation(["study", "common"]);
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      <div className="flex items-start gap-2">
        <code className="min-w-0 flex-1 rounded-md border bg-muted px-2 py-1.5 font-mono text-xs break-all">
          {value}
        </code>
        <Button
          type="button"
          variant="outline"
          size="icon-sm"
          aria-label={
            copied
              ? t("study:shortcut.copied")
              : `${t("common:actions.copy")}: ${label}`
          }
          onClick={() => {
            void navigator.clipboard
              .writeText(value)
              .then(() => setCopied(true))
              .catch(() => setCopied(false));
          }}
        >
          {copied ? <CheckIcon /> : <CopyIcon />}
        </Button>
      </div>
    </div>
  );
}

export function MacShortcutSetup({ isOpen, onClose, lastDelayMs }: Props) {
  const { t, i18n } = useTranslation(["study", "common"]);
  const credential = useHotkeyCredential(isOpen);
  const create = useCreateHotkeyCredential();
  const revoke = useRevokeHotkeyCredential();
  const hint = useShortcutHint();
  // The secret exists only here, right after it was created.
  const [token, setToken] = useState<string | null>(null);
  const [test, setTest] = useState<{ ok: boolean; text: string } | null>(null);
  const [testing, setTesting] = useState(false);

  const exists = credential.data?.exists ?? false;
  const bearer = token
    ? t("study:shortcut.bearerValue", { token })
    : t("study:shortcut.bearerPlaceholder");
  const url = hotkeyUrl("press");
  const testUrl = hotkeyUrl("test");
  const curl = `curl -sS -X POST -H "Authorization: ${bearer}" -H "X-Invocation-Id: $(date +%s%N)" "${url}"`;

  const createKey = async () => {
    setTest(null);
    const created = await create.mutateAsync();
    setToken(created.token);
  };
  const runTest = async () => {
    if (!token) return;
    setTesting(true);
    try {
      setTest(await sendHotkey(token, "test"));
    } catch {
      setTest({ ok: false, text: "Request failed" });
    } finally {
      setTesting(false);
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t("study:shortcut.title")}</DialogTitle>
          <DialogDescription>{t("study:shortcut.intro")}</DialogDescription>
        </DialogHeader>
        <ol className="flex list-decimal flex-col gap-5 pl-5 text-sm">
          <li>
            <div className="flex flex-col gap-3">
              <p>{t("study:shortcut.step1")}</p>
              {token && (
                <Alert
                  variant="warning"
                  isInline
                  title={t("study:shortcut.keyShownOnce")}
                />
              )}
              {!token && exists && (
                <p className="text-muted-foreground">
                  {t("study:shortcut.activeKey", {
                    hint: credential.data?.hint ?? "",
                  })}{" "}
                  {credential.data?.last_used_at
                    ? t("study:shortcut.lastUsed", {
                        when: formatRelativeTime(
                          credential.data.last_used_at,
                          i18n.language,
                        ),
                      })
                    : t("study:shortcut.neverUsed")}
                </p>
              )}
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  type="button"
                  variant={exists ? "outline" : "default"}
                  size="sm"
                  disabled={create.isPending}
                  aria-busy={create.isPending}
                  onClick={() => void createKey()}
                >
                  {create.isPending && (
                    <Spinner data-icon="inline-start" aria-hidden="true" />
                  )}
                  {exists
                    ? t("study:shortcut.replace")
                    : t("study:shortcut.create")}
                </Button>
                {exists && (
                  <Button
                    type="button"
                    variant="destructive"
                    size="sm"
                    disabled={revoke.isPending}
                    onClick={() => {
                      setToken(null);
                      setTest(null);
                      revoke.mutate();
                    }}
                  >
                    {t("study:shortcut.revoke")}
                  </Button>
                )}
              </div>
              {exists && !token && (
                <p className="text-xs text-muted-foreground">
                  {t("study:shortcut.replaceHelp", {
                    hint: credential.data?.hint ?? "",
                  })}
                </p>
              )}
              <div aria-live="polite">
                {revoke.isSuccess && !exists && (
                  <p className="text-muted-foreground">
                    {t("study:shortcut.revoked")}
                  </p>
                )}
              </div>
            </div>
          </li>
          <li>
            <div className="flex flex-col gap-3">
              <p>{t("study:shortcut.step2")}</p>
              <ol className="flex list-[lower-alpha] flex-col gap-3 pl-5">
                <li>{t("study:shortcut.action1")}</li>
                <li>
                  <div className="flex flex-col gap-2">
                    <span>{t("study:shortcut.action2")}</span>
                    <Copyable
                      label={t("study:shortcut.action2Url")}
                      value={url}
                    />
                    <Copyable
                      label={t("study:shortcut.action2Header1")}
                      value={bearer}
                    />
                    <span className="text-muted-foreground">
                      {t("study:shortcut.action2Header2")}
                    </span>
                  </div>
                </li>
                <li>{t("study:shortcut.action3")}</li>
              </ol>
            </div>
          </li>
          <li>{t("study:shortcut.step3")}</li>
          <li>
            <div className="flex flex-col gap-3">
              <p>{t("study:shortcut.step4")}</p>
              <Copyable label={t("study:shortcut.testUrl")} value={testUrl} />
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={!token || testing}
                  aria-busy={testing}
                  onClick={() => void runTest()}
                >
                  {testing && (
                    <Spinner data-icon="inline-start" aria-hidden="true" />
                  )}
                  {t("study:shortcut.testHere")}
                </Button>
              </div>
              {!token && (
                <p className="text-xs text-muted-foreground">
                  {t("study:shortcut.testNeedsKey")}
                </p>
              )}
              <div aria-live="polite">
                {test && (
                  <Alert
                    variant={test.ok ? "success" : "warning"}
                    isInline
                    title={test.text}
                  />
                )}
              </div>
            </div>
          </li>
        </ol>
        <Separator />
        <details className="text-sm">
          <summary className="cursor-pointer font-medium">
            {t("study:shortcut.curlTitle")}
          </summary>
          <div className="mt-3 flex flex-col gap-2">
            <p className="text-muted-foreground">
              {t("study:shortcut.curlHelp")}
            </p>
            <Copyable label="curl" value={curl} />
          </div>
        </details>
        <Field>
          <FieldLabel htmlFor="study-shortcut-hint">
            {t("study:shortcut.hintLabel")}
          </FieldLabel>
          <Input
            id="study-shortcut-hint"
            className="max-w-40"
            defaultValue={hint}
            placeholder={t("study:shortcut.hintPlaceholder")}
            maxLength={16}
            onChange={(event) => writeShortcutHint(event.currentTarget.value)}
          />
          <FieldDescription>{t("study:shortcut.hintHelp")}</FieldDescription>
        </Field>
        {typeof lastDelayMs === "number" && (
          <p className="text-sm text-muted-foreground">
            {t("study:shortcut.lastDelay", { ms: lastDelayMs })}
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            {t("common:actions.close")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
