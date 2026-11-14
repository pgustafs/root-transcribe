import { useState } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { I18nextProvider } from "react-i18next";
import { expect, it, vi } from "vitest";
import i18n from "@/i18n";
import { ConfirmDeleteModal } from "./ConfirmDeleteModal";
import { Button } from "@/components/ui/button";

it("focuses the safe cancel action and restores focus without deleting", async () => {
  const actor = userEvent.setup();
  const onDelete = vi.fn();
  function Example() {
    const [open, setOpen] = useState(false);
    return (
      <>
        <Button onClick={() => setOpen(true)}>Delete recording</Button>
        <ConfirmDeleteModal
          isOpen={open}
          itemName="Interview"
          onConfirm={onDelete}
          onClose={() => setOpen(false)}
        />
      </>
    );
  }
  render(
    <I18nextProvider i18n={i18n}>
      <Example />
    </I18nextProvider>,
  );
  const trigger = screen.getByRole("button", { name: "Delete recording" });
  await actor.click(trigger);
  expect(screen.getByRole("alertdialog")).toHaveAccessibleDescription(
    /Interview.*permanently deleted/,
  );
  const cancel = screen.getByRole("button", { name: "Cancel" });
  expect(cancel).toHaveFocus();
  await actor.keyboard("{Enter}");
  expect(onDelete).not.toHaveBeenCalled();
  await waitFor(() => expect(trigger).toHaveFocus());
  expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
});
