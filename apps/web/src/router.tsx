import { Navigate, createBrowserRouter } from "react-router-dom";
import { AppLayout } from "@/components/layout/AppLayout";
import { AgentPage } from "@/routes/AgentPage";
import { recoverRoute } from "@/lib/route-recovery";

function loadPage(path: string, load: Parameters<typeof recoverRoute>[0]) {
  return recoverRoute(load, () => {
    if (!router.state.initialized) return "/agent";
    if (router.state.navigation.location?.pathname !== path) return null;
    const current = router.state.location;
    return current.pathname === path ? "/agent" : current.pathname + current.search + current.hash;
  });
}

export const router = createBrowserRouter([
  {
    element: <AppLayout />,
    children: [
      { index: true, element: <Navigate to="/agent" replace /> },
      { path: "/agent", element: <AgentPage /> },
      {
        path: "/research",
        ...loadPage("/research", async () => {
          const { ResearchPage } = await import("@/routes/ResearchPage");
          return { Component: ResearchPage };
        }),
      },
      {
        path: "/portfolio",
        ...loadPage("/portfolio", async () => {
          const { PortfolioPage } = await import("@/routes/PortfolioPage");
          return { Component: PortfolioPage };
        }),
      },
      {
        path: "/memory",
        ...loadPage("/memory", async () => {
          const { MemoryPage } = await import("@/routes/MemoryPage");
          return { Component: MemoryPage };
        }),
      },
      {
        path: "/audit",
        ...loadPage("/audit", async () => {
          const { AuditPage } = await import("@/routes/AuditPage");
          return { Component: AuditPage };
        }),
      },
      {
        path: "/rules",
        ...loadPage("/rules", async () => {
          const { RulesPage } = await import("@/routes/RulesPage");
          return { Component: RulesPage };
        }),
      },
      {
        path: "/skills",
        ...loadPage("/skills", async () => {
          const { SkillsPage } = await import("@/routes/SkillsPage");
          return { Component: SkillsPage };
        }),
      },
      {
        path: "/jobs",
        ...loadPage("/jobs", async () => {
          const { JobsPage } = await import("@/routes/JobsPage");
          return { Component: JobsPage };
        }),
      },
      {
        path: "/settings",
        ...loadPage("/settings", async () => {
          const { SettingsPage } = await import("@/routes/SettingsPage");
          return { Component: SettingsPage };
        }),
      },
    ],
  },
]);
