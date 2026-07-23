import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Briefcase, Loader2, Pencil, PlusCircle, Trash2, X } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { NewBadge } from "@/components/ui/new-badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { Account, AccountKind, TradeSide } from "@/lib/types";
import {
  createAccount,
  deleteAccount,
  fetchAccounts,
  fetchPortfolio,
  postTrade,
  updateAccount,
} from "@/lib/workbench-api";

function isMarketHours(): boolean {
  const now = new Date();
  const day = now.getDay();
  if (day === 0 || day === 6) return false;
  const h = now.getHours();
  const m = now.getMinutes();
  const t = h * 60 + m;
  return t >= 9 * 60 + 30 && t <= 15 * 60;
}

function formatMoney(value: number): string {
  return value.toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatPct(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

function formatTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function pnlClass(value: number): string {
  if (value > 0) return "text-red-600 dark:text-red-500";
  if (value < 0) return "text-emerald-600 dark:text-emerald-400";
  return "text-muted-foreground";
}

const selectClassName =
  "flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2";

const ACCOUNT_KIND_OPTIONS: { value: AccountKind; label: string }[] = [
  { value: "short_stock", label: "短线股票" },
  { value: "etf_rotation", label: "ETF 轮动" },
  { value: "mid_term", label: "中线" },
  { value: "long_term", label: "长线" },
  { value: "mixed", label: "混合" },
];

export function PortfolioPage() {
  const queryClient = useQueryClient();

  const accountsQuery = useQuery({
    queryKey: ["accounts"],
    queryFn: fetchAccounts,
  });

  const portfolioQuery = useQuery({
    queryKey: ["portfolio"],
    queryFn: fetchPortfolio,
    refetchInterval: isMarketHours() ? 30_000 : false,
  });

  // Account management state
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [editingAccount, setEditingAccount] = useState<Account | null>(null);
  const [selectedAccountId, setSelectedAccountId] = useState<string | null>(null);
  const [newAccount, setNewAccount] = useState({ kind: "short_stock" as AccountKind, name: "", description: "", capital: "" });
  const [editForm, setEditForm] = useState({ name: "", description: "", capital: "" });

  // Trade form state
  const [symbol, setSymbol] = useState("");
  const [account, setAccount] = useState<AccountKind>("short_stock");
  const [side, setSide] = useState<TradeSide>("buy");
  const [quantity, setQuantity] = useState("");
  const [price, setPrice] = useState("");

  const createMutation = useMutation({
    mutationFn: createAccount,
    onSuccess: () => {
      toast.success("账户已创建");
      void queryClient.invalidateQueries({ queryKey: ["accounts"] });
      setShowCreateForm(false);
      setNewAccount({ kind: "short_stock", name: "", description: "", capital: "" });
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, ...body }: { id: string; name?: string; description?: string; capital?: number }) =>
      updateAccount(id, body),
    onSuccess: () => {
      toast.success("账户已更新");
      void queryClient.invalidateQueries({ queryKey: ["accounts"] });
      setEditingAccount(null);
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const deleteMutation = useMutation({
    mutationFn: deleteAccount,
    onSuccess: () => {
      toast.success("账户已删除");
      void queryClient.invalidateQueries({ queryKey: ["accounts"] });
      if (selectedAccountId) setSelectedAccountId(null);
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const tradeMutation = useMutation({
    mutationFn: postTrade,
    onSuccess: () => {
      toast.success("模拟成交已记录");
      void queryClient.invalidateQueries({ queryKey: ["portfolio"] });
      void queryClient.invalidateQueries({ queryKey: ["accounts"] });
      setSymbol("");
      setQuantity("");
      setPrice("");
    },
    onError: (err: Error) => toast.error(err.message || "提交失败"),
  });

  const handleCreateSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!newAccount.name.trim()) { toast.error("请输入账户名"); return; }
    createMutation.mutate({
      kind: newAccount.kind,
      name: newAccount.name.trim(),
      description: newAccount.description,
      capital: Number(newAccount.capital) || 0,
    });
  };

  const handleEditSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!editingAccount) return;
    updateMutation.mutate({
      id: editingAccount.id,
      name: editForm.name || undefined,
      description: editForm.description || undefined,
      capital: editForm.capital ? Number(editForm.capital) : undefined,
    });
  };

  const handleTradeSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    const qty = Number.parseInt(quantity, 10);
    const px = Number.parseFloat(price);
    if (!symbol.trim()) { toast.error("请输入代码"); return; }
    if (!Number.isFinite(qty) || qty <= 0) { toast.error("请输入有效数量"); return; }
    if (!Number.isFinite(px) || px <= 0) { toast.error("请输入有效价格"); return; }
    tradeMutation.mutate({ symbol: symbol.trim(), account, side, quantity: qty, price: px });
  };

  const accounts: Account[] = accountsQuery.data ?? [];
  const accountLabel = (id: string) => accounts.find((a) => a.id === id || a.kind === id)?.name ?? id;

  const data = portfolioQuery.data;
  const accountKinds = accounts.map((a) => a.kind) as AccountKind[];
  const allPositions = data
    ? accountKinds.flatMap((acct) =>
        (data.positions_by_account[acct] ?? []).map((pos) => ({ ...pos, account: acct })),
      )
    : [];

  const filteredPositions = selectedAccountId
    ? allPositions.filter((p) => {
        const acct = accounts.find((a) => a.id === selectedAccountId);
        return acct && p.account === acct.kind;
      })
    : allPositions;

  return (
    <div className="flex-1 overflow-y-auto p-4 md:p-6">
      <div className="mx-auto max-w-5xl space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-semibold tracking-tight">模拟持仓</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              账户管理、持仓与已实现交易
              {isMarketHours() && (
                <span className="ml-2 text-emerald-600">● 盘中自动刷新</span>
              )}
            </p>
          </div>
          <Button size="sm" variant="outline" onClick={() => setShowCreateForm(true)}>
            <PlusCircle className="mr-1 h-4 w-4" />
            新建账户
          </Button>
        </div>

        {portfolioQuery.isLoading && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> 加载中…
          </div>
        )}

        {portfolioQuery.error && (
          <Card className="border-destructive/40">
            <CardHeader>
              <CardTitle className="text-base text-destructive">无法加载持仓</CardTitle>
              <CardDescription>
                {portfolioQuery.error instanceof Error ? portfolioQuery.error.message : "未知错误"}
              </CardDescription>
            </CardHeader>
          </Card>
        )}

        {/* Create account form */}
        {showCreateForm && (
          <Card>
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between">
                <CardTitle className="text-base">新建账户</CardTitle>
                <Button size="icon" variant="ghost" onClick={() => setShowCreateForm(false)}>
                  <X className="h-4 w-4" />
                </Button>
              </div>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleCreateSubmit} className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
                <div className="space-y-1">
                  <label className="text-xs font-medium text-muted-foreground">类型</label>
                  <select
                    className={selectClassName}
                    value={newAccount.kind}
                    onChange={(e) => setNewAccount({ ...newAccount, kind: e.target.value as AccountKind })}
                  >
                    {ACCOUNT_KIND_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                  </select>
                </div>
                <div className="space-y-1">
                  <label className="text-xs font-medium text-muted-foreground">名称</label>
                  <Input placeholder="我的账户" value={newAccount.name} onChange={(e) => setNewAccount({ ...newAccount, name: e.target.value })} />
                </div>
                <div className="space-y-1">
                  <label className="text-xs font-medium text-muted-foreground">描述</label>
                  <Input placeholder="可选" value={newAccount.description} onChange={(e) => setNewAccount({ ...newAccount, description: e.target.value })} />
                </div>
                <div className="space-y-1">
                  <label className="text-xs font-medium text-muted-foreground">资金</label>
                  <Input type="number" min={0} step={10000} placeholder="300000" value={newAccount.capital} onChange={(e) => setNewAccount({ ...newAccount, capital: e.target.value })} />
                </div>
                <div className="flex items-end">
                  <Button type="submit" className="w-full" disabled={createMutation.isPending}>
                    {createMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : "创建"}
                  </Button>
                </div>
              </form>
            </CardContent>
          </Card>
        )}

        {/* Edit account form */}
        {editingAccount && (
          <Card>
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between">
                <CardTitle className="text-base">编辑账户: {editingAccount.name}</CardTitle>
                <Button size="icon" variant="ghost" onClick={() => setEditingAccount(null)}>
                  <X className="h-4 w-4" />
                </Button>
              </div>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleEditSubmit} className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <div className="space-y-1">
                  <label className="text-xs font-medium text-muted-foreground">名称</label>
                  <Input value={editForm.name} onChange={(e) => setEditForm({ ...editForm, name: e.target.value })} />
                </div>
                <div className="space-y-1">
                  <label className="text-xs font-medium text-muted-foreground">描述</label>
                  <Input value={editForm.description} onChange={(e) => setEditForm({ ...editForm, description: e.target.value })} />
                </div>
                <div className="space-y-1">
                  <label className="text-xs font-medium text-muted-foreground">资金</label>
                  <Input type="number" min={0} step={10000} value={editForm.capital} onChange={(e) => setEditForm({ ...editForm, capital: e.target.value })} />
                </div>
                <div className="flex items-end">
                  <Button type="submit" className="w-full" disabled={updateMutation.isPending}>
                    {updateMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : "保存"}
                  </Button>
                </div>
              </form>
            </CardContent>
          </Card>
        )}

        {data && (
          <>
            {/* Account cards */}
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {accounts.map((acct) => {
                const summary = data.accounts.find((s) => s.account === acct.kind);
                const isSelected = selectedAccountId === acct.id;
                return (
                  <Card
                    key={acct.id}
                    className={`cursor-pointer transition-colors ${isSelected ? "ring-2 ring-primary" : "hover:bg-muted/50"}`}
                    onClick={() => setSelectedAccountId(isSelected ? null : acct.id)}
                  >
                    <CardHeader className="pb-2">
                      <div className="flex items-start justify-between">
                        <div>
                          <CardTitle className="flex items-center gap-2 text-base">
                            {acct.name}
                            <NewBadge createdAt={acct.created_at} />
                          </CardTitle>
                          <CardDescription className="font-mono text-xs">
                            {acct.kind} · 资金 {formatMoney(acct.capital)}
                          </CardDescription>
                        </div>
                        <div className="flex gap-1" onClick={(e) => e.stopPropagation()}>
                          <Button
                            size="icon"
                            variant="ghost"
                            className="h-7 w-7"
                            onClick={() => {
                              setEditingAccount(acct);
                              setEditForm({ name: acct.name, description: acct.description, capital: String(acct.capital) });
                            }}
                          >
                            <Pencil className="h-3 w-3" />
                          </Button>
                          <Button
                            size="icon"
                            variant="ghost"
                            className="h-7 w-7 text-destructive hover:text-destructive"
                            onClick={() => {
                              if (confirm(`确认删除账户「${acct.name}」？`)) {
                                deleteMutation.mutate(acct.id);
                              }
                            }}
                          >
                            <Trash2 className="h-3 w-3" />
                          </Button>
                        </div>
                      </div>
                      {acct.description && (
                        <p className="text-xs text-muted-foreground mt-1">{acct.description}</p>
                      )}
                    </CardHeader>
                    <CardContent className="space-y-2 text-sm">
                      <div className="flex justify-between">
                        <span className="text-muted-foreground">持仓数</span>
                        <span>{summary?.position_count ?? 0}</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-muted-foreground">市值 / 利用率</span>
                        <span>
                          {formatMoney(summary?.market_value ?? 0)}
                          {acct.capital > 0 && (
                            <span className="ml-1 text-xs text-muted-foreground">
                              ({formatPct((summary?.market_value ?? 0) / acct.capital)})
                            </span>
                          )}
                        </span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-muted-foreground">未实现盈亏</span>
                        <span className={pnlClass(summary?.unrealized_pnl ?? 0)}>
                          {formatMoney(summary?.unrealized_pnl ?? 0)}
                          {(summary?.cost_basis ?? 0) > 0 && (
                            <span className="ml-1 text-xs">
                              ({((summary?.unrealized_pnl ?? 0) / (summary?.cost_basis ?? 1) * 100).toFixed(2)}%)
                            </span>
                          )}
                        </span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-muted-foreground">已实现盈亏</span>
                        <span className={pnlClass(summary?.realized_pnl ?? 0)}>
                          {formatMoney(summary?.realized_pnl ?? 0)}
                          {acct.capital > 0 && (summary?.realized_pnl ?? 0) !== 0 && (
                            <span className="ml-1 text-xs">
                              ({((summary?.realized_pnl ?? 0) / acct.capital * 100).toFixed(2)}%)
                            </span>
                          )}
                        </span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-muted-foreground">总收益</span>
                        {(() => {
                          const total = (summary?.unrealized_pnl ?? 0) + (summary?.realized_pnl ?? 0);
                          return (
                            <span className={pnlClass(total)}>
                              {formatMoney(total)}
                              {acct.capital > 0 && <span className="ml-1 text-xs">({(total / acct.capital * 100).toFixed(2)}%)</span>}
                            </span>
                          );
                        })()}
                      </div>
                      <div className="flex justify-between text-xs text-muted-foreground">
                        <span>胜率 {formatPct(summary?.win_rate ?? 0)}</span>
                        <span>手续费 {formatMoney(summary?.fees ?? 0)}</span>
                      </div>
                    </CardContent>
                  </Card>
                );
              })}
            </div>

            {/* Positions table */}
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-base">
                  <Briefcase className="h-4 w-4" />
                  {selectedAccountId
                    ? `持仓 — ${accountLabel(accounts.find((a) => a.id === selectedAccountId)?.kind ?? "")}`
                    : "当前持仓（全部）"}
                </CardTitle>
                <CardDescription>
                  {selectedAccountId ? "点击账户卡片取消筛选" : "点击账户卡片可筛选"}
                </CardDescription>
              </CardHeader>
              <CardContent>
                {filteredPositions.length === 0 ? (
                  <p className="text-sm text-muted-foreground">暂无持仓。</p>
                ) : (
                  <div className="-mx-6 overflow-x-auto px-6">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>账户</TableHead>
                          <TableHead>股票</TableHead>
                          <TableHead className="text-right">数量</TableHead>
                          <TableHead className="text-right">成本价</TableHead>
                          <TableHead className="text-right">现价</TableHead>
                          <TableHead className="text-right">市值</TableHead>
                          <TableHead className="text-right">浮盈</TableHead>
                          <TableHead className="text-right">盈亏%</TableHead>
                          <TableHead className="text-right">操作</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {filteredPositions.map((pos) => {
                          const pnlPct = pos.pnl_pct ?? (pos.avg_cost > 0 ? (pos.last_price / pos.avg_cost - 1) * 100 : 0);
                          return (
                            <TableRow key={`${pos.account}-${pos.symbol}`}>
                              <TableCell>
                                <Badge variant="secondary">{accountLabel(pos.account)}</Badge>
                              </TableCell>
                              <TableCell>
                                <div className="flex flex-col">
                                  <div className="flex items-center gap-2">
                                    {pos.name && <span className="text-sm">{pos.name}</span>}
                                    <NewBadge createdAt={pos.opened_at} />
                                  </div>
                                  <span className="font-mono text-xs text-muted-foreground">{pos.symbol}</span>
                                </div>
                              </TableCell>
                              <TableCell className="text-right">{pos.quantity}</TableCell>
                              <TableCell className="text-right">{formatMoney(pos.avg_cost)}</TableCell>
                              <TableCell className="text-right">{formatMoney(pos.last_price)}</TableCell>
                              <TableCell className="text-right">{formatMoney(pos.market_value)}</TableCell>
                              <TableCell className={`text-right ${pnlClass(pos.unrealized_pnl)}`}>
                                {formatMoney(pos.unrealized_pnl)}
                              </TableCell>
                              <TableCell className={`text-right ${pnlClass(pnlPct)}`}>
                                {pnlPct >= 0 ? "+" : ""}{pnlPct.toFixed(2)}%
                              </TableCell>
                              <TableCell className="text-right">
                                <div className="flex justify-end gap-1">
                                  <Button
                                    size="sm"
                                    variant="ghost"
                                    className="h-6 px-2 text-xs text-emerald-600 hover:text-emerald-700"
                                    onClick={() => {
                                      setSymbol(pos.symbol);
                                      setAccount(pos.account);
                                      setSide("buy");
                                      setPrice(String(pos.last_price));
                                      setQuantity("");
                                      document.getElementById("trade-qty")?.focus();
                                    }}
                                  >
                                    买入
                                  </Button>
                                  <Button
                                    size="sm"
                                    variant="ghost"
                                    className="h-6 px-2 text-xs text-red-600 hover:text-red-700"
                                    onClick={() => {
                                      setSymbol(pos.symbol);
                                      setAccount(pos.account);
                                      setSide("sell");
                                      setPrice(String(pos.last_price));
                                      setQuantity(String(pos.quantity));
                                      document.getElementById("trade-qty")?.focus();
                                    }}
                                  >
                                    卖出
                                  </Button>
                                </div>
                              </TableCell>
                            </TableRow>
                          );
                        })}
                      </TableBody>
                    </Table>
                  </div>
                )}
              </CardContent>
            </Card>

            {/* Realized trades */}
            <Card>
              <CardHeader>
                <CardTitle className="text-base">已实现交易</CardTitle>
                <CardDescription>平仓记录（FIFO 匹配）</CardDescription>
              </CardHeader>
              <CardContent>
                {data.realized_trades.length === 0 ? (
                  <p className="text-sm text-muted-foreground">暂无已实现交易。</p>
                ) : (
                  <div className="-mx-6 overflow-x-auto px-6">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>账户</TableHead>
                          <TableHead>代码</TableHead>
                          <TableHead className="text-right">数量</TableHead>
                          <TableHead className="text-right">买入</TableHead>
                          <TableHead className="text-right">卖出</TableHead>
                          <TableHead className="text-right">盈亏</TableHead>
                          <TableHead className="text-right">手续费</TableHead>
                          <TableHead>平仓时间</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {data.realized_trades.map((trade, index) => (
                          <TableRow key={`${trade.account}-${trade.symbol}-${trade.closed_at}-${index}`}>
                            <TableCell>
                              <Badge variant="outline">{accountLabel(trade.account)}</Badge>
                            </TableCell>
                            <TableCell className="font-mono text-xs">{trade.symbol}</TableCell>
                            <TableCell className="text-right">{trade.quantity}</TableCell>
                            <TableCell className="text-right">{formatMoney(trade.entry_price)}</TableCell>
                            <TableCell className="text-right">{formatMoney(trade.exit_price)}</TableCell>
                            <TableCell className={`text-right ${pnlClass(trade.pnl)}`}>
                              {formatMoney(trade.pnl)}
                            </TableCell>
                            <TableCell className="text-right">{formatMoney(trade.fees)}</TableCell>
                            <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                              {formatTime(trade.closed_at)}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                )}
              </CardContent>
            </Card>

            {/* Trade form */}
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-base">
                  <PlusCircle className="h-4 w-4" />
                  提交纸面成交
                </CardTitle>
                <CardDescription>
                  系统自动识别市场规则（T+0/T+1、涨跌幅）。
                </CardDescription>
              </CardHeader>
              <CardContent>
                <form onSubmit={handleTradeSubmit} className="grid gap-4 sm:grid-cols-2 lg:grid-cols-6">
                  <div className="space-y-1.5">
                    <label htmlFor="trade-symbol" className="text-xs font-medium text-muted-foreground">代码</label>
                    <Input id="trade-symbol" placeholder="600519" value={symbol} onChange={(e) => setSymbol(e.target.value)} className="font-mono" />
                  </div>
                  <div className="space-y-1.5">
                    <label htmlFor="trade-account" className="text-xs font-medium text-muted-foreground">账户</label>
                    <select id="trade-account" className={selectClassName} value={account} onChange={(e) => setAccount(e.target.value as AccountKind)}>
                      {accounts.map((acct) => (
                        <option key={acct.id} value={acct.kind}>{acct.name}</option>
                      ))}
                    </select>
                  </div>
                  <div className="space-y-1.5">
                    <label htmlFor="trade-side" className="text-xs font-medium text-muted-foreground">方向</label>
                    <select id="trade-side" className={selectClassName} value={side} onChange={(e) => setSide(e.target.value as TradeSide)}>
                      <option value="buy">买入</option>
                      <option value="sell">卖出</option>
                    </select>
                  </div>
                  <div className="space-y-1.5">
                    <label htmlFor="trade-qty" className="text-xs font-medium text-muted-foreground">数量</label>
                    <Input id="trade-qty" type="number" min={1} step="any" placeholder="100" value={quantity} onChange={(e) => setQuantity(e.target.value)} />
                  </div>
                  <div className="space-y-1.5">
                    <label htmlFor="trade-price" className="text-xs font-medium text-muted-foreground">价格</label>
                    <Input id="trade-price" type="number" min={0} step="0.01" placeholder="10.00" value={price} onChange={(e) => setPrice(e.target.value)} />
                  </div>
                  <div className="flex items-end">
                    <Button type="submit" className="w-full" disabled={tradeMutation.isPending}>
                      {tradeMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : "提交"}
                    </Button>
                  </div>
                </form>
              </CardContent>
            </Card>
          </>
        )}

        {!data && portfolioQuery.isLoading && <Skeleton className="h-48 w-full" />}
      </div>
    </div>
  );
}
