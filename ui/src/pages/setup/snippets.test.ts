import { describe, expect, it } from "vitest";
import { CLIENTS, isHttpsGitUrl, snippetFor, suggestToken } from "./snippets";

describe("setup snippets", () => {
  it("every client snippet carries the URL and the key", () => {
    for (const c of CLIENTS) {
      const s = snippetFor(c, "http://router.lan:8400/mcp", "k-123");
      expect(s, c).toContain("http://router.lan:8400/mcp");
      expect(s, c).toContain("Bearer k-123");
    }
  });
  it("accepts only https git URLs", () => {
    expect(isHttpsGitUrl("https://github.com/o/r.git")).toBe(true);
    for (const bad of ["http://x/r.git", "git@github.com:o/r.git", "file:///etc", "ssh://x/r"]) {
      expect(isHttpsGitUrl(bad), bad).toBe(false);
    }
  });
  it("suggests a 48-hex token", () => {
    expect(suggestToken()).toMatch(/^[0-9a-f]{48}$/);
  });
});
