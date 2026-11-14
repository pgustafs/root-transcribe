import { FieldError } from "@/components/ui/field";
export function FormFieldError({
  id,
  message,
}: {
  id: string;
  message?: string;
}) {
  return message ? <FieldError id={id}>{message}</FieldError> : null;
}
