/** React Query hook for public app config (identity, limits and transcription availability) from the backend. */

import { useQuery } from "@tanstack/react-query";

import { getConfig } from "@/api/system";

export function useConfig() {
  return useQuery({
    queryKey: ["config"],
    // Pass the query's own signal so React Query can cancel it; passing
    // `getConfig` directly would hand it the QueryFunctionContext as the
    // signal argument and break the inferred data type.
    queryFn: ({ signal }) => getConfig(signal),
    // Availability changes while workers start or recover.
    staleTime: 15_000,
    refetchInterval: 30_000,
    retry: false,
  });
}
