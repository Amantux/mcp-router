/**
 * The build's version (D6). The image passes VITE_APP_VERSION as a Docker build arg
 * (from pyproject.toml); a plain `npm run build` falls back to package.json's version
 * (vite.config.ts), which a lockstep test keeps equal to pyproject.toml.
 */
export function appVersion(): string | undefined {
  return import.meta.env.VITE_APP_VERSION || undefined;
}
