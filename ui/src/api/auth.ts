/**
 * Dashboard session credentials (admin token + optional agent key).
 *
 * Storage rule: memory + sessionStorage ONLY. Never localStorage (it outlives
 * the browser session and is shared by every tab forever) and never cookies
 * (sent automatically, so a CSRF vector). Clearing the tab clears the
 * credentials. Values are never logged or rendered; only booleans and the
 * non-secret agent id resolved from GET /me leave this module.
 */
import { useSyncExternalStore } from "react";

export type Identity = "admin" | "agent";

/** What the dashboard knows about whether the backend accepts our credentials. */
export type ConnectionStatus =
  /** No request has been answered yet. */
  | "unknown"
  /** The backend answered an admin request with 2xx. */
  | "connected"
  /** The backend answered an admin request with 401/403. */
  | "rejected";

export interface AuthSnapshot {
  hasAdminToken: boolean;
  hasAgentKey: boolean;
  status: ConnectionStatus;
  /** The agent key was refused (401) on an agent-identity request. */
  agentKeyRejected: boolean;
  /** Non-secret agent id the agent key authenticates as (from GET /me), if probed. */
  agentId: string | null;
  /** Bumped whenever credentials change, so pages can refetch. */
  epoch: number;
}

const ADMIN_KEY = "mcpr.adminToken";
const AGENT_KEY = "mcpr.agentKey";

function readSession(key: string): string | null {
  try {
    return window.sessionStorage.getItem(key);
  } catch {
    return null; // storage blocked: memory-only for this tab
  }
}

function writeSession(key: string, value: string | null): void {
  try {
    if (value) window.sessionStorage.setItem(key, value);
    else window.sessionStorage.removeItem(key);
  } catch {
    /* storage blocked: memory-only */
  }
}

let adminToken: string | null = null;
let agentKey: string | null = null;
let snapshot: AuthSnapshot = {
  hasAdminToken: false,
  hasAgentKey: false,
  status: "unknown",
  agentKeyRejected: false,
  agentId: null,
  epoch: 0,
};
const listeners = new Set<() => void>();

function emit(patch: Partial<AuthSnapshot>): void {
  snapshot = { ...snapshot, ...patch };
  listeners.forEach((l) => l());
}

/** Load credentials from sessionStorage into memory (called once at startup and by tests). */
export function hydrateAuth(): void {
  adminToken = readSession(ADMIN_KEY);
  agentKey = readSession(AGENT_KEY);
  snapshot = {
    hasAdminToken: !!adminToken,
    hasAgentKey: !!agentKey,
    status: "unknown",
    agentKeyRejected: false,
    agentId: null,
    epoch: snapshot.epoch + 1,
  };
  listeners.forEach((l) => l());
}

/**
 * Update credentials. `undefined` leaves a credential unchanged; `null` or ""
 * clears it. Bumps the epoch so mounted pages refetch under the new identity.
 */
export function setCredentials(next: { adminToken?: string | null; agentKey?: string | null }): void {
  const adminChanged = next.adminToken !== undefined;
  const agentChanged = next.agentKey !== undefined;
  if (adminChanged) {
    adminToken = next.adminToken?.trim() || null;
    writeSession(ADMIN_KEY, adminToken);
  }
  if (agentChanged) {
    agentKey = next.agentKey?.trim() || null;
    writeSession(AGENT_KEY, agentKey);
  }
  emit({
    hasAdminToken: !!adminToken,
    hasAgentKey: !!agentKey,
    status: adminChanged ? "unknown" : snapshot.status,
    agentKeyRejected: agentChanged ? false : snapshot.agentKeyRejected,
    agentId: agentChanged ? null : snapshot.agentId,
    epoch: snapshot.epoch + 1,
  });
}

export function clearCredentials(): void {
  setCredentials({ adminToken: null, agentKey: null });
}

/**
 * The bearer to send for a request made as `identity`. An agent-identity
 * request with no agent key falls back to the admin token (the playground's
 * "run as admin" mode). Only client.ts should call this.
 */
export function bearerFor(identity: Identity): string | null {
  if (identity === "agent" && agentKey) return agentKey;
  return adminToken;
}

/** Which credential an `identity` request will actually present. */
export function effectiveIdentity(identity: Identity): Identity | "none" {
  if (identity === "agent" && agentKey) return "agent";
  return adminToken ? "admin" : "none";
}

/** Called by client.ts with the HTTP outcome of every request. */
export function noteResponse(identity: Identity, status: number): void {
  const asAgent = effectiveIdentity(identity) === "agent";
  if (status === 401 || status === 403) {
    if (asAgent) {
      // 403 on an agent call is a policy answer, not a bad key; only 401 means the key is wrong.
      if (status === 401 && !snapshot.agentKeyRejected) emit({ agentKeyRejected: true });
    } else if (snapshot.status !== "rejected") emit({ status: "rejected" });
    return;
  }
  if (status >= 200 && status < 300) {
    if (asAgent) {
      if (snapshot.agentKeyRejected) emit({ agentKeyRejected: false });
    } else if (snapshot.status !== "connected") emit({ status: "connected" });
  }
}

export function setAgentId(agentId: string | null): void {
  if (snapshot.agentId !== agentId) emit({ agentId });
}

export function getAuthSnapshot(): AuthSnapshot {
  return snapshot;
}

export function subscribeAuth(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useAuth(): AuthSnapshot {
  return useSyncExternalStore(subscribeAuth, getAuthSnapshot, getAuthSnapshot);
}

/** True for the statuses that mean "the backend didn't accept our credentials". */
export function isAuthStatus(status: number): boolean {
  return status === 401 || status === 403;
}
