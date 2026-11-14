import { expect, test } from "@playwright/test";

test("standalone sign-in is available", async ({ page }) => {
  await page.goto("/login");
  await expect(page).toHaveTitle(/Root Transcribe/);
  await expect(page.getByRole("button", { name: /log in/i })).toBeVisible();
});
