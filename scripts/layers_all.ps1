# Build the full family-suitability layers (resumable - every source cached)
$ErrorActionPreference = "Continue"
Set-Location "E:\dev\acres-per-pound"
python -m acres_per_pound.cli layers *>&1 | Tee-Object -FilePath "$env:TEMP\layers_all.log"
Write-Output "LAYERS DONE $(Get-Date -Format HH:mm:ss)"
