import { useQuery } from "@tanstack/react-query";
import { ChevronDown, Folder, Plug, Server, Sparkles } from "lucide-react";
import { QueryNotice } from "@/components/layout/WorkspaceState";
import { Skeleton } from "@/components/ui/skeleton";
import { fetchAgentMcp, fetchAgentSkills } from "@/lib/agent-api";

const serverStates: Record<string, { label: string; tone: string }> = {
  connected: { label: "已连接", tone: "positive" },
  ok: { label: "正常", tone: "positive" },
  configured: { label: "已配置", tone: "neutral" },
  unavailable: { label: "不可用", tone: "error" },
  error: { label: "异常", tone: "error" },
};

function ExtensionState({ label, tone }: { label: string; tone: string }) {
  return <span className="extension-state" data-tone={tone}><i aria-hidden="true" />{label}</span>;
}

function ExtensionLoading({ label }: { label: string }) {
  return <div className="extension-loading" role="status" aria-label={label}>
    <Skeleton className="h-4 w-48" /><Skeleton className="h-3 w-3/4" /><Skeleton className="h-3 w-1/2" />
  </div>;
}

export function AgentExtensions() {
  const skillsQuery = useQuery({ queryKey: ["agent-skills"], queryFn: fetchAgentSkills });
  const mcpQuery = useQuery({ queryKey: ["agent-mcp"], queryFn: fetchAgentMcp });
  const skills = skillsQuery.data?.skills ?? [];
  const servers = mcpQuery.data?.servers ?? [];

  return <div className="extension-directory">
    <section className="extension-panel" aria-labelledby="available-skills-title">
      <header className="extension-panel-heading">
        <span className="extension-section-icon" aria-hidden="true"><Sparkles size={20} strokeWidth={1.6} /></span>
        <h2 id="available-skills-title">可用技能</h2>
        {skillsQuery.isSuccess ? <span className="extension-count">{skills.length} 项</span> : null}
      </header>
      {skillsQuery.isPending ? <ExtensionLoading label="正在加载技能" /> : skillsQuery.error ? <QueryNotice title="无法加载技能" error={skillsQuery.error} retry={() => void skillsQuery.refetch()} /> : skills.length === 0 ? <div className="extension-empty"><Sparkles size={20} strokeWidth={1.4} aria-hidden="true" /><p>暂无已加载的技能</p></div> : <ul className="extension-skill-list">
        {skills.map(skill => <li key={skill.name} className="extension-skill">
          <div className="extension-skill-heading">
            <h3>{skill.name}</h3>
            <ExtensionState label={skill.enabled !== false ? "已启用" : "未启用"} tone={skill.enabled !== false ? "positive" : "neutral"} />
          </div>
          <p className="extension-description">{skill.description ?? "—"}</p>
          <span className="extension-source" data-wide={(skill.source ?? skill.path ?? "").length > 24}><Folder size={12} aria-hidden="true" /><span><span className="sr-only">来源：</span>{skill.source === "project" ? "项目技能" : skill.source ?? skill.path ?? "—"}</span></span>
        </li>)}
      </ul>}
    </section>

    <section className="extension-panel extension-servers" aria-labelledby="external-tools-title">
      <header className="extension-panel-heading">
        <span className="extension-section-icon" aria-hidden="true"><Server size={20} strokeWidth={1.6} /></span>
        <h2 id="external-tools-title">外部工具</h2>
        {mcpQuery.isSuccess && servers.length > 0 ? <span className="extension-count">{servers.length} 个服务</span> : null}
      </header>
      {mcpQuery.isPending ? <ExtensionLoading label="正在加载外部工具" /> : mcpQuery.error ? <QueryNotice title="无法加载外部工具" error={mcpQuery.error} retry={() => void mcpQuery.refetch()} /> : servers.length === 0 ? <div className="extension-empty"><Plug size={22} strokeWidth={1.4} aria-hidden="true" /><p>暂未配置外部工具服务</p></div> : <ul className="extension-server-list">
        {servers.map(server => {
          const state = serverStates[server.status] ?? { label: server.status, tone: "neutral" };
          return <li key={server.name} className="extension-server">
            <div className="extension-skill-heading"><h3>{server.name}</h3><ExtensionState {...state} /></div>
            {server.command ? <dl className="extension-command"><dt>连接信息</dt><dd><code>{server.command}</code></dd></dl> : null}
            {server.tools && server.tools.length > 0 ? <details className="extension-tool-details"><summary><ChevronDown size={13} aria-hidden="true" />{server.tools.length} 个工具</summary><ul>{server.tools.map(tool => <li key={tool}><code>{tool}</code></li>)}</ul></details> : server.status === "connected" ? <p className="extension-zero-tools">0 个工具</p> : null}
            {server.error ? <p className="extension-error" role="alert">{server.error}</p> : null}
          </li>;
        })}
      </ul>}
    </section>
  </div>;
}
