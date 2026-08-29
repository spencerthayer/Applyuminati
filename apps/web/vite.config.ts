/**
 * Vite configuration for the Applyuminati local web client.
 *
 * Two decisions here are load-bearing:
 *
 * 1. `base: "./"` emits document-relative asset URLs. In production the
 *    FastAPI container serves this bundle from the same origin as the API, and
 *    we do not know in advance whether it is mounted at `/`, at `/applyuminati`
 *    behind a reverse proxy, on `localhost:8000`, or on some LAN hostname.
 *    Relative asset URLs make the built bundle location-independent, so the
 *    same `dist/` works everywhere with no rebuild and no baked-in host.
 *    (This is why the router uses hash routing — see `src/App.tsx`.)
 *
 * 2. The dev proxy is the *only* place a backend host is ever named. The
 *    application code always fetches relative `/api/v1/...` URLs, so in dev the
 *    proxy supplies the origin and in production the same origin already does.
 *    There is deliberately no `VITE_API_URL`: an env var baked at build time
 *    would defeat (1).
 */

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

/**
 * Where `vite dev` forwards `/api` requests. Affects the dev server only and is
 * never present in the production bundle. Docker dev compose sets this to
 * `http://api:8000`.
 */
const DEV_PROXY_TARGET = process.env.APPLYUMINATI_DEV_PROXY_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  base: "./",
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: DEV_PROXY_TARGET,
        changeOrigin: true,
      },
    },
  },
  preview: {
    port: 4173,
    strictPort: true,
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
  test: {
    environment: "jsdom",
    globals: false,
    setupFiles: ["./vitest.setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
    restoreMocks: true,
  },
});
