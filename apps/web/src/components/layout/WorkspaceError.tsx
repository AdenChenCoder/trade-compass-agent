import { AlertCircle, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EmptyState } from "./WorkspaceState";

export function WorkspaceError() {
  return <main className="workspace-recovery">
    <a href="/agent" className="recovery-brand" aria-label="交易罗盘首页"><span className="brand-mark" aria-hidden="true" /><strong>交易罗盘</strong></a>
    <EmptyState icon={AlertCircle} title="页面暂时无法打开" description="可能是页面更新或连接中断。请重新加载后再试。" action={<Button onClick={() => window.location.reload()}><RefreshCw size={16} className="mr-2" />重新加载</Button>} />
    <a href="/agent" className="recovery-link">返回对话</a>
  </main>;
}
