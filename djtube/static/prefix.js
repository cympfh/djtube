export const PUBLIC_PREFIX = "/djtube";

export function publicPrefix() {
  if (typeof document === "undefined") return PUBLIC_PREFIX;
  const meta = document.querySelector('meta[name="djtube-prefix"]');
  const value = meta?.getAttribute("content")?.trim() || "";
  if (value.startsWith("/")) return value.replace(/\/$/, "") || PUBLIC_PREFIX;
  return PUBLIC_PREFIX;
}
