import { useState } from "react";
import { motion, useReducedMotion } from "motion/react";
import { ArrowUpRight, ChartNoAxesCombined, Search, Wallet } from "lucide-react";

const prompts = [
  { label: "看市场", text: "今天大盘资金情况", icon: ChartNoAxesCombined, angle: -38 },
  { label: "研究个股", text: "600519 短线怎么看", icon: Search, angle: 25 },
  { label: "回顾持仓", text: "帮我分析当前持仓的风险", icon: Wallet, angle: 78 },
];

export function AgentWelcome({ onPrompt, showPrompts = true }: { onPrompt: (prompt: string) => void; showPrompts?: boolean }) {
  const [direction, setDirection] = useState(-38);
  const reduceMotion = useReducedMotion();
  return (
    <section className="agent-welcome" aria-label="开始对话">
      <div className="welcome-intro">
        <div className="welcome-copy"><div className="welcome-brand"><span className="brand-mark" aria-hidden="true" /></div><h1>今天，<br /><span>研究什么？</span></h1></div>
        <div className="welcome-compass" aria-hidden="true">
          <svg viewBox="0 0 240 240" fill="none"><circle cx="120" cy="120" r="108" /><circle cx="120" cy="120" r="88" strokeDasharray="1 10.5" strokeWidth="6" /><path d="M120 2v21M120 217v21M2 120h21M217 120h21" /><circle cx="120" cy="120" r="50" /></svg>
          <motion.span className="compass-needle" animate={{ rotate: direction }} transition={{ duration: reduceMotion ? 0 : .42, ease: [.22, 1, .36, 1] }}><svg viewBox="0 0 120 160" fill="none"><path d="M60 12L88 90L60 78L32 90Z" fill="currentColor" /><path d="M60 148L32 70L60 82L88 70Z" fill="currentColor" opacity=".24" /><circle cx="60" cy="80" r="5" fill="currentColor" /></svg></motion.span>
        </div>
      </div>
      {showPrompts ? <div className="welcome-prompts">
        {prompts.map(({ label, text, icon: Icon, angle }) => (
          <motion.button type="button" key={label} onPointerEnter={() => setDirection(angle)} onFocus={() => setDirection(angle)} whileHover={reduceMotion ? undefined : { y: -4 }} whileTap={reduceMotion ? undefined : { scale: .98 }} onClick={() => onPrompt(text)}>
            <span className="prompt-symbol"><Icon size={25} strokeWidth={1.5} aria-hidden /></span><span className="prompt-copy"><strong>{label}</strong><small>{text}</small></span><ArrowUpRight size={17} className="prompt-arrow" aria-hidden />
          </motion.button>
        ))}
      </div> : null}
    </section>
  );
}
