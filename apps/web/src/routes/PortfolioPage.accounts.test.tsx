import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Account, AccountKind } from "@/lib/types";
import { fetchAccounts } from "@/lib/workbench-api";
import { PortfolioPage } from "./PortfolioPage";

vi.mock("@/components/portfolio/AutonomousTradingControl", () => ({
  AutonomousTradingControl: () => null,
}));
vi.mock("@/lib/workbench-api", () => ({
  fetchAccounts: vi.fn(),
  fetchPortfolio: () => new Promise(() => {}),
  createAccount: vi.fn(),
  deleteAccount: vi.fn(),
  postTrade: vi.fn(),
  updateAccount: vi.fn(),
}));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

async function renderWithAccounts(kinds: AccountKind[]) {
  vi.mocked(fetchAccounts).mockResolvedValue(kinds.map((kind) => ({
    id: kind,
    kind,
    name: kind,
    capital: 100_000,
  })) as Account[]);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(<QueryClientProvider client={queryClient}><PortfolioPage /></QueryClientProvider>);
  });
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
  return container;
}

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  container?.remove();
  root = null;
  container = null;
  vi.mocked(fetchAccounts).mockReset();
});

describe("account creation choices", () => {
  it("offers only unused kinds and selects the first available kind", async () => {
    const view = await renderWithAccounts(["short_stock", "etf_rotation", "mid_term"]);
    const create = Array.from(view.querySelectorAll("button")).find((button) => button.textContent?.includes("新建账户"));
    expect(create?.disabled).toBe(false);
    await act(async () => { create?.click(); });
    const select = view.querySelector<HTMLSelectElement>("select");
    expect(select?.value).toBe("long_term");
    expect(Array.from(select?.options ?? []).map((option) => option.value)).toEqual(["long_term", "mixed"]);
  });

  it("does not open a form when every kind already exists", async () => {
    const view = await renderWithAccounts(["short_stock", "etf_rotation", "mid_term", "long_term", "mixed"]);
    const create = Array.from(view.querySelectorAll("button")).find((button) => button.textContent?.includes("账户类型已用完"));
    expect(create?.disabled).toBe(true);
    expect(view.querySelector("select")).toBeNull();
  });
});
