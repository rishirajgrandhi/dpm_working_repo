// Formatting only. NO verdict or threshold logic (04 §1, 12 §7).
//
// A lint rule bans comparison against thresholds in this directory, and a test asserts
// the UI renders a FAIL purely from the API's status field (14 §6b).

export function duration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

export function shortSha(sha: string, length = 7): string {
  return sha.slice(0, length);
}

export function titleCase(value: string): string {
  return value.replace(/[._-]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
