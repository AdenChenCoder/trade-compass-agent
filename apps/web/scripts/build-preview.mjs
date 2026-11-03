import { copyFile, mkdir, mkdtemp, readdir, rename, rm, access } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "vite";

export async function publishPreview(staged, destination) {
  await access(path.join(staged, "index.html"));
  async function publishFile(relative) {
    const target = path.join(destination, relative);
    await mkdir(path.dirname(target), { recursive: true });
    const temporary = `${target}.${randomUUID()}.tmp`;
    try {
      await copyFile(path.join(staged, relative), temporary);
      await rename(temporary, target);
    } finally {
      await rm(temporary, { force: true });
    }
  }
  async function publishAssets(directory = "") {
    for (const entry of await readdir(path.join(staged, directory), { withFileTypes: true })) {
      const relative = path.join(directory, entry.name);
      if (relative === "index.html") continue;
      if (entry.isDirectory()) await publishAssets(relative);
      else if (entry.isFile()) await publishFile(relative);
    }
  }
  await publishAssets();
  // Existing tabs still import their original hashed chunks. Publish HTML last.
  await publishFile("index.html");
}

export async function buildPreview() {
  const webRoot = fileURLToPath(new URL("..", import.meta.url));
  const temporaryRoot = path.resolve(webRoot, "../../temp");
  await mkdir(temporaryRoot, { recursive: true });
  const staged = await mkdtemp(path.join(temporaryRoot, "web-build-"));
  const destination = path.join(temporaryRoot, "web-preview");
  try {
    await build({ root: webRoot, build: { outDir: staged, emptyOutDir: true } });
    await publishPreview(staged, destination);
    console.log(`Preview published to ${destination}`);
  } finally {
    await rm(staged, { recursive: true, force: true });
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await buildPreview();
}
