param([Parameter(Mandatory=$true)][string]$StopFile, [Parameter(Mandatory=$true)][string]$OutputFile)
$ErrorActionPreference = 'Stop'
$samples = [Collections.Generic.List[object]]::new()
while (-not (Test-Path -LiteralPath $StopFile)) {
    $processes = @(Get-Process -Name chrome -ErrorAction SilentlyContinue)
    $samples.Add([ordered]@{
        utc = [DateTime]::UtcNow.ToString('o')
        process_count = $processes.Count
        total_working_set_bytes = [long](($processes | Measure-Object WorkingSet64 -Sum).Sum)
        total_private_bytes = [long](($processes | Measure-Object PrivateMemorySize64 -Sum).Sum)
    })
    Start-Sleep -Seconds 1
}
$report = [ordered]@{
    method = 'Windows Get-Process chrome WorkingSet64 and PrivateMemorySize64, summed across all Chrome processes each second'
    scope = 'All Chrome processes, including pre-existing unrelated tabs and extension processes; not isolated tab memory'
    metric = 'sampled maximum, not process lifetime peak'
    samples = $samples.ToArray()
}
[IO.File]::WriteAllText($OutputFile, ($report | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
