/**
 * The connection a recording browser keeps open to hear shortcut presses.
 *
 * Server-sent events read with `fetch`, because the request needs the
 * signed-in bearer token (EventSource cannot send one). Network events keep
 * arriving while this page is in a background tab. The stream reconnects by
 * itself; a press that arrives while it is down is reported to the Mac as
 * "recorder not connected", never silently lost.
 */

import { apiClient } from "@/api/client";
import { getAccessToken } from "@/auth/keycloak";

export type StudyCommand = {
  invocation: string;
  action: "toggle";
  /** Server clock when the press arrived. */
  sent_ms: number;
  stale_after_ms: number;
};

export type CommandStreamHandlers = {
  onCommand: (command: StudyCommand, ageMs: number) => void;
  onState?: (connected: boolean) => void;
};

/** Split a text buffer into complete SSE events; returns the unfinished rest. */
export function parseEvents(buffer: string): {
  events: { event: string; data: string }[];
  rest: string;
} {
  const events: { event: string; data: string }[] = [];
  const blocks = buffer.replace(/\r\n/g, "\n").split("\n\n");
  const rest = blocks.pop() ?? "";
  for (const block of blocks) {
    let event = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith(":")) continue; // heartbeat
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (data.length > 0) events.push({ event, data: data.join("\n") });
  }
  return { events, rest };
}

const RETRY_MS = [500, 1000, 2000, 5000];

export function openCommandStream(
  sessionId: string,
  handlers: CommandStreamHandlers,
): () => void {
  const abort = new AbortController();
  let attempt = 0;
  // server clock minus this browser's clock, learnt from the hello event.
  let clockOffset = 0;

  const run = async () => {
    while (!abort.signal.aborted) {
      try {
        const token = await getAccessToken();
        const prefix = apiClient.defaults.baseURL ?? "/api/v1";
        const response = await fetch(
          `${prefix}/study/commands/stream?session_id=${encodeURIComponent(sessionId)}`,
          {
            headers: {
              Accept: "text/event-stream",
              ...(token ? { Authorization: `Bearer ${token}` } : {}),
            },
            signal: abort.signal,
            cache: "no-store",
          },
        );
        if (!response.ok || !response.body) {
          // 404: the server does not know the session yet; it is created
          // moments after the recording starts, so keep trying.
          throw new Error(`stream ${response.status}`);
        }
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const parsed = parseEvents(buffer);
          buffer = parsed.rest;
          for (const { event, data } of parsed.events) {
            if (event === "hello") {
              const hello = JSON.parse(data) as { server_ms: number };
              clockOffset = hello.server_ms - Date.now();
              attempt = 0;
              handlers.onState?.(true);
            } else if (event === "command") {
              const command = JSON.parse(data) as StudyCommand;
              const age = Date.now() + clockOffset - command.sent_ms;
              handlers.onCommand(command, Math.max(0, age));
            }
          }
        }
      } catch {
        // Fall through to the retry below.
      }
      if (abort.signal.aborted) break;
      handlers.onState?.(false);
      const wait = RETRY_MS[Math.min(attempt, RETRY_MS.length - 1)];
      attempt += 1;
      await new Promise((resolve) => setTimeout(resolve, wait));
    }
  };
  void run();
  return () => {
    abort.abort();
    handlers.onState?.(false);
  };
}
