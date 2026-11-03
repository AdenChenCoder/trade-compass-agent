import * as React from "react";
import { motion } from "motion/react";
import { cn } from "@/lib/utils";

interface TabsContextValue {
  value: string;
  onValueChange: (value: string) => void;
  id: string;
}
const TabsContext = React.createContext<TabsContextValue | null>(null);
function useTabs() {
  const ctx = React.useContext(TabsContext);
  if (!ctx) throw new Error("Tabs components must be used within <Tabs>");
  return ctx;
}

export function Tabs({ defaultValue, value: controlledValue, onValueChange, children, className }: { defaultValue: string; value?: string; onValueChange?: (value: string) => void; children: React.ReactNode; className?: string }) {
  const [localValue, setValue] = React.useState(defaultValue);
  const value = controlledValue ?? localValue;
  const id = React.useId();
  return <TabsContext.Provider value={{ value, onValueChange: onValueChange ?? setValue, id }}><div className={className}>{children}</div></TabsContext.Provider>;
}

export function TabsList({ children, className }: { children: React.ReactNode; className?: string }) {
  function handleKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const tabs = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]'));
    const current = tabs.indexOf(document.activeElement as HTMLButtonElement);
    if (current < 0) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    tabs[next]?.focus();
    tabs[next]?.click();
  }
  return <div role="tablist" onKeyDown={handleKeyDown} className={cn("ui-tabs text-muted-foreground", className)}>{children}</div>;
}

export function TabsTrigger({ value, children, className }: { value: string; children: React.ReactNode; className?: string }) {
  const { value: selected, onValueChange, id } = useTabs();
  return (
    <button type="button" role="tab" id={`${id}-tab-${value}`} aria-controls={`${id}-panel-${value}`} aria-selected={selected === value} tabIndex={selected === value ? 0 : -1}
      className={cn("ui-tab inline-flex items-center justify-center whitespace-nowrap font-medium hover:text-foreground", className)} onClick={() => onValueChange(value)}>
      {selected === value ? <motion.span className="tab-selection" layoutId={`${id}-selection`} aria-hidden="true" transition={{ duration: .28, ease: [.22, 1, .36, 1] }} /> : null}
      <span className="tab-label">{children}</span>
    </button>
  );
}

export function TabsContent({ value, children, className, forceMount = false }: { value: string; children: React.ReactNode; className?: string; forceMount?: boolean }) {
  const { value: selected, id } = useTabs();
  if (selected !== value && !forceMount) return null;
  return <motion.div initial={false} animate={selected === value ? { opacity: 1, y: 0 } : { opacity: 0, y: 8 }} transition={{ duration: .22, ease: [.22, 1, .36, 1] }} hidden={selected !== value} role="tabpanel" tabIndex={0} id={`${id}-panel-${value}`} aria-labelledby={`${id}-tab-${value}`} className={cn("ui-tab-panel", className)}>{children}</motion.div>;
}
