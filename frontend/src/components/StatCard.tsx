import {
  CardHeader,
  CardTitle,
  CardContent,
  CardFooter,
  Card,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { type ReactNode } from "react";

import { Link } from "react-router";

type Props = {
  title: string;
  value: ReactNode;
  detail?: ReactNode;
  isLoading?: boolean;
  /** Screen-reader text for the loading skeleton. */
  loadingLabel?: string;
  to?: string;
  linkLabel?: string;
};

export function StatCard({
  title,
  value,
  detail,
  isLoading,
  loadingLabel,
  to,
  linkLabel,
}: Props) {
  return (
    <Card className="h-full">
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton
            role="status"
            aria-label={loadingLabel}
            className="h-5 w-full"
          />
        ) : (
          <p className={cn("app-copy", "stat-value")}>{value}</p>
        )}
        {detail && !isLoading && <small className="app-copy">{detail}</small>}
      </CardContent>
      {to && linkLabel && (
        <CardFooter>
          <Link to={to}>{linkLabel}</Link>
        </CardFooter>
      )}
    </Card>
  );
}
