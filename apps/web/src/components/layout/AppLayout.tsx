import { useState } from "react";
import { motion } from "motion/react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { ArrowUpRight, BookOpen, Bot, Brain, Briefcase, ChevronRight, ClipboardList, Clock, FlaskConical, Menu, Settings, Sparkles, X } from "lucide-react";
import { cn } from "@/lib/utils";

const navigation = [
  { label: "工作空间", items: [
    { path: "/agent", label: "Agent 对话", icon: Bot },
    { path: "/portfolio", label: "模拟持仓", icon: Briefcase },
    { path: "/research", label: "策略研究", icon: FlaskConical },
  ] },
  { label: "知识与执行", items: [
    { path: "/memory", label: "记忆", icon: Brain },
    { path: "/rules", label: "规则", icon: BookOpen },
    { path: "/skills", label: "技能", icon: Sparkles },
    { path: "/jobs", label: "定时任务", icon: Clock },
    { path: "/audit", label: "审计回放", icon: ClipboardList },
  ] },
  { label: "偏好设置", items: [{ path: "/settings", label: "设置", icon: Settings }] },
];

export function AppLayout() {
  const { pathname } = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);
  const current = navigation.flatMap(group => group.items).find(item => item.path === pathname);

  return (
    <div className="workbench-shell">
      <a href="#workspace" className="skip-link">跳到主要内容</a>
      <aside className="workbench-rail">
        <NavLink to="/agent" className="workbench-brand" aria-label="交易罗盘首页" onClick={() => setMenuOpen(false)}>
          <span className="brand-mark" aria-hidden="true" />
          <span className="brand-wordmark"><strong>交易罗盘</strong><small>TRADE COMPASS</small></span>
        </NavLink>
        <button className="mobile-menu-toggle" type="button" aria-label={menuOpen ? "收起导航" : "展开导航"} aria-expanded={menuOpen} aria-controls="workbench-navigation" onClick={() => setMenuOpen(!menuOpen)}>
          {menuOpen ? <X size={20} /> : <Menu size={20} />}
        </button>
        <nav id="workbench-navigation" className={cn("workbench-navigation", menuOpen && "is-open")} aria-label="主导航">
          {navigation.map(group => (
            <div className="navigation-group" key={group.label}>
              <p className="navigation-label">{group.label}</p>
              {group.items.map(({ path, label, icon: Icon }) => (
                <NavLink key={path} to={path} title={label} onClick={() => setMenuOpen(false)} className={({ isActive }) => cn("workspace-link", isActive && "is-active")}>
                  {({ isActive }) => <>
                  {isActive ? <motion.span layoutId="workspace-navigation" className="navigation-selection" aria-hidden="true" transition={{ duration: .3, ease: [.22, 1, .36, 1] }} /> : null}
                  <Icon size={19} strokeWidth={1.7} aria-hidden />
                  <span>{label}</span>
                  <ArrowUpRight className="navigation-arrow" size={15} aria-hidden />
                  </>}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
      </aside>
      <div className="workbench-body">
        <header className="workbench-topbar">
          <div className="workspace-breadcrumb"><span>工作台</span><ChevronRight size={13} aria-hidden /><strong>{current?.label ?? "交易罗盘"}</strong></div>
          <span className="workbench-date">{new Intl.DateTimeFormat("zh-CN", { month: "long", day: "numeric", weekday: "long" }).format(new Date())}</span>
        </header>
        <main id="workspace" tabIndex={-1} className="workspace-content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
