import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it } from "vitest";
import { MarkdownRenderer } from "./MarkdownRenderer";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

it("each report citation navigates to its own source, including repeated source numbers", async () => {
  const element = document.createElement("div"); document.body.append(element);
  const root = createRoot(element);
  try {
    await act(async () => root.render(<>
      <article><MarkdownRenderer>{"第一份报告[^source-1]\n\n[^source-1]: 证券甲，10元。"}</MarkdownRenderer></article>
      <article><MarkdownRenderer>{"第二份报告[^source-1]\n\n[^source-1]: 证券乙，20元。"}</MarkdownRenderer></article>
    </>));
    const links = element.querySelectorAll<HTMLAnchorElement>("a[data-footnote-ref]");
    expect(links).toHaveLength(2);
    expect(links[0].hash).not.toBe(links[1].hash);
    for (const [index, link] of Array.from(links).entries()) {
      const target = document.getElementById(link.hash.slice(1));
      expect(target?.closest("article")).toBe(link.closest("article"));
      expect(target?.textContent).toContain(index === 0 ? "证券甲，10元" : "证券乙，20元");
    }
  } finally {
    await act(async () => root.unmount()); element.remove();
  }
});
