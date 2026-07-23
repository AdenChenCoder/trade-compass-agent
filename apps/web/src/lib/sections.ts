import type { TurnSection } from "@/lib/types";

function isJsonContent(content: string): boolean {
  const trimmed = content.trim();
  if (
    (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
    (trimmed.startsWith("[") && trimmed.endsWith("]"))
  ) {
    try {
      JSON.parse(trimmed);
      return true;
    } catch {
      return false;
    }
  }
  return false;
}

function isVisibleSection(section: TurnSection): boolean {
  if (section.kind === "tool" || section.kind === "json" || section.kind === "raw") {
    return false;
  }
  if (section.kind === "summary" || section.kind === "narrative") {
    return true;
  }
  return !isJsonContent(section.content);
}

export function filterVisibleSections(
  sections: TurnSection[] | undefined,
): TurnSection[] {
  if (!sections?.length) return [];
  return sections.filter(isVisibleSection);
}
