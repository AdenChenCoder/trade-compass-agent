import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import { ChevronRight, Sparkles } from "lucide-react";
import { fetchAgentSkills } from "@/lib/agent-api";
import type { AgentSkill } from "@/lib/types";
import { cn } from "@/lib/utils";

export interface SkillSlashMenuHandle {
  handleKey(e: React.KeyboardEvent<HTMLTextAreaElement>): boolean;
}

interface Props {
  input: string;
  onSelect(skillName: string): void;
}

export const SkillSlashMenu = forwardRef<SkillSlashMenuHandle, Props>(
  function SkillSlashMenu({ input, onSelect }, ref) {
    const [allSkills, setAllSkills] = useState<AgentSkill[]>([]);
    const [selected, setSelected] = useState(0);
    const listRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
      fetchAgentSkills()
        .then((r) => setAllSkills(r.skills ?? []))
        .catch(() => {});
    }, []);

    const slashActive = input.startsWith("/") && !input.includes("\n");
    const query = slashActive ? input.slice(1).toLowerCase() : "";

    const filtered = slashActive
      ? allSkills.filter(
          (s) =>
            s.name.toLowerCase().includes(query) ||
            (s.description ?? "").toLowerCase().includes(query),
        )
      : [];

    const visible = slashActive && filtered.length > 0;

    useEffect(() => {
      setSelected(0);
    }, [query]);

    useEffect(() => {
      if (!visible || !listRef.current) return;
      const active = listRef.current.querySelector("[data-active='true']");
      active?.scrollIntoView({ block: "nearest" });
    }, [selected, visible]);

    const apply = useCallback(
      (skill: AgentSkill) => {
        onSelect(skill.name);
      },
      [onSelect],
    );

    useImperativeHandle(
      ref,
      () => ({
        handleKey: (e) => {
          if (!visible) return false;

          switch (e.key) {
            case "ArrowDown":
              e.preventDefault();
              setSelected((s) => (s + 1) % filtered.length);
              return true;

            case "ArrowUp":
              e.preventDefault();
              setSelected((s) => (s - 1 + filtered.length) % filtered.length);
              return true;

            case "Tab":
            case "Enter": {
              e.preventDefault();
              const item = filtered[selected];
              if (item) apply(item);
              return true;
            }

            case "Escape":
              e.preventDefault();
              onSelect("");
              return true;

            default:
              return false;
          }
        },
      }),
      [visible, filtered, selected, apply, onSelect],
    );

    if (!visible) return null;

    return (
      <div
        ref={listRef}
        className="absolute bottom-full left-0 right-0 z-50 mb-1 max-h-64 overflow-y-auto rounded-lg border bg-popover shadow-lg"
        role="listbox"
      >
        <div className="px-3 py-1.5 text-[0.65rem] font-medium uppercase tracking-wider text-muted-foreground/60">
          技能 — 输入筛选，↑↓ 选择，Enter 确认
        </div>
        {filtered.map((skill, i) => {
          const active = i === selected;
          return (
            <div
              key={skill.name}
              role="option"
              aria-selected={active}
              data-active={active}
              className={cn(
                "flex cursor-pointer items-center gap-2 px-3 py-2 text-sm",
                active
                  ? "bg-accent text-accent-foreground"
                  : "hover:bg-muted/50",
              )}
              onMouseEnter={() => setSelected(i)}
              onClick={() => apply(skill)}
            >
              <ChevronRight
                className={cn(
                  "h-3 w-3 shrink-0 transition-colors",
                  active ? "text-primary" : "text-transparent",
                )}
              />
              <Sparkles className="h-3.5 w-3.5 shrink-0 text-amber-500/70" />
              <span className="font-mono text-xs font-medium">{skill.name}</span>
              {skill.description && (
                <span className="ml-1 truncate text-xs text-muted-foreground">
                  {skill.description}
                </span>
              )}
              {skill.source === "project" && (
                <span className="ml-auto shrink-0 rounded bg-blue-500/10 px-1.5 py-0.5 text-[0.6rem] text-blue-600 dark:text-blue-400">
                  内置
                </span>
              )}
            </div>
          );
        })}
      </div>
    );
  },
);
