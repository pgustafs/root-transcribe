/** Read-only visual audit of the deployed app. Screenshots stay on this host. */
import { chromium } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { readFile, mkdir, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = fileURLToPath(new URL("../../", import.meta.url));
const output = path.join(root, "frontend/test-results/visual-audit");
const base = process.env.E2E_BASE_URL ?? "http://localhost:8095";
const username = process.env.E2E_USERNAME ?? "admin@exampel.com";
let password = process.env.E2E_PASSWORD;
if (!password) {
  const secrets = JSON.parse(
    await readFile(path.join(root, "deploy/secrets.local.json"), "utf8"),
  );
  password = secrets.KEYCLOAK_APP_ADMIN_PASSWORD;
}
if (!password)
  throw new Error("Set E2E_PASSWORD to the application admin password.");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  viewport: { width: 1440, height: 1000 },
  reducedMotion: "reduce",
});
const page = await context.newPage();
const report = { pages: [], failures: [], playback: null };
let recordings = [];
page.on("response", async (response) => {
  if (/\/recordings(?:\?|$)/.test(response.url()) && response.ok()) {
    try {
      const value = await response.json();
      if (Array.isArray(value)) recordings = value;
    } catch {
      /* Non-JSON response. */
    }
  }
});
async function settle() {
  await page
    .waitForLoadState("networkidle", { timeout: 15000 })
    .catch(() => {});
  await page
    .locator('[aria-busy="true"]')
    .first()
    .waitFor({ state: "hidden", timeout: 10000 })
    .catch(() => {});
  await page.evaluate(() => document.fonts.ready);
}
async function capture(name, mode) {
  await settle();
  await page.evaluate(async () => {
    await Promise.all(
      document
        .getAnimations()
        .filter((a) => a.effect?.getComputedTiming().iterations !== Infinity)
        .map((a) => a.finished.catch(() => {})),
    );
  });
  const file = `${name}-${mode}.png`;
  await page.screenshot({ path: path.join(output, file), fullPage: true });
  const layout = await page.evaluate(() => ({
    width: innerWidth,
    overflow: document.documentElement.scrollWidth > innerWidth + 1,
    audio: [...document.querySelectorAll("audio")].map((el) => {
      const r = el.getBoundingClientRect();
      return {
        width: r.width,
        height: r.height,
        top: r.top,
        bottom: r.bottom,
        controls: el.controls,
        readyState: el.readyState,
        error: el.error?.code ?? null,
        display: getComputedStyle(el).display,
      };
    }),
  }));
  const pageScroll = page.locator("#main-content");
  let bottomFile;
  if (
    (await pageScroll.count()) &&
    (await pageScroll.evaluate((el) => el.scrollHeight > el.clientHeight + 20))
  ) {
    const top = await pageScroll.evaluate((el) => el.scrollTop);
    await pageScroll.evaluate((el) => {
      el.scrollTop = el.scrollHeight;
    });
    bottomFile = `${name}-${mode}-bottom.png`;
    await page.screenshot({
      path: path.join(output, bottomFile),
      fullPage: true,
    });
    await pageScroll.evaluate((el, position) => {
      el.scrollTop = position;
    }, top);
  }
  const a11y = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  report.pages.push({
    name,
    mode,
    file,
    bottomFile,
    ...layout,
    playbackVisible: layout.audio.length
      ? layout.audio.every(
          (a) =>
            a.width >= 200 &&
            a.height >= 48 &&
            a.display !== "none" &&
            a.top >= 0 &&
            a.bottom <= (mode === "mobile" ? 844 : 1000),
        )
      : null,
    violations: a11y.violations.map((v) => ({
      id: v.id,
      impact: v.impact,
      targets: v.nodes.map((n) => n.target),
    })),
  });
  console.log(
    `Captured ${name} (${mode}); ${a11y.violations.length} accessibility findings`,
  );
}
async function visit(name, url, setup) {
  try {
    for (const [mode, width, height, dark] of [
      ["desktop", 1440, 1000, false],
      ["dark", 1440, 1000, true],
      ["mobile", 390, 844, false],
    ]) {
      await page.setViewportSize({ width, height });
      await page.goto(new URL(url, base).href);
      await page.evaluate(
        (value) => {
          localStorage.setItem("root_transcribe.theme", value);
          window.dispatchEvent(new Event("root-transcribe-theme"));
        },
        dark ? "dark" : "light",
      );
      await settle();
      if (setup) await setup();
      await capture(name, mode);
    }
  } catch (error) {
    report.failures.push({ name, error: error.message.split("\n")[0] });
    console.log(`Could not capture ${name}; see report.json`);
  }
}
try {
  await visit("login", "/login");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(new URL("/login", base).href);
  await page.getByRole("button", { name: /log in|logga in/i }).click();
  await page.locator('input[name="username"]').fill(username);
  await page.locator('input[name="password"]').fill(password);
  await page
    .locator('input[type="submit"],button[type="submit"]')
    .first()
    .click();
  await page.waitForURL(
    (url) => url.origin === new URL(base).origin && url.pathname !== "/login",
    { timeout: 30000 },
  );
  await visit("workspaces", "/workspaces");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(new URL("/workspaces", base).href);
  await settle();
  const workspace = await page
    .locator('a[href^="/workspaces/"]')
    .first()
    .getAttribute("href")
    .catch(() => null);
  if (workspace) {
    await visit("recording", workspace, async () => {
      const playable = recordings.find(
        (r) =>
          !r.audio_deleted &&
          ["completed", "failed", "cancelled"].includes(r.status),
      );
      if (playable) {
        const choose = page.getByRole("button", {
          name: /choose recording|välj inspelning/i,
        });
        if (await choose.isVisible()) await choose.click();
        await page
          .getByRole("button", { name: playable.title, exact: true })
          .first()
          .click();
        await page
          .locator("audio")
          .waitFor({ state: "attached", timeout: 15000 })
          .catch(() => {});
        await settle();
        if (!report.playback && (await page.locator("audio").count())) {
          const audio = page.locator("audio").first();
          await audio.evaluate((el) => el.play());
          await page.waitForFunction(
            () => document.querySelector("audio")?.currentTime > 0.25,
          );
          const played = await audio.evaluate((el) => {
            el.pause();
            return el.currentTime;
          });
          await audio.evaluate((el) => {
            el.currentTime = 2;
          });
          await page.waitForFunction(() => {
            const el = document.querySelector("audio");
            return el && !el.seeking && el.currentTime >= 1.9;
          });
          report.playback = await audio.evaluate(
            (el, played) => ({
              playedSeconds: played,
              seekSeconds: el.currentTime,
              readyState: el.readyState,
              error: el.error?.code ?? null,
            }),
            played,
          );
          await audio.evaluate((el) => {
            el.currentTime = 0;
          });
        }
        await page
          .locator("audio")
          .first()
          .scrollIntoViewIfNeeded()
          .catch(() => {});
      }
    });
  } else
    report.failures.push({
      name: "recording",
      error: "No existing workspace was available.",
    });
  if (workspace) {
    for (const [name, tab] of [
      ["clips", "Clips"],
      ["sensitive-content", "Sensitive content"],
    ]) {
      await visit(name, workspace, async () => {
        await page.getByRole("tab", { name: tab, exact: true }).click();
        await page.waitForFunction(
          (label) =>
            [...document.querySelectorAll('[role="tab"]')].some(
              (el) =>
                el.textContent === label &&
                el.getAttribute("aria-selected") === "true",
            ),
          tab,
        );
      });
    }
    await visit("recording-actions", workspace, async () => {
      await page
        .getByRole("button", {
          name: "Review, comments & privacy",
          exact: true,
        })
        .click();
    });
  }
  for (const [name, url] of [
    ["speakers", "/admin/speakers"],
    ["users", "/users"],
    ["jobs", "/admin/jobs"],
    ["profile", "/profile"],
    ["forbidden", "/forbidden"],
    ["not-found", "/not-a-page"],
  ])
    await visit(name, url);
} catch (error) {
  await page
    .screenshot({ path: path.join(output, "audit-error.png"), fullPage: true })
    .catch(() => {});
  report.failures.push({
    name: "audit",
    error: error.message.split("\n")[0],
    pagePath: new URL(page.url()).pathname,
  });
  console.error(
    "Audit could not finish; inspect audit-error.png and report.json.",
  );
  process.exitCode = 1;
} finally {
  if (
    report.failures.length ||
    report.pages.some(
      (p) => p.overflow || p.playbackVisible === false || p.violations.length,
    )
  )
    process.exitCode = 1;
  await writeFile(
    path.join(output, "report.json"),
    JSON.stringify(report, null, 2) + "\n",
  );
  await browser.close();
  console.log(`Screenshots and report: ${output}`);
}
