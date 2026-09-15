import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

export function Markdown({ text }: { text: string }) {
  return <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{ img: () => <span>[图片请在电脑查看]</span>, a: ({ children }) => <span className="link-text">{children}</span> }}>{text}</ReactMarkdown></div>;
}
