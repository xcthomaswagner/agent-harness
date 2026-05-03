// esbuild one-file build for the operator dashboard.
// Output lands in ../l1_preprocessing/operator_static/ so FastAPI can serve
// it directly as StaticFiles. CI does not run this — the committed output
// is the source of truth for the Python service.
//
// CSS handling: esbuild's default CSS handling injects styles via a
// runtime fetch from the JS bundle, which doesn't match our
// file-served-static deployment. We instead use esbuild's standalone
// CSS bundler on a single entry (src/index.css) that imports every
// component CSS via plain CSS @import statements. The result is one
// operator.css file that operator.html <link>s directly.

import * as esbuild from "esbuild";
import { copyFileSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = resolve(__dirname, "../l1_preprocessing/operator_static");

mkdirSync(OUT_DIR, { recursive: true });

const watch = process.argv.includes("--watch");

const jsBuild = {
  entryPoints: [resolve(__dirname, "src/main.tsx")],
  bundle: true,
  outfile: resolve(OUT_DIR, "operator.js"),
  format: "iife",
  target: ["es2020"],
  jsx: "automatic",
  jsxImportSource: "preact",
  // Component TS files that do ``import "./whatever.css"`` need the CSS
  // to be a no-op at JS-bundle time (we bundle CSS separately below).
  // ``empty`` loader turns the CSS import into nothing, so the JS
  // bundle stays pure JS.
  loader: { ".css": "empty" },
  minify: !watch,
  sourcemap: watch ? "inline" : false,
  logLevel: "info",
  define: {
    "process.env.NODE_ENV": watch ? '"development"' : '"production"',
  },
};

// Single CSS entry that @imports every component's stylesheet in the
// order they must cascade. esbuild resolves the @imports and emits one
// concatenated file.
const cssBuild = {
  entryPoints: [resolve(__dirname, "src/styles/index.css")],
  bundle: true,
  outfile: resolve(OUT_DIR, "operator.css"),
  minify: !watch,
  sourcemap: watch ? "inline" : false,
  logLevel: "info",
};

function copyStatics() {
  copyFileSync(
    resolve(__dirname, "src/index.html"),
    resolve(OUT_DIR, "index.html"),
  );
  // tokens.css is kept as a standalone file in the output too, in case
  // someone wants to consume just the tokens (e.g., a future embed of a
  // single primitive on another page). Not referenced by index.html.
  copyFileSync(
    resolve(__dirname, "src/styles/tokens.css"),
    resolve(OUT_DIR, "tokens.css"),
  );
  const rev = process.env.GIT_SHA || "dev";
  writeFileSync(
    resolve(OUT_DIR, "build.json"),
    JSON.stringify({ rev, built_at: new Date().toISOString() }, null, 2) + "\n",
  );
  console.log(`[operator-ui] statics copied to ${OUT_DIR}`);
}

if (watch) {
  const jsCtx = await esbuild.context(jsBuild);
  const cssCtx = await esbuild.context(cssBuild);
  await jsCtx.watch();
  await cssCtx.watch();
  copyStatics();
  console.log("[operator-ui] watch mode — rebuilds on src changes");
} else {
  await Promise.all([esbuild.build(jsBuild), esbuild.build(cssBuild)]);
  copyStatics();
  console.log(`[operator-ui] build complete → ${OUT_DIR}`);
}
