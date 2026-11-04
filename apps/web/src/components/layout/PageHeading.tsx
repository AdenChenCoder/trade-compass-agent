import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";

export function PageHeading({ title, description, actions, icon: Icon }: { title: string; description?: ReactNode; actions?: ReactNode; icon?: LucideIcon }) {
  return (
    <header className="page-heading">
      <div className="page-heading-main min-w-0">
        {Icon ? <span className="page-symbol"><Icon size={24} strokeWidth={1.6} aria-hidden="true" /></span> : null}
        <div>
        <h1>{title}</h1>
        {description ? <p className="page-description">{description}</p> : null}
        </div>
      </div>
      {actions ? <div className="page-actions">{actions}</div> : null}
    </header>
  );
}
