import { describe, expect, it } from "vitest";

import {
  BROWSER_FAMILIES,
  MEDIA_CODECS,
  MEDIA_CONTAINERS,
  browserInfo,
  mediaTokens,
} from "./clientDiagnostics";

describe("browserInfo", () => {
  it.each([
    [
      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
      "Chrome",
      126,
    ],
    [
      "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
      "Firefox",
      128,
    ],
    [
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
      "Safari",
      17,
    ],
    [
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
      "Edge",
      126,
    ],
  ])("summarises %s", (ua, family, major) => {
    expect(browserInfo(ua)).toEqual({ family, major });
  });

  it("degrades to the 'unknown' family rather than throwing", () => {
    expect(browserInfo(undefined)).toEqual({ family: "unknown", major: null });
    expect(browserInfo("some custom agent")).toEqual({
      family: "unknown",
      major: null,
    });
  });

  it("only ever returns a family the backend accepts", () => {
    // The server writes this to its operator log, so an arbitrary string here
    // would be a log-injection channel (issue #37 review round 5).
    const agents = [
      "Chrome/126.0.0.0",
      "Firefox/128.0",
      "token=secret\nforged=true Chrome/1.0",
      "",
      "/tmp/customer",
    ];
    for (const ua of agents) {
      const { family, major } = browserInfo(ua);
      expect(BROWSER_FAMILIES).toContain(family);
      expect(major === null || Number.isInteger(major)).toBe(true);
    }
  });

  it("drops an implausible major version instead of forwarding it", () => {
    // The backend bounds this to 0..999; a bogus value must not get the whole
    // diagnostic rejected.
    expect(browserInfo("Chrome/123456").major).toBeNull();
  });

  it("keeps only family + major (never the raw user agent)", () => {
    const ua =
      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0.6478.126 Safari/537.36";
    const info = browserInfo(ua);
    expect(info).toEqual({ family: "Chrome", major: 126 });
    expect(JSON.stringify(info)).not.toContain("Linux");
    expect(JSON.stringify(info)).not.toContain("537.36");
  });
});

describe("mediaTokens", () => {
  it("normalises real recorder MIMEs to closed tokens", () => {
    expect(mediaTokens("audio/webm;codecs=opus")).toEqual({
      container: "webm",
      codec: "opus",
    });
    expect(mediaTokens("audio/ogg;codecs=vorbis")).toEqual({
      container: "ogg",
      codec: "vorbis",
    });
    expect(mediaTokens("audio/mp4;codecs=mp4a.40.2")).toEqual({
      container: "mp4",
      codec: "aac",
    });
  });

  it("falls back to the filename when the MIME is missing or generic", () => {
    expect(mediaTokens("", "meeting.webm").container).toBe("webm");
    expect(mediaTokens("application/octet-stream", "x.m4a").container).toBe(
      "mp4",
    );
  });

  it("NEVER passes caller-chosen text through", () => {
    // A MIME-shaped string is still arbitrary text — `secret/topsecret` and a
    // parameter-smuggled token both validated under the old regex contract
    // (issue #37 review round 6). Both must normalise to "unknown".
    for (const hostile of [
      "secret/topsecret",
      "audio/webm;token=secret",
      "/tmp/customer/recording",
      "https://evil.example/?t=secret",
      "audio/webm\nforged=true",
    ]) {
      const { container, codec } = mediaTokens(hostile);
      expect(MEDIA_CONTAINERS).toContain(container);
      expect(MEDIA_CODECS).toContain(codec);
      expect(JSON.stringify({ container, codec })).not.toContain("secret");
      expect(JSON.stringify({ container, codec })).not.toContain("forged");
    }
    // The parameter-smuggling case still reports its real container...
    expect(mediaTokens("audio/webm;token=secret").container).toBe("webm");
    // ...and an invented type is simply unknown.
    expect(mediaTokens("secret/topsecret")).toEqual({
      container: "unknown",
      codec: "unknown",
    });
  });
});
