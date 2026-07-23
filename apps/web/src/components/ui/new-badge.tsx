import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { isNewContent } from "@/lib/freshness";
import { cn } from "@/lib/utils";

const FreshnessNowContext = createContext(Date.now());

export function FreshnessProvider({ children }: { children: ReactNode }) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  return (
    <FreshnessNowContext.Provider value={now}>
      {children}
    </FreshnessNowContext.Provider>
  );
}

export function NewBadge({
  createdAt,
  className,
}: {
  createdAt: string | null | undefined;
  className?: string;
}) {
  const now = useContext(FreshnessNowContext);
  if (!isNewContent(createdAt, now)) return null;

  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center rounded-full border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0 text-[9px] font-semibold leading-4 tracking-wider text-emerald-700 dark:text-emerald-300",
        className,
      )}
      title="24 小时内新增"
      aria-label="24 小时内新增"
    >
      NEW
    </span>
  );
}
