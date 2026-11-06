#!/bin/sh
# The FLAT SERVER is the primary harvester. It is the faster box and it owns the sites while its line is up.
#
# GitHub Actions is the backup only: it can take a site when this loop is stopped or the line is down long
# enough for the 20-minute Drive lease to expire, and then it resumes from the same chapter. The lease in
# novels/state/<site>-0of1.json is the only thing that decides who may work a site.
#
# --budget-min 20 : twenty minutes per site per lap, so all seven move every hour instead of one running
#                   for 45 minutes while six wait.
# --threads 16  : sixteen chapters in flight; the per-site rate cap in runner.SITES keeps it polite.
# --wait-min 2  : if the backup is mid-novel here, skip this site and come back next lap.
# HARVEST_WORK  : chapters already written stay on this disk, so a restart never re-downloads them.

# --- two host problems this container has, healed on every start -------------------------------------
# 1. rclone resolves googleapis.com to IPv6 and there is no IPv6 route, so every Drive call fails with
#    "network is unreachable". Turning IPv6 off makes it resolve A records only.
if [ "$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null)" != "1" ]; then
  sysctl -qw net.ipv6.conf.all.disable_ipv6=1 2>/dev/null || true
  sysctl -qw net.ipv6.conf.default.disable_ipv6=1 2>/dev/null || true
fi
# 2. one nameserver on a line that drops packets failed 3 lookups in 5. Rotate over three.
if [ "$(grep -c nameserver /etc/resolv.conf 2>/dev/null)" -lt 2 ]; then
  printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\nnameserver 9.9.9.9\noptions timeout:2 attempts:1 rotate\n' > /etc/resolv.conf
fi

# Data budget. A chapter page is ~55 KB on the wire for ~6 KB kept, so the rate IS the data bill:
#   6 req/s  ~ 1.2 GB/hour      2 req/s  ~ 400 MB/hour      1 req/s  ~ 200 MB/hour
# Set HARVEST_RATE in this file to change it; stop the unit entirely with:  systemctl stop harvest-runner
export HARVEST_RATE=2
export HARVEST_WORK=/opt/harvest/work
cd /opt/harvest || exit 1
mkdir -p "$HARVEST_WORK" out
LOG=/opt/harvest/out/harvest-runner.log

while true; do
  for site in galaxynovels novltime sunovels wuxiadreams fanmtl wuxiaworldsite novelfire; do
    echo "=== $(date -u +%FT%TZ) $site ===" >> "$LOG"
    PYTHONPATH=harvest python3 harvest/runner.py --site "$site" --budget-min 20 --threads 16 --wait-min 2 --only \
      >> "$LOG" 2>&1
    sleep 15
  done
  sleep 45
done
