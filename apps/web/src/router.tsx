import { Navigate, createBrowserRouter } from "react-router-dom";
import { AppLayout } from "@/components/layout/AppLayout";
import { AgentPage } from "@/routes/AgentPage";

export const router = createBrowserRouter([
  {
    element: <AppLayout />,
    children: [
      { index: true, element: <Navigate to="/agent" replace /> },
      { path: "/agent", element: <AgentPage /> },
      {
        path: "/portfolio",
        lazy: async () => {
          const { PortfolioPage } = await import("@/routes/PortfolioPage");
          return { Component: PortfolioPage };
        },
      },
      {
        path: "/memory",
        lazy: async () => {
          const { MemoryPage } = await import("@/routes/MemoryPage");
          return { Component: MemoryPage };
        },
      },
      {
        path: "/audit",
        lazy: async () => {
          const { AuditPage } = await import("@/routes/AuditPage");
          return { Component: AuditPage };
        },
      },
      {
        path: "/rules",
        lazy: async () => {
          const { RulesPage } = await import("@/routes/RulesPage");
          return { Component: RulesPage };
        },
      },
      {
        path: "/skills",
        lazy: async () => {
          const { SkillsPage } = await import("@/routes/SkillsPage");
          return { Component: SkillsPage };
        },
      },
      {
        path: "/jobs",
        lazy: async () => {
          const { JobsPage } = await import("@/routes/JobsPage");
          return { Component: JobsPage };
        },
      },
      {
        path: "/settings",
        lazy: async () => {
          const { SettingsPage } = await import("@/routes/SettingsPage");
          return { Component: SettingsPage };
        },
      },
    ],
  },
]);
