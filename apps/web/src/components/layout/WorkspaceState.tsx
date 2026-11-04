import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { AlertCircle } from "lucide-react";
import { Button } from "@/components/ui/button";

export function EmptyState({ icon: Icon, title, description, action }: {
  icon: LucideIcon; title: string; description?: string; action?: ReactNode;
}) {
  return <div className="workspace-empty">
    <span className="empty-symbol"><Icon size={24} strokeWidth={1.5} aria-hidden="true" /></span>
    <h3>{title}</h3>
    {description ? <p>{description}</p> : null}
    {action}
  </div>;
}

export function QueryNotice({ title, error, retry }: { title: string; error: unknown; retry: () => void }) {
  return <div className="query-notice" role="alert">
    <AlertCircle size={19} aria-hidden="true" />
    <div><strong>{title}</strong><p>{error instanceof Error ? error.message : "请稍后重试。"}</p></div>
    <Button variant="outline" size="sm" onClick={retry}>重试</Button>
  </div>;
}

export function SectionNav({ items, label }: { items: { id: string; label: string }[]; label: string }) {
  return <nav className="section-navigation" aria-label={label}>
    {items.map(item => <a key={item.id} href={`#${item.id}`}>{item.label}</a>)}
  </nav>;
}
