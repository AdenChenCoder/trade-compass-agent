// @vitest-environment node
import { afterEach, expect, it } from "vitest";
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { publishPreview } from "./build-preview.mjs";

const temporaryRoot = fileURLToPath(new URL("../../../temp/", import.meta.url));
const roots = [];
async function sandbox() {
  await mkdir(temporaryRoot, { recursive: true });
  const root = await mkdtemp(path.join(temporaryRoot, "preview-test-"));
  roots.push(root);
  return root;
}
async function bundle(root, version) {
  const staged = path.join(root, version);
  await mkdir(path.join(staged, "assets"), { recursive: true });
  await writeFile(path.join(staged, "index.html"), `<script type="module" src="/assets/entry-${version}.js"></script>`);
  await writeFile(path.join(staged, `assets/entry-${version}.js`), `import('./skill-${version}.js')`);
  await writeFile(path.join(staged, `assets/skill-${version}.js`), `export default '${version}'`);
  return staged;
}
afterEach(async () => { await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true }))); });

it("keeps an open page's lazy imports available while new pages use the new entry", async () => {
  const root = await sandbox();
  const destination = path.join(root, "preview");
  await publishPreview(await bundle(root, "old"), destination);
  const oldEntry = await readFile(path.join(destination, "assets/entry-old.js"), "utf8");
  await publishPreview(await bundle(root, "new"), destination);
  expect(await readFile(path.join(destination, "index.html"), "utf8")).toContain("entry-new.js");
  expect(oldEntry).toContain("skill-old.js");
  expect(await readFile(path.join(destination, "assets/skill-old.js"), "utf8")).toContain("'old'");
  expect(await readFile(path.join(destination, "assets/skill-new.js"), "utf8")).toContain("'new'");
  expect((await readdir(destination)).some(name => name.endsWith(".tmp"))).toBe(false);
});

it("leaves the published entry usable when the next build cannot be published", async () => {
  const root = await sandbox();
  const destination = path.join(root, "preview");
  await publishPreview(await bundle(root, "working"), destination);
  const broken = await bundle(root, "broken");
  await mkdir(path.join(destination, "assets/skill-broken.js"));
  await expect(publishPreview(broken, destination)).rejects.toThrow();
  expect(await readFile(path.join(destination, "index.html"), "utf8")).toContain("entry-working.js");
  expect(await readFile(path.join(destination, "assets/skill-working.js"), "utf8")).toContain("'working'");
  expect((await readdir(path.join(destination, "assets"))).some(name => name.endsWith(".tmp"))).toBe(false);
});
