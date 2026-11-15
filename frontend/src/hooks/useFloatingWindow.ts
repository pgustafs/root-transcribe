/**
 * A small always-on-top window that belongs to this page (Document
 * Picture-in-Picture, Chrome and Edge on desktop). It stays above other tabs,
 * so a control can be reached while another tab is being watched.
 *
 * The window is empty: the caller renders into `container` with a portal, so
 * the content stays part of this page's React tree.
 */

import { useCallback, useEffect, useRef, useState } from "react";

type PipHost = {
  requestWindow: (options?: {
    width?: number;
    height?: number;
  }) => Promise<Window>;
};

function host(): PipHost | null {
  if (typeof window === "undefined") return null;
  const pip = (window as unknown as { documentPictureInPicture?: PipHost })
    .documentPictureInPicture;
  return pip ?? null;
}

export function isFloatingWindowSupported(): boolean {
  return host() !== null;
}

/** Give the new window this page's styles, theme and language. */
function dress(target: Document, title: string): () => void {
  for (const sheet of Array.from(document.styleSheets)) {
    if (sheet.href) {
      // Linked sheets keep their own address, so fonts in them still load.
      const link = target.createElement("link");
      link.rel = "stylesheet";
      link.href = sheet.href;
      target.head.appendChild(link);
      continue;
    }
    try {
      const style = target.createElement("style");
      style.textContent = Array.from(sheet.cssRules)
        .map((rule) => rule.cssText)
        .join("\n");
      target.head.appendChild(style);
    } catch {
      // A sheet we may not read: the window works without it.
    }
  }
  const root = document.documentElement;
  const sync = () => {
    target.documentElement.className = root.className;
    target.documentElement.lang = root.lang;
    target.documentElement.style.colorScheme = root.style.colorScheme;
  };
  sync();
  target.title = title;
  target.body.style.margin = "0";
  const observer = new MutationObserver(sync);
  observer.observe(root, {
    attributes: true,
    attributeFilter: ["class", "lang", "style"],
  });
  return () => observer.disconnect();
}

export type FloatingWindow = {
  supported: boolean;
  isOpen: boolean;
  /** Where to render (with a portal) while the window is open. */
  container: HTMLElement | null;
  /** Must be called from a click or key press; the browser refuses otherwise. */
  open: () => Promise<boolean>;
  close: () => void;
};

export function useFloatingWindow(options: {
  title: string;
  width: number;
  height: number;
}): FloatingWindow {
  const { title, width, height } = options;
  const [container, setContainer] = useState<HTMLElement | null>(null);
  const windowRef = useRef<Window | null>(null);
  const undressRef = useRef<(() => void) | null>(null);
  const openingRef = useRef(false);

  const forget = useCallback(() => {
    undressRef.current?.();
    undressRef.current = null;
    windowRef.current = null;
    setContainer(null);
  }, []);

  const close = useCallback(() => {
    const current = windowRef.current;
    forget();
    try {
      current?.close();
    } catch {
      // Already gone.
    }
  }, [forget]);

  const open = useCallback(async () => {
    const pip = host();
    if (!pip) return false;
    if (windowRef.current) return true;
    if (openingRef.current) return false;
    openingRef.current = true;
    try {
      const opened = await pip.requestWindow({ width, height });
      undressRef.current = dress(opened.document, title);
      const mount = opened.document.createElement("div");
      opened.document.body.appendChild(mount);
      // The learner closed it with the window's own button.
      opened.addEventListener("pagehide", () => {
        if (windowRef.current === opened) forget();
      });
      windowRef.current = opened;
      setContainer(mount);
      return true;
    } catch {
      // No click behind the call, or the browser refused.
      return false;
    } finally {
      openingRef.current = false;
    }
  }, [forget, height, title, width]);

  useEffect(() => {
    if (windowRef.current) windowRef.current.document.title = title;
  }, [title]);

  // This page is going away: the window has nothing to control any more.
  useEffect(() => close, [close]);

  return {
    supported: isFloatingWindowSupported(),
    isOpen: container !== null,
    container,
    open,
    close,
  };
}
