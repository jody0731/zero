/* Rounds component web UI: session list badge. */

export function register(host) {
  host.sessionBadge((row) => (row.rounds ? "rounds" : null));
}
