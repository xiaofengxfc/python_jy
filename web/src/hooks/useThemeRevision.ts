import { useEffect, useState } from "react";

/** Re-renders canvas consumers when the theme attribute flips, so charts can
 * re-read their colours from the CSS variables. */
export function useThemeRevision(): number {
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    const observer = new MutationObserver(() => setRevision((value) => value + 1));
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    return () => observer.disconnect();
  }, []);

  return revision;
}

/** Reads a design token from :root so canvas drawing never hardcodes a colour
 * that would break in the other theme. */
export function cssVar(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
  return value || fallback;
}
