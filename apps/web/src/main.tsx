import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router-dom";
import { Toaster } from "sonner";
import { FreshnessProvider } from "@/components/ui/new-badge";
import { router } from "@/router";
import "@/index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <FreshnessProvider>
        <RouterProvider router={router} />
        <Toaster position="bottom-right" richColors closeButton />
      </FreshnessProvider>
    </QueryClientProvider>
  </StrictMode>,
);
