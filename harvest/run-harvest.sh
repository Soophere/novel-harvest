#!/bin/sh
# The FLAT SERVER is the primary harvester and now runs THREE SITES AT ONCE.
#
# Six cores and a wired line: one site at 6 req/s was leaving five sixths of the box idle. Each site still
# has exactly one owner (its own Drive lease), which is the rule; working three different sites at the same
# time breaks nothing and triples the pace.
#
# Knobs, all in one place:
#   HARVEST_RATE  requests per second per site  (6 is the highest that did not get a 429 from galaxynovels)
#   THREADS       chapters in flight per site
#   BUDGET        minutes before a site hands its lease back and the next group starts
#
# Stop it from the laptop:   ssh home-zt "pct exec 100 -- systemctl stop harvest-runner"
# Start it again:            ssh home-zt "pct exec 100 -- systemctl start harvest-runner"

export HARVEST_RATE=${HARVEST_RATE:-6}
export HARVEST_WORK=/opt/harvest/work
THREADS=${THREADS:-8}
BUDGET=${BUDGET:-25}

cd /opt/harvest || exit 1
mkdir -p "$HARVEST_WORK" out

# 1. the box has no IPv6 route, and rclone resolves googleapis.com to AAAA first
if [ "$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null)" != "1" ]; then
  sysctl -qw net.ipv6.conf.all.disable_ipv6=1 2>/dev/null || true
fi
# 2. one nameserver on a lossy line failed 3 lookups in 5
if [ "$(grep -c nameserver /etc/resolv.conf 2>/dev/null)" -lt 2 ]; then
  printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\nnameserver 9.9.9.9\noptions timeout:2 attempts:1 rotate\n' > /etc/resolv.conf
fi

one() {
  site="$1"
  log="/opt/harvest/out/$site.log"
  [ -f "$log" ] && [ "$(wc -c < "$log")" -gt 5000000 ] && mv "$log" "$log.1"
  echo "=== $(date -u +%FT%TZ) $site ===" >> "$log"
  PYTHONPATH=harvest python3 harvest/runner.py --site "$site" --budget-min "$BUDGET" \
    --threads "$THREADS" --wait-min 1 --only >> "$log" 2>&1
}

while true; do
  for group in "galaxynovels novltime sunovels" "wuxiadreams fanmtl wuxiaworldsite" "novelfire"; do
    for s in $group; do one "$s" & done
    wait
  done
  sleep 20
done
