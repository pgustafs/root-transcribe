/**
 * Play a recording whose two channels hold different sources in both ears.
 *
 * An online-meeting recording has the microphone on the left channel and the
 * meeting tab on the right (so the transcript knows who is where). Played as
 * it is, you would hear yourself in one ear and the call in the other. The
 * player's sound is therefore routed through a node that folds the channels
 * into one, which the browser then sends to both ears.
 *
 * The routing starts on the first "play" (browsers only let sound start from
 * a user's action) and is made once per audio element: an element can be
 * connected to Web Audio only once. For any other recording the node is
 * switched back to passing the channels through untouched.
 */

import { type RefObject, useEffect } from "react";

type Routing = { context: AudioContext; fold: GainNode };

const routed = new WeakMap<HTMLMediaElement, Routing>();

export function useMonoPlayback(
  audioRef: RefObject<HTMLAudioElement | null>,
  /** Fold the channels into one (an online-meeting recording). */
  foldChannels: boolean,
  /** Changes when the player gets another recording (or another element). */
  source: unknown,
) {
  useEffect(() => {
    const element = audioRef.current;
    if (!element) return;
    const apply = () => {
      let routing = routed.get(element);
      if (!routing) {
        // Leave the element alone until a recording needs folding.
        if (!foldChannels || typeof AudioContext === "undefined") return;
        try {
          const context = new AudioContext();
          const fold = context.createGain();
          context.createMediaElementSource(element).connect(fold);
          fold.connect(context.destination);
          routing = { context, fold };
          routed.set(element, routing);
        } catch {
          return; // plays as it is: both channels, one per ear
        }
      }
      routing.fold.channelCountMode = foldChannels ? "explicit" : "max";
      routing.fold.channelCount = foldChannels ? 1 : 2;
      void routing.context.resume();
    };
    element.addEventListener("play", apply);
    // Already routed (an earlier recording in this player): follow this one.
    if (routed.has(element)) apply();
    return () => element.removeEventListener("play", apply);
  }, [audioRef, foldChannels, source]);
}
