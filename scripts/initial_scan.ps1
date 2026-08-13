# Initial full UK scan: processes every region sequentially with run-once,
# accumulating into snapshots/state.json. Resumable - re-running skips
# already-scanned regions quickly thanks to the disk cache.
$ErrorActionPreference = "Continue"
$root = "E:\dev\acres-per-pound"
$regions = python -c "import json; [print(r['slug']) for r in json.load(open('data/regions.json', encoding='utf-8'))]"
if ($LASTEXITCODE -ne 0) { Write-Output "failed to list regions"; exit 1 }
$total = ($regions | Measure-Object).Count
$i = 0
foreach ($slug in $regions) {
    $i++
    Write-Output "=== [$i/$total] $slug $(Get-Date -Format HH:mm:ss) ==="
    python -m acres_per_pound.cli run-once --regions $slug 2>&1 | Select-Object -Last 2
}
Write-Output "ALL DONE $(Get-Date -Format HH:mm:ss)"
