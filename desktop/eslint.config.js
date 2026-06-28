import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default [
  {
    ignores: ["dist/**", "dist-electron/**", "node_modules/**", "eslint.config.js", "vite.config.ts", "electron/devLauncher.cjs"]
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    languageOptions: {
      parserOptions: {
        project: ["./tsconfig.json", "./tsconfig.electron.json"]
      }
    }
  }
];
