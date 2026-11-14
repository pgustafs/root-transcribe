import { CircleAlertIcon as ExclamationCircleIcon } from "lucide-react";
import {
  EmptyDescription,
  EmptyContent,
  Empty,
  EmptyHeader,
  EmptyTitle,
  EmptyMedia,
} from "@/components/ui/empty";
import { Button } from "@/components/ui/button";
import { Component, type ErrorInfo, type ReactNode } from "react";
import { withTranslation, type WithTranslation } from "react-i18next";

type Props = WithTranslation & { children: ReactNode };
type State = { hasError: boolean };

class ErrorBoundaryInner extends Component<Props, State> {
  state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Diagnostics sink — a real telemetry/Sentry hook can replace this.
    console.error("Unhandled render error:", error, info.componentStack);
  }

  private handleReload = () => window.location.reload();

  render() {
    const { t, children } = this.props;
    if (!this.state.hasError) return children;

    return (
      // Bullseye fills the 100%-tall #root, centring the fallback.
      <div className="flex min-h-40 items-center justify-center">
        <Empty>
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <ExclamationCircleIcon />
            </EmptyMedia>
            <EmptyTitle role="heading" aria-level={Number("h1".slice(1))}>
              {t("errorBoundary.title")}
            </EmptyTitle>
          </EmptyHeader>
          <EmptyDescription>{t("errorBoundary.body")}</EmptyDescription>
          <EmptyContent>
            <div className="flex flex-wrap justify-center gap-2">
              <Button onClick={this.handleReload} variant="default">
                {t("errorBoundary.reload")}
              </Button>
            </div>
          </EmptyContent>
        </Empty>
      </div>
    );
  }
}

export const ErrorBoundary = withTranslation("common")(ErrorBoundaryInner);
