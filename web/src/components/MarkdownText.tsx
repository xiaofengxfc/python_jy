import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/** AI answers are Markdown. Render them instead of showing raw `**` and list
 * markers in a pre-wrapped blob; react-markdown escapes raw HTML by default. */
export default function MarkdownText({
  text,
  className = "ai-text",
}: {
  text: string;
  className?: string;
}) {
  return (
    <div className={className}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}
