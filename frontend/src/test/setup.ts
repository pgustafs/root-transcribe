import "fake-indexeddb/auto";
/** Global Vitest setup: jest-dom matchers, MSW lifecycle, and jsdom polyfills. */

import "@testing-library/jest-dom/vitest";

import { cleanup, configure } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, vi } from "vitest";

import { resetIdentityMock } from "@/test/identity";

vi.mock(
  "@/auth/keycloak",
  async () => (await import("@/test/identity")).identityMock,
);

import { resetStore } from "@/test/msw/handlers";
import { server } from "@/test/msw/server";

// Testing Library waits 1s by default for findBy*. That is tight for the heavy
// pages once coverage instrumentation is in play on a shared CI runner, where a
// first render can cross the line and fail a passing test. This raises only the
// ceiling: queries still resolve the moment the element appears.
configure({ asyncUtilTimeout: 5000 });

// jsdom lacks these APIs that Radix components rely on.
if (!window.matchMedia) {
  window.matchMedia = (query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }) as unknown as MediaQueryList;
}

// jsdom's Blob implements arrayBuffer()/text() but not stream(). MSW's XHR
// interceptor (msw >= 2.15) rebuilds blob responses as fetch Responses, and
// undici's body extraction calls blob.stream() — without this the blob
// download/audio tests crash with "object.stream is not a function".
if (
  typeof Blob !== "undefined" &&
  typeof Blob.prototype.stream !== "function"
) {
  Blob.prototype.stream = function stream(this: Blob) {
    const buffer = this.arrayBuffer();
    return new ReadableStream<Uint8Array<ArrayBuffer>>({
      async start(controller) {
        controller.enqueue(new Uint8Array(await buffer));
        controller.close();
      },
    });
  };
}

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
window.ResizeObserver = window.ResizeObserver ?? ResizeObserverMock;

// jsdom does not lay out or scroll elements; transcript playback follows
// the active segment through these methods.
Element.prototype.scrollTo = Element.prototype.scrollTo ?? (() => {});
Element.prototype.scrollIntoView =
  Element.prototype.scrollIntoView ?? (() => {});

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));

afterEach(() => {
  cleanup();
  server.resetHandlers();
  resetStore();
  localStorage.clear();
  resetIdentityMock();
});

afterAll(() => server.close());
