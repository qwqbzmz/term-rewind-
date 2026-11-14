param([switch]$Interactive)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$rewind = Join-Path $PSScriptRoot 'rewind.ps1'
$python = Join-Path $PSScriptRoot 'runtime\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { $python = 'python.exe' }
$demo = Join-Path $PSScriptRoot ('demo-data\' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$project = Join-Path $demo 'project'
$store = Join-Path $demo 'snapshots'
New-Item -ItemType Directory -Path $project -Force | Out-Null

$config = Join-Path $project 'config.json'
$draft = Join-Path $project 'draft.md'
$script = Join-Path $project 'clean_data.py'
[IO.File]::WriteAllText($config, "{`"mode`": `"original`"}`n")
[IO.File]::WriteAllText($draft, "An uncommitted draft.`n")
@'
from pathlib import Path

Path("config.json").write_text('{"mode": "broken"}\n', encoding="utf-8")
Path("draft.md").unlink()
Path("generated.tmp").write_bytes(b"generated\x00\xff")
print("Demo script failed after modifying files.")
raise SystemExit(7)
'@ | Set-Content -LiteralPath $script -Encoding ascii

Push-Location -LiteralPath $project
try {
    Write-Host "Demo project: $project" -ForegroundColor Cyan
    $output = & $rewind --store $store run -- $python clean_data.py
    $status = $LASTEXITCODE
    $output | Write-Host
    if ($status -ne 7) { throw "Expected the demo script to exit with code 7, got $status." }
    $id = [regex]::Match(($output -join "`n"), '(?m)^Recording ([a-f0-9]{12})').Groups[1].Value
    if (-not $id) { throw 'Could not locate the recorded command.' }

    Write-Host "`n1. Review what the failing command changed" -ForegroundColor Cyan
    & $rewind --store $store diff $id
    if ($LASTEXITCODE -ne 0) { throw 'Could not read the recorded diff.' }

    Write-Host "`n2. Edit config.json again and try undoing the old command" -ForegroundColor Cyan
    [IO.File]::WriteAllText($config, "{`"mode`": `"edited-afterward`"}`n")
    & $rewind --store $store undo $id --yes
    if ($LASTEXITCODE -ne 1 -or (Test-Path -LiteralPath $draft)) { throw 'Expected a conflict before any files were restored.' }

    if ($Interactive) {
        Write-Host "`n3. Try it yourself in the timeline" -ForegroundColor Cyan
        Write-Host 'Enter opens the command. Select draft.md, Enter previews it, U then Y restores it.'
        Write-Host 'Use Esc to go back or quit.'
        $null = Read-Host 'Press Enter to open the timeline'
        & $rewind --store $store ui
        if ($LASTEXITCODE -ne 0) { throw 'Could not open the timeline.' }
        if ((Test-Path -LiteralPath $draft) -and [IO.File]::ReadAllText($draft).Trim() -eq 'An uncommitted draft.' -and
            [IO.File]::ReadAllText($config).Trim() -eq '{"mode": "edited-afterward"}') {
            Write-Host "PASS  draft.md restored; the later config.json edit was preserved." -ForegroundColor Green
        }
        return
    }

    Write-Host "`n3. Restore only draft.md" -ForegroundColor Cyan
    & $rewind --store $store undo $id --file draft.md --yes
    if ($LASTEXITCODE -ne 0 -or [IO.File]::ReadAllText($draft).Trim() -ne 'An uncommitted draft.' -or
        [IO.File]::ReadAllText($config).Trim() -ne '{"mode": "edited-afterward"}') {
        throw 'The draft or the later config edit did not survive as expected.'
    }
    Write-Host "PASS  draft.md restored; the later config.json edit was preserved." -ForegroundColor Green

} finally {
    Pop-Location
}
