# One-off: full INSPIRE enrichment (E&W). Resumable - cached zips/pkls are skipped.
$ErrorActionPreference = "Continue"
$root = "E:\dev\acres-per-pound"
Set-Location $root
python -m acres_per_pound.cli enrich --all *>&1 | Tee-Object -FilePath "$env:TEMP\enrich_all.log"
Write-Output "ENRICH ALL DONE $(Get-Date -Format HH:mm:ss)"
