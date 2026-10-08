import { useEffect, useState } from "react";

const QUERY = "(prefers-color-scheme: dark)";

/** Tracks the OS colour-scheme preference; "light" when matchMedia is absent (tests). */
export function useColorScheme(): "light" | "dark" {
  const get = () =>
    typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia(QUERY).matches
      ? "dark"
      : "light";
  const [scheme, setScheme] = useState<"light" | "dark">(get);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mql = window.matchMedia(QUERY);
    const onChange = () => setScheme(mql.matches ? "dark" : "light");
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, []);
  return scheme;
}
