import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/utils";

/**
 * Formatted Markdown (notes written by agents, edited by people).
 *
 * Raw HTML in the text is NOT rendered (react-markdown's default), so model
 * output cannot inject markup. Links open in a new tab.
 */
export function Markdown({
  children,
  className,
}: {
  children: string;
  className?: string;
}) {
  return (
    <div className={cn("md-doc", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ node: _node, children: text, ...props }) => (
            <a {...props} target="_blank" rel="noreferrer noopener">
              {text}
            </a>
          ),
          // Check-list boxes show state; ticking is done in the editor.
          input: ({ node: _node, ...props }) => (
            <input {...props} disabled={true} readOnly={true} />
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
