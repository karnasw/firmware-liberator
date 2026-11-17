// The website's build: the repository's Markdown (src/site.mjs) rendered in
// the layout under src/layouts, written out as files for the Worker.
import { appendFileSync, copyFileSync, globSync, mkdirSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, passthroughImageService } from "astro/config";
import { satteri } from "@astrojs/markdown-satteri";
import sitemap from "@astrojs/sitemap";
import { PICTURES, ROOT, SITE, pages, retired, rewrite } from "./src/site.mjs";
import { llmsFigures, loadStats } from "./src/stats.mjs";

const onSite = pages();

// Each link and picture in a page, rewritten for the site (src/site.mjs).
const links = ({ fileURL }) => {
  const from = relative(ROOT, fileURLToPath(fileURL));
  const visit = (node, ctx) => {
    const url = rewrite(node.url, from, onSite);
    if (url !== node.url) ctx.setProperty(node, "url", url);
  };
  return { name: "site-links", link: visit, definition: visit, image: visit };
};

// The pictures the pages show, beside them as in the repository.
const pictures = {
  name: "site-pictures",
  hooks: {
    "astro:build:done": ({ dir }) => {
      for (const path of globSync(PICTURES, { cwd: ROOT })) {
        const to = join(fileURLToPath(dir), path);
        mkdirSync(dirname(to), { recursive: true });
        copyFileSync(join(ROOT, path), to);
      }
    },
  },
};

// llms.txt, written by hand in public/, with the headline figures after it
// (src/stats.mjs) while they are fresh: never a stale figure without its date.
const figures = {
  name: "llms-figures",
  hooks: {
    "astro:build:done": async ({ dir }) => {
      const section = llmsFigures(await loadStats());
      if (section) {
        appendFileSync(join(fileURLToPath(dir), "llms.txt"), `\n${section}`);
      }
    },
  },
};

export default defineConfig({
  site: SITE,
  build: { format: "file" },
  image: { service: passthroughImageService() },
  markdown: { processor: satteri({ mdastPlugins: [links] }) },
  integrations: [sitemap({ filter: (page) => !retired(new URL(page).pathname) }), pictures, figures],
});
