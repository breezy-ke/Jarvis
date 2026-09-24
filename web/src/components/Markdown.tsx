import ReactMarkdown, { type Components } from "react-markdown";

/**
 * Safe Markdown for assistant replies. Raw HTML is dropped, images are never
 * loaded (so a message can't phone home), and links only allow http(s)/mailto.
 */
const SAFE_URL = /^(https?:|mailto:)/i;

const components: Components = {
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer nofollow"
      className="font-medium text-primary underline underline-offset-2"
    >
      {children}
    </a>
  ),
  p: ({ children }) => <p className="leading-relaxed [&:not(:first-child)]:mt-2">{children}</p>,
  ul: ({ children }) => <ul className="mt-2 list-disc space-y-1 pl-5">{children}</ul>,
  ol: ({ children }) => <ol className="mt-2 list-decimal space-y-1 pl-5">{children}</ol>,
  h1: ({ children }) => <h3 className="mt-3 font-semibold">{children}</h3>,
  h2: ({ children }) => <h3 className="mt-3 font-semibold">{children}</h3>,
  h3: ({ children }) => <h4 className="mt-3 font-semibold">{children}</h4>,
  code: ({ children, className }) =>
    className ? (
      <code className={className}>{children}</code>
    ) : (
      <code className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em]">{children}</code>
    ),
  pre: ({ children }) => (
    <pre className="mt-2 overflow-x-auto rounded-md bg-muted p-3 font-mono text-xs">{children}</pre>
  ),
  blockquote: ({ children }) => (
    <blockquote className="mt-2 border-l-2 pl-3 text-muted-foreground">{children}</blockquote>
  ),
};

export function Markdown({ children }: { children: string }) {
  return (
    <ReactMarkdown
      skipHtml
      disallowedElements={["img"]}
      unwrapDisallowed
      urlTransform={(url) => (SAFE_URL.test(url) ? url : "")}
      components={components}
    >
      {children}
    </ReactMarkdown>
  );
}
