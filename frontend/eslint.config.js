import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import jsxA11y from "eslint-plugin-jsx-a11y";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "coverage", "playwright-report", "test-results"] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
      "jsx-a11y": jsxA11y,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // React Compiler rules from eslint-plugin-react-hooks 7. This app does not
      // build with the compiler, and existing code (recording pipeline, crop
      // step) uses refs and effect-driven resets on purpose. Warn, so new code
      // is nudged without forcing a risky rewrite of the recorder.
      "react-hooks/refs": "warn",
      "react-hooks/set-state-in-effect": "warn",
      "react-hooks/immutability": "warn",
      "react-hooks/preserve-manual-memoization": "warn",
      ...jsxA11y.flatConfigs.recommended.rules,
      "react-refresh/only-export-components": [
        "warn",
        { allowConstantExport: true },
      ],
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],

    },
  },
  // Node-context config files.
  {
    files: ["*.config.{js,ts}"],
    languageOptions: { globals: globals.node },
  },
  // Test files and helpers export non-component values by design.
  {
    files: ["**/*.test.{ts,tsx}", "src/test/**", "src/components/ui/**"],
    rules: {
      "react-refresh/only-export-components": "off",
    },
  },
);
