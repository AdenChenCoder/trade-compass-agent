import { useId, useRef } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

export function Markdown({ text, interactive = true }: { text: string; interactive?: boolean }) {
  const root = useRef<HTMLDivElement>(null);
  const prefix = useId();
  return <div className="markdown" ref={root} onClick={event => {
    const link = (event.target as Element).closest<HTMLAnchorElement>('a[data-footnote-ref], a[data-footnote-backref]');
    if (!link || !root.current) return;
    // Resolve within this message, never navigate the PWA or another report.
    const id = link.hash.slice(1);
    const target = Array.from(root.current.querySelectorAll<HTMLElement>('[id]')).find(element => element.id === id);
    if (!target) return;
    event.preventDefault();
    const details = target.closest('details');
    if (details) details.open = true;
    target.tabIndex = -1;
    target.focus({ preventScroll: true });
    target.scrollIntoView({ block: 'nearest' });
  }}>
    <ReactMarkdown remarkPlugins={[remarkGfm]}
      remarkRehypeOptions={{ clobberPrefix: prefix, footnoteLabel: '数据来源', footnoteBackLabel: '返回正文引用' }}
      components={{
        img: () => <span>[图片请在电脑查看]</span>,
        a: ({ node, children, ...props }) => interactive && (node?.properties.dataFootnoteRef !== undefined || node?.properties.dataFootnoteBackref !== undefined)
          ? <a {...props}>{children}</a> : <span className="link-text">{children}</span>,
        section: ({ node, children, ...props }) => node?.properties.dataFootnotes !== undefined
          ? interactive
            ? <details className="report-sources"><summary>数据来源</summary>{children}</details>
            : <div hidden>{children}</div>
          : <section {...props}>{children}</section>,
      }}>{text}</ReactMarkdown>
  </div>;
}
