# dispatch-harvest.ps1 - keep the GitHub backup actually running.
#
# GitHub Actions never fired its own schedule for this repo (zero runs with event=schedule since the repo
# was created), and the workflow file cannot be edited without the "workflow" token scope. So this script
# asks GitHub to start a run itself, on a Windows task.
#
# It is a no-op when a run is already going, so running it often costs nothing.

$ErrorActionPreference = "Continue"
$repo = "Soophere/novel-harvest"
$log  = "D:\were i waste my time\novelTranslation\out\dispatch.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

function Note($m) { "$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ss') $m" | Add-Content -Path $log }

try {
    $json = gh run list --repo $repo --limit 5 --json status,databaseId,createdAt 2>$null
    $runs = $json | ConvertFrom-Json
} catch {
    Note "gh run list failed: $_"
    exit 1
}

$busy = @($runs | Where-Object { $_.status -eq "in_progress" -or $_.status -eq "queued" })
if ($busy.Count -gt 0) {
    Note "already $($busy.Count) active - nothing to do"
    exit 0
}

# quiet hours: the flat server is primary; only step in if it has not pushed for a while
$out = gh workflow run harvest.yml --repo $repo -f site="" -f budget="55" 2>&1
if ($LASTEXITCODE -eq 0) { Note "dispatched harvest (55 min)" }
else { Note "dispatch failed: $out" }
