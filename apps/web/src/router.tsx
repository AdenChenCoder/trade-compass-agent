import { Navigate, createBrowserRouter } from "react-router-dom";
import { AppLayout } from "@/components/layout/AppLayout";
import { AgentPage } from "@/routes/AgentPage";
import { AuditPage } from "@/routes/AuditPage";
import { JobsPage } from "@/routes/JobsPage";
import { MemoryPage } from "@/routes/MemoryPage";
import { PortfolioPage } from "@/routes/PortfolioPage";
import { RulesPage } from "@/routes/RulesPage";
import { SettingsPage } from "@/routes/SettingsPage";
import { SkillsPage } from "@/routes/SkillsPage";

export const router = createBrowserRouter([
  {
    element: <AppLayout />,
    children: [
      { index: true, element: <Navigate to="/agent" replace /> },
      { path: "/agent", element: <AgentPage /> },
      { path: "/portfolio", element: <PortfolioPage /> },
      { path: "/memory", element: <MemoryPage /> },
      { path: "/audit", element: <AuditPage /> },
      { path: "/rules", element: <RulesPage /> },
      { path: "/skills", element: <SkillsPage /> },
      { path: "/jobs", element: <JobsPage /> },
      { path: "/settings", element: <SettingsPage /> },
    ],
  },
]);
