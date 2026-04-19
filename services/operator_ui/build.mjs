// esbuild one-file build for the operator dashboard.
// Output lands in ../l1_preprocessing/operator_static/ so FastAPI can serve
// it directly as StaticFiles. CI does not run this — the committed output
// is the source of truth for the Python service.

import * as esbuild from "esbuild";
import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = resolve(__dirname, "../l1_preprocessing/operator_static");

mkdirSync(OUT_DIR, { recursive: true });

const watch = process.argv.includes("--watch");

const buildOptions = {
  entryPoints: [resolve(__dirname, "src/main.tsx")],
  bundle: true,
  outfile: resolve(OUT_DIR, "operator.js"),
  format: "iife",
  target: ["es2020"],
  jsx: "automatic",
  jsxImportSource: "preact",
  loader: { ".css": "text" },
  minify: !watch,
  sourcemap: watch ? "inline" : false,
  logLevel: "info",
  define: {
    "process.env.NODE_ENV": watch ? '"development"' : '"production"',
  },
};

function copyStatics() {
  // index.html is a template — the FastAPI route inlines DASHBOARD_API_KEY
  // at request time. We still copy a build-time copy for sanity checking.
  copyFileSync(
    resolve(__dirname, "src/index.html"),
    resolve(OUT_DIR, "index.html"),
  );
  copyFileSync(
    resolve(__dirname, "src/styles/tokens.css"),
    resolve(OUT_DIR, "tokens.css"),
  );
  // Emit a build marker so operators can verify which commit is live.
  const rev = process.env.GIT_SHA || "dev";
  writeFileSync(
    resolve(OUT_DIR, "build.json"),
    JSON.stringify({ rev, built_at: new Date().toISOString() }, null, 2) + "\n",
  );
  console.log(`[operator-ui] statics copied to ${OUT_DIR}`);
}

if (watch) {
  const ctx = await esbuild.context(buildOptions);
  await ctx.watch();
  copyStatics();
  console.log("[operator-ui] watch mode — rebuilds on src changes");
} else {
  await esbuild.build(buildOptions);
  copyStatics();
  console.log(`[operator-ui] build complete → ${OUT_DIR}`);
}
