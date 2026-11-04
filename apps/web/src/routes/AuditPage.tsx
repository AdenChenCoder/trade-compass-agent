import { motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowUpRight, ClipboardList, FileSearch, Search, X } from "lucide-react";
import { PageHeading } from "@/components/layout/PageHeading";
import { EmptyState, QueryNotice } from "@/components/layout/WorkspaceState";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fetchAudit, fetchAuditEvent } from "@/lib/workbench-api";

function formatTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function AuditPage() {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const detailRef = useRef<HTMLElement>(null);
  const libraryRef = useRef<HTMLElement>(null);
  const auditQuery = useQuery({ queryKey: ["audit"], queryFn: () => fetchAudit(100) });
  const detailQuery = useQuery({ queryKey: ["audit", selectedId], queryFn: () => fetchAuditEvent(selectedId!), enabled: Boolean(selectedId) });
  const records = auditQuery.data ?? [];
  const term = search.trim().toLowerCase();
  const filtered = records.filter(event => `${event.id} ${event.event_type} ${event.summary}`.toLowerCase().includes(term));
  const detail = detailQuery.data ?? records.find(event => event.id === selectedId);
  useEffect(() => {
    if (selectedId && window.innerWidth < 1024) {
      detailRef.current?.focus({ preventScroll: true });
      detailRef.current?.scrollIntoView({ block: "start" });
    }
  }, [selectedId, detailQuery.isPending]);

  return <div className="workspace-page audit-page"><div className="page-container">
    <PageHeading title="审计回放" icon={ClipboardList} />
    <div className="collection-workspace audit-workspace">
      <section ref={libraryRef} tabIndex={-1} className="collection-library" aria-label="审计事件">
        <div className="collection-toolbar">
          <div><h2><ClipboardList size={17} aria-hidden="true" />事件记录</h2><p>最新在前，最多展示最近 100 条</p></div>
          <label className="workspace-search"><Search size={16} aria-hidden="true" /><Input aria-label="搜索审计记录" placeholder="搜索摘要、类型或记录 ID" value={search} onChange={event => setSearch(event.target.value)} /></label>
        </div>
        {auditQuery.isPending ? <div className="collection-loading"><Skeleton className="h-16" /><Skeleton className="h-16" /><Skeleton className="h-16" /></div>
          : auditQuery.error ? <QueryNotice title="无法加载审计记录" error={auditQuery.error} retry={() => void auditQuery.refetch()} />
          : !records.length ? <EmptyState icon={ClipboardList} title="还没有审计记录" description="推荐与决策产生的记录会展示在这里，可逐条回看。" />
          : !filtered.length ? <EmptyState icon={Search} title="没有匹配的记录" description="搜索范围为当前加载的最近记录。" action={<Button size="sm" variant="outline" onClick={() => setSearch("")}>清除搜索</Button>} />
          : <Table><TableHeader><TableRow><TableHead>时间 / 类型</TableHead><TableHead>事件摘要</TableHead></TableRow></TableHeader><TableBody>{filtered.map(event => <TableRow key={event.id} data-selected={selectedId === event.id}>
            <TableCell><div className="audit-event-time"><time dateTime={event.timestamp}>{formatTime(event.timestamp)}</time><Badge variant="secondary">{event.event_type}</Badge></div></TableCell>
            <TableCell><button className="audit-event-open" aria-controls="audit-detail" aria-pressed={selectedId === event.id} onClick={() => setSelectedId(event.id)}><span>{event.summary || event.id}</span><ArrowUpRight size={16} aria-hidden="true" /></button></TableCell>
          </TableRow>)}</TableBody></Table>}
        {auditQuery.isSuccess ? <p className="collection-footnote" role="status">显示 {filtered.length} / {records.length} 条记录</p> : null}
      </section>
      <section ref={detailRef} tabIndex={-1} id="audit-detail" className="detail-pane" aria-label="事件详情">
        <div className="detail-pane-heading"><FileSearch size={17} aria-hidden="true" /><h2>事件详情</h2>{selectedId ? <Button size="sm" variant="ghost" className="lg:hidden" onClick={() => { libraryRef.current?.focus({ preventScroll: true }); libraryRef.current?.scrollIntoView({ block: "start" }); }}>返回列表</Button> : null}{selectedId ? <Button size="icon" variant="ghost" aria-label="关闭详情" onClick={() => setSelectedId(null)}><X size={17} /></Button> : null}</div>
        {!selectedId ? <EmptyState icon={FileSearch} title="选择一条记录" description="从列表中选择一条事件。" />
          : detailQuery.isLoading && !detail ? <div className="collection-loading"><Skeleton className="h-48" /></div>
          : detailQuery.error ? <QueryNotice title="无法加载详情" error={detailQuery.error} retry={() => void detailQuery.refetch()} />
          : detail ? <motion.div key={selectedId} initial={{ opacity: 0, x: 10 }} animate={{ opacity: 1, x: 0 }} transition={{ duration: .24 }} className="detail-pane-body">
            <Badge variant="secondary">{detail.event_type}</Badge>
            <h3 className="detail-title">{detail.summary}</h3>
            <dl className="record-facts"><div><dt>发生时间</dt><dd>{formatTime(detail.timestamp)}</dd></div><div><dt>记录 ID</dt><dd className="font-mono">{detail.id}</dd></div></dl>
            <h4 className="document-label">原始记录</h4><pre className="document-reader audit-payload">{JSON.stringify(detail.payload, null, 2)}</pre>
          </motion.div> : null}
      </section>
    </div>
  </div></div>;
}
