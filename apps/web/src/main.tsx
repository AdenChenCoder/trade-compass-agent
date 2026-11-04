import { StrictMode } from "react";
import { MotionConfig } from "motion/react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router-dom";
import { Toaster } from "sonner";
import { FreshnessProvider } from "@/components/ui/new-badge";
import { router } from "@/router";
import { initializeAppearance } from "../../shared/appearance";
import { useAppearance } from "@/lib/appearance";
import "../../shared/appearance.css";
import "@/index.css";
import "@/workspace.css";
import "@/interaction.css";

initializeAppearance();

function AppearanceToaster() {
  const appearance = useAppearance();
  return <Toaster theme={appearance === "glacier" ? "dark" : "light"} position="bottom-right" richColors closeButton />;
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <MotionConfig reducedMotion="user" transition={{ duration: 0.24, ease: [0.22, 1, 0.36, 1] }}>
    <QueryClientProvider client={queryClient}>
      <FreshnessProvider>
        <RouterProvider router={router} />
        <AppearanceToaster />
      </FreshnessProvider>
    </QueryClientProvider>
    </MotionConfig>
  </StrictMode>,
);
