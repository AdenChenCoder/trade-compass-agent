import { useSyncExternalStore } from "react";
import { getAppearance, subscribeAppearance } from "../../../shared/appearance";

export function useAppearance() {
  return useSyncExternalStore(subscribeAppearance, getAppearance);
}
