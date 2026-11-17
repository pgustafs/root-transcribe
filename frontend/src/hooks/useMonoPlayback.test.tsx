import { render } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, expect, it, vi } from "vitest";

import { useMonoPlayback } from "./useMonoPlayback";

type FakeNode = {
  channelCount: number;
  channelCountMode: string;
  connect: ReturnType<typeof vi.fn>;
};
let contexts: {
  fold: FakeNode;
  source: FakeNode;
  resume: ReturnType<typeof vi.fn>;
}[];

function installAudioContext() {
  contexts = [];
  vi.stubGlobal(
    "AudioContext",
    class {
      destination = {};
      resume = vi.fn();
      private nodes = {
        fold: { channelCount: 2, channelCountMode: "max", connect: vi.fn() },
        source: { channelCount: 2, channelCountMode: "max", connect: vi.fn() },
      };
      constructor() {
        contexts.push({ ...this.nodes, resume: this.resume });
      }
      createGain = () => this.nodes.fold;
      createMediaElementSource = () => this.nodes.source;
    },
  );
}

function Player({ fold, url }: { fold: boolean; url: string }) {
  const ref = useRef<HTMLAudioElement>(null);
  useMonoPlayback(ref, fold, url);
  return <audio ref={ref} src={url} data-testid="player" />;
}

afterEach(() => vi.unstubAllGlobals());

it("folds an online meeting's two channels into both ears, from the first play", () => {
  installAudioContext();
  const { getByTestId, rerender } = render(<Player fold={true} url="/a" />);
  const audio = getByTestId("player");
  // Nothing is touched before the user plays: browsers require the gesture.
  expect(contexts).toHaveLength(0);

  audio.dispatchEvent(new Event("play"));
  expect(contexts).toHaveLength(1);
  const [{ fold, source, resume }] = contexts;
  expect(source.connect).toHaveBeenCalledWith(fold);
  expect([fold.channelCount, fold.channelCountMode]).toEqual([1, "explicit"]);
  expect(resume).toHaveBeenCalled();

  // Playing again does not route the element a second time.
  audio.dispatchEvent(new Event("play"));
  expect(contexts).toHaveLength(1);

  // The same player gets an ordinary recording: its channels pass through.
  rerender(<Player fold={false} url="/b" />);
  expect([fold.channelCount, fold.channelCountMode]).toEqual([2, "max"]);
  rerender(<Player fold={true} url="/c" />);
  expect([fold.channelCount, fold.channelCountMode]).toEqual([1, "explicit"]);
});

it("leaves an ordinary recording's player alone", () => {
  installAudioContext();
  const { getByTestId } = render(<Player fold={false} url="/a" />);
  getByTestId("player").dispatchEvent(new Event("play"));
  expect(contexts).toHaveLength(0);
});

it("plays as it is where Web Audio is missing", () => {
  vi.stubGlobal("AudioContext", undefined);
  const { getByTestId } = render(<Player fold={true} url="/a" />);
  expect(() =>
    getByTestId("player").dispatchEvent(new Event("play")),
  ).not.toThrow();
});
