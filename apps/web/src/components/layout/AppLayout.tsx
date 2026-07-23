import { NavLink, Outlet } from "react-router-dom";
import { BookOpen, Bot, Brain, Briefcase, ClipboardList, Clock, Settings, Sparkles } from "lucide-react";
import { cn } from "@/lib/utils";

const navLinkClass = ({ isActive }: { isActive: boolean }) =>
  cn(
    "inline-flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium transition-colors",
    isActive
      ? "bg-primary text-primary-foreground"
      : "text-muted-foreground hover:bg-muted hover:text-foreground",
  );

export function AppLayout() {
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <header className="shrink-0 border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/80">
        <div className="container flex h-14 items-center justify-between gap-4">
          <div className="flex items-center gap-2 font-semibold tracking-tight">
            <Bot className="h-5 w-5" aria-hidden />
            <span>交易罗盘</span>
          </div>
          <nav className="flex flex-wrap items-center gap-1">
            <NavLink to="/agent" className={navLinkClass}>
              <Bot className="h-4 w-4" aria-hidden />
              Agent
            </NavLink>
            <NavLink to="/portfolio" className={navLinkClass}>
              <Briefcase className="h-4 w-4" aria-hidden />
              持仓
            </NavLink>
            <NavLink to="/memory" className={navLinkClass}>
              <Brain className="h-4 w-4" aria-hidden />
              记忆
            </NavLink>
            <NavLink to="/audit" className={navLinkClass}>
              <ClipboardList className="h-4 w-4" aria-hidden />
              审计
            </NavLink>
            <NavLink to="/rules" className={navLinkClass}>
              <BookOpen className="h-4 w-4" aria-hidden />
              规则
            </NavLink>
            <NavLink to="/skills" className={navLinkClass}>
              <Sparkles className="h-4 w-4" aria-hidden />
              技能
            </NavLink>
            <NavLink to="/jobs" className={navLinkClass}>
              <Clock className="h-4 w-4" aria-hidden />
              任务
            </NavLink>
            <NavLink to="/settings" className={navLinkClass}>
              <Settings className="h-4 w-4" aria-hidden />
              设置
            </NavLink>
          </nav>
        </div>
      </header>
      <main className="flex min-h-0 flex-1 flex-col overflow-hidden">
        <Outlet />
      </main>
    </div>
  );
}
