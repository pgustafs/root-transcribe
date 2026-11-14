/** Root component: wires up data, routing, and auth providers. */

import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nextProvider } from "react-i18next";
import { BrowserRouter } from "react-router-dom";

import { AuthProvider } from "@/auth/AuthProvider";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { RecordingProvider } from "@/components/recording/RecordingProvider";
import { ToastProvider } from "@/components/toast/ToastProvider";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useTheme } from "@/hooks/useTheme";
import i18n from "@/i18n";
import { AppRoutes } from "@/routes";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 10_000,
    },
  },
});

/** Apply the user’s light/dark preference before sign-in as well. */
function ThemeGate({ children }: { children: ReactNode }) {
  useTheme();
  return <>{children}</>;
}

export function App() {
  return (
    <I18nextProvider i18n={i18n}>
      <ErrorBoundary>
        <QueryClientProvider client={queryClient}>
          <ThemeGate>
            <TooltipProvider>
              <BrowserRouter
                future={{
                  v7_startTransition: true,
                  v7_relativeSplatPath: true,
                }}
              >
                <AuthProvider>
                  {/* App-level so an active meeting recording (and its recording
                    dock) survives route changes anywhere in the SPA. */}
                  <RecordingProvider>
                    <ToastProvider>
                      <AppRoutes />
                    </ToastProvider>
                  </RecordingProvider>
                </AuthProvider>
              </BrowserRouter>
            </TooltipProvider>
          </ThemeGate>
        </QueryClientProvider>
      </ErrorBoundary>
    </I18nextProvider>
  );
}
