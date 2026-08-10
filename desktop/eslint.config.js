import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default [
  {
    ignores: [
      "dist/**",
      "dist-electron/**",
      "node_modules/**",
      "eslint.config.js",
      "vite.config.ts",
      "electron/devLauncher.cjs",
      // Standalone headed HIL orchestration is checked with `node --check`;
      // it is plain ESM rather than part of a TypeScript project.
      "hardware-e2e/**",
      // This standalone runner launches the already-built executable with
      // Playwright Electron and is likewise syntax-checked by Node directly.
      "e2e/packagedSmoke.mjs"
    ]
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    languageOptions: {
      parserOptions: {
        project: ["./tsconfig.json", "./tsconfig.electron.json", "./tsconfig.e2e.json"]
      }
    }
  }
];
