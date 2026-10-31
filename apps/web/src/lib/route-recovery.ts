import { createElement, type ComponentType } from "react";
import { replace } from "react-router-dom";
import { toast } from "sonner";

function isModuleLoadError(error: unknown): boolean {
  return error instanceof Error && /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module|Unable to preload CSS/i.test(error.message);
}

export function recoverRoute(load: () => Promise<{ Component: ComponentType }>, recoveryPath: () => string | null) {
  let loaded: ComponentType | null = null;
  let failureToast: string | number | undefined;
  return {
    // Failed route.lazy promises are cached by React Router. A loader keeps
    // each failed navigation recoverable, while successful modules stay cached.
    loader: async () => {
      if (loaded) return null;
      try {
        loaded = (await load()).Component;
        if (failureToast !== undefined) toast.dismiss(failureToast);
        failureToast = undefined;
        return null;
      } catch (error) {
        const path = recoveryPath();
        if (!path || !isModuleLoadError(error)) throw error;
        failureToast = toast.error("页面暂时无法打开", {
          id: failureToast,
          description: "请检查连接后重试。若刚更新应用，请先保留未发送的内容，再刷新页面。",
          duration: Infinity,
          closeButton: true,
        });
        // Keep the mounted route and its drafts; never reload the document here.
        throw replace(path);
      }
    },
    Component: () => loaded ? createElement(loaded) : null,
  };
}
