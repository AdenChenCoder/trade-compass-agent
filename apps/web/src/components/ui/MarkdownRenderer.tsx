import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useId } from "react";

export function MarkdownRenderer({ children }: { children: string }) {
  const prefix = useId();
  return <ReactMarkdown remarkPlugins={[remarkGfm]}
    remarkRehypeOptions={{ clobberPrefix: prefix, footnoteLabel: "数据来源", footnoteBackLabel: "返回正文引用" }}>
    {children}
  </ReactMarkdown>;
}
