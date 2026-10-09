// Once-per-session auto-redirect into the setup wizard. Only a non-secret
// marker lives in sessionStorage; credentials never touch it.
export const REDIRECT_KEY = "mcpr.setup.autoRedirect";
export const STEP_KEY = "mcpr.setup.step"; // progress only; never a secret

export function redirectClaimed(): boolean {
  try {
    return sessionStorage.getItem(REDIRECT_KEY) !== null;
  } catch {
    return true; // no storage: never redirect (cannot guarantee "once")
  }
}

/** Mark the session as already redirected; returns true if this call claimed it. */
export function claimRedirect(): boolean {
  if (redirectClaimed()) return false;
  try {
    sessionStorage.setItem(REDIRECT_KEY, "1");
    return true;
  } catch {
    return false;
  }
}

/** "Skip setup": forget wizard progress and never auto-redirect again this session. */
export function skipSetup(): void {
  try {
    sessionStorage.setItem(REDIRECT_KEY, "skipped");
    sessionStorage.removeItem(STEP_KEY);
  } catch {
    /* storage unavailable: nothing to clear */
  }
}
