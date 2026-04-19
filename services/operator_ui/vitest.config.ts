import { defineConfig } from "vitest/config";

export default defineConfig({
  // Vitest 4 ships with oxc by default; configure JSX there rather than on
  // esbuild so both code paths don't fire during transform.
  oxc: {
    jsx: {
      runtime: "automatic",
      importSource: "preact",
    },
  },
  resolve: {
    alias: {
      react: "preact/compat",
      "react-dom": "preact/compat",
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    css: false,
    include: ["src/**/__tests__/**/*.test.ts?(x)"],
  },
});
