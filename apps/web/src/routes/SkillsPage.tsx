import { motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowUpRight, ChevronDown, FileCode2, FileText, Pin, PinOff, Search, Sparkles } from "lucide-react";
import { toast } from "sonner";
import { PageHeading } from "@/components/layout/PageHeading";
import { EmptyState, QueryNotice } from "@/components/layout/WorkspaceState";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { MarkdownRenderer } from "@/components/ui/MarkdownRenderer";
import { NewBadge } from "@/components/ui/new-badge";
import { Skeleton } from "@/components/ui/skeleton";
import { fetchSkillDetail, fetchSkills, pinSkill } from "@/lib/workbench-api";

const states: Record<string, string> = { active: "活跃", stale: "闲置", archived: "已归档" };

export function SkillsPage() {
  const queryClient = useQueryClient();
  const [selectedSkill, setSelectedSkill] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const detailRef = useRef<HTMLElement>(null);
  const libraryRef = useRef<HTMLElement>(null);
  const skillsQuery = useQuery({ queryKey: ["skills"], queryFn: () => fetchSkills(true) });
  const detailQuery = useQuery({ queryKey: ["skill-detail", selectedSkill], queryFn: () => fetchSkillDetail(selectedSkill!), enabled: !!selectedSkill });
  const pinMutation = useMutation({
    mutationFn: ({ name, pinned }: { name: string; pinned: boolean }) => pinSkill(name, pinned),
    onSuccess: () => {
      toast.success("已更新固定状态");
      void queryClient.invalidateQueries({ queryKey: ["skills"] });
    },
    onError: (error: unknown) => toast.error(error instanceof Error ? error.message : "操作失败"),
  });
  const skills = skillsQuery.data?.skills ?? [];
  const term = search.trim().toLowerCase();
  const filtered = skills.filter(skill => `${skill.name} ${skill.description ?? ""} ${skill.category}`.toLowerCase().includes(term));
  const detail = detailQuery.data;

  useEffect(() => {
    if (selectedSkill && window.innerWidth < 1024) {
      detailRef.current?.focus({ preventScroll: true });
      detailRef.current?.scrollIntoView({ block: "start" });
    }
  }, [selectedSkill, detailQuery.isPending]);

  return <div className="workspace-page skills-page"><div className="page-container">
    <PageHeading title="技能" icon={Sparkles} />
    <div className="collection-workspace">
      <section ref={libraryRef} tabIndex={-1} className="collection-library" aria-label="技能列表">
        <div className="collection-toolbar">
          <div><h2><Sparkles size={17} aria-hidden="true" />交易技能</h2><p>{skillsQuery.isSuccess ? `${skills.length} 个技能` : "技能目录"}</p></div>
          <label className="workspace-search"><Search size={16} aria-hidden="true" /><Input aria-label="搜索技能" placeholder="搜索名称、类别或内容说明" value={search} onChange={event => setSearch(event.target.value)} /></label>
        </div>
        {skillsQuery.isPending ? <div className="collection-loading"><Skeleton className="h-20" /><Skeleton className="h-20" /><Skeleton className="h-20" /></div>
          : skillsQuery.error ? <QueryNotice title="无法加载技能" error={skillsQuery.error} retry={() => void skillsQuery.refetch()} />
          : !skills.length ? <EmptyState icon={Sparkles} title="还没有交易技能" description="研究中积累的技能会出现在这里。" />
          : !filtered.length ? <EmptyState icon={Search} title="没有匹配的技能" description="试试其他关键词，或清除搜索查看全部技能。" action={<Button variant="outline" size="sm" onClick={() => setSearch("")}>清除搜索</Button>} />
          : <ul className="skill-library-list">{filtered.map(skill => <li key={skill.name} data-selected={selectedSkill === skill.name}>
            <button className="skill-open" aria-controls="skill-detail" aria-pressed={selectedSkill === skill.name} onClick={() => setSelectedSkill(skill.name)}>
              <span className="skill-row-title">{skill.pinned ? <Pin size={14} aria-label="已固定" /> : <FileText size={16} aria-hidden="true" />}<strong>{skill.name}</strong><NewBadge createdAt={skill.created_at} /><ArrowUpRight size={15} aria-hidden="true" /></span>
              <span className="skill-row-description">{skill.description || "暂无说明"}</span>
              <span className="skill-row-meta"><Badge variant="secondary">{skill.category}</Badge><span>{states[skill.state] ?? skill.state}</span><span>使用 {skill.use_count} 次</span></span>
            </button>
            <Button size="icon" variant="ghost" className="skill-pin" disabled={pinMutation.isPending} aria-label={`${skill.pinned ? "取消固定" : "固定"} ${skill.name}`} onClick={() => pinMutation.mutate({ name: skill.name, pinned: !skill.pinned })}>{skill.pinned ? <PinOff size={16} /> : <Pin size={16} />}</Button>
          </li>)}</ul>}
        {term && skillsQuery.isSuccess ? <p className="collection-footnote" role="status">显示 {filtered.length} / {skills.length} 个技能</p> : null}
      </section>
      <section tabIndex={-1} id="skill-detail" ref={detailRef} className="detail-pane" aria-label="技能详情">
        <div className="detail-pane-heading"><FileText size={17} aria-hidden="true" /><h2>技能详情</h2>{selectedSkill ? <Button size="sm" variant="ghost" className="lg:hidden" onClick={() => { libraryRef.current?.focus({ preventScroll: true }); libraryRef.current?.scrollIntoView({ block: "start" }); }}>返回列表</Button> : null}</div>
        {!selectedSkill ? <EmptyState icon={FileText} title="选择一个技能" description="从列表中打开，开始阅读。" />
          : detailQuery.isPending ? <div className="collection-loading"><Skeleton className="h-8" /><Skeleton className="h-64" /></div>
          : detailQuery.error ? <QueryNotice title="无法读取技能" error={detailQuery.error} retry={() => void detailQuery.refetch()} />
          : detail ? <motion.div key={selectedSkill} initial={{ opacity: 0, x: 10 }} animate={{ opacity: 1, x: 0 }} transition={{ duration: .24 }} className="detail-pane-body">
            <h3 className="detail-title">{detail.name}</h3><p className="detail-description">{detail.description}</p>
            <dl className="record-facts">
              {detail.created_by ? <div><dt>创建者</dt><dd>{detail.created_by}</dd></div> : null}
              <div><dt>修改次数</dt><dd>{detail.patch_count}</dd></div>
              {detail.last_used_at ? <div><dt>最近使用</dt><dd>{new Date(detail.last_used_at).toLocaleDateString("zh-CN")}</dd></div> : null}
            </dl>
            <div className="skill-document prose prose-sm max-w-none dark:prose-invert"><MarkdownRenderer>{detail.content}</MarkdownRenderer></div>
            <details className="document-source">
              <summary>
                <FileCode2 size={16} className="document-source-icon" aria-hidden="true" />
                <span className="document-source-show">查看原文</span><span className="document-source-hide">收起原文</span>
                <span className="document-source-format" aria-hidden="true">Markdown</span>
                <ChevronDown size={15} className="document-source-chevron" aria-hidden="true" />
              </summary>
              <div className="document-source-content"><pre className="document-reader" tabIndex={0} aria-label={`${detail.name} 原文`}><code>{detail.content}</code></pre></div>
            </details>
          </motion.div> : null}
      </section>
    </div>
  </div></div>;
}
