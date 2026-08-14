# Re-scan regions missing from state (e.g. after a bug fix). Resumable.
$ErrorActionPreference = "Continue"
$root = "E:\dev\acres-per-pound"
$slugs = python -c @"
import json, pathlib
s = json.load(open('snapshots/state.json', encoding='utf-8'))
rows = s['listings'].values()
covered = {r['region_name'] for r in rows}
regs = json.load(open('data/regions.json', encoding='utf-8'))
missing = [r['slug'] for r in regs if r['name'] not in covered]
print('\n'.join(missing))
"@
if ($LASTEXITCODE -ne 0) { Write-Output "failed to compute missing regions"; exit 1 }
if ([string]::IsNullOrWhiteSpace(($slugs -join ""))) { Write-Output "nothing missing"; exit 0 }
$list = @($slugs)
$total = $list.Count
$i = 0
foreach ($slug in $list) {
    $i++
    Write-Output "=== [$i/$total] $slug $(Get-Date -Format HH:mm:ss) ==="
    python -m acres_per_pound.cli run-once --regions $slug 2>&1 | Select-Object -Last 1
}
Write-Output "RESCAN DONE $(Get-Date -Format HH:mm:ss)"
