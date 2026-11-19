# Update the Deadline Cloud Houdini Submitter from GitHub (Windows)
# Run as Administrator (PowerShell 5.1 or 7.x)
#
# This build is based on upstream 0.7.14 of the Houdini submitter. Install/upgrade the
# official Deadline Cloud Submitter to the matching 0.7.14 base first, then run this
# script to apply the ODS customizations (husk render path, render-output path mapping,
# UNC to drive-letter asset fix, <LATEST> token refs, Conda queue-env fixes).
#
# The same tag also carries the adaptor (houdini_handler.py). Workers get it from the
# queue environment, which builds from this tag. This script only updates the workstation.
#
# Usage:
#   .\update_deadline_houdini_from_github.ps1
#   .\update_deadline_houdini_from_github.ps1 -InstallRoot D:\test\Houdini   # dry run into a test folder
#   .\update_deadline_houdini_from_github.ps1 -GitRef 0.7.14-ods3
param(
    [string]$InstallRoot = "C:\Program Files\DeadlineCloudSubmitter\Submitters\Houdini",
    [string]$GitRef = "0.7.14-ods3"
)
# ========== CONFIG ==========
$repoOwner = "oehmen"
$repoName  = "deadline-cloud-for-houdini"
$srcBase   = "src/deadline/houdini_submitter"
# repo file -> path relative to $InstallRoot
$itemsToCopy = [ordered]@{
    "$srcBase/python/deadline_cloud_for_houdini/submitter.py"                  = "python\deadline_cloud_for_houdini\submitter.py"
    "$srcBase/python/deadline_cloud_for_houdini/_assets.py"                    = "python\deadline_cloud_for_houdini\_assets.py"
    "$srcBase/python/deadline_cloud_for_houdini/hip_settings.py"               = "python\deadline_cloud_for_houdini\hip_settings.py"
    "$srcBase/python/deadline_cloud_for_houdini/houdini_submitter_widget.py"   = "python\deadline_cloud_for_houdini\houdini_submitter_widget.py"
    "$srcBase/python/deadline_cloud_for_houdini/queue_parameters.py"           = "python\deadline_cloud_for_houdini\queue_parameters.py"
    "$srcBase/otls/deadline_cloud.hda/Driver_1deadline__cloud/DialogScript"    = "otls\deadline_cloud.hda\Driver_1deadline__cloud\DialogScript"
}
$panelPlaceholder = "# __SUBMITTER_PANEL_SOURCE__"
# ========== SCRIPT SETUP ==========
$ErrorActionPreference = "Stop"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
# Force TLS 1.2 (PS 5.1 may default to older TLS)
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$UpdatedFiles = @()
if (-not (Test-Path -LiteralPath $InstallRoot)) {
    throw "Install root not found: $InstallRoot. Install the official Deadline Cloud Submitter 0.7.14 first."
}
function Get-RepoText {
    param([string]$RepoFilePath)
    $rawUrl = "https://raw.githubusercontent.com/$repoOwner/$repoName/$GitRef/$RepoFilePath"
    Write-Host "  [DOWNLOAD] $rawUrl"
    $webClient = New-Object System.Net.WebClient
    $webClient.Headers.Add("User-Agent", "powershell-deadline-update-script")
    $webClient.Encoding = [System.Text.Encoding]::UTF8
    return $webClient.DownloadString($rawUrl)
}
function Write-WithBackup {
    param([string]$Destination, [string]$Content, [string]$RepoPath)
    $destDir = Split-Path -Path $Destination -Parent
    if (-not (Test-Path -LiteralPath $destDir)) {
        New-Item -ItemType Directory -Path $destDir -Force | Out-Null
    }
    $backup = $null
    if (Test-Path -LiteralPath $Destination) {
        $backup = "$Destination.bak-$timestamp"
        Write-Host "  [BACKUP] $Destination -> $backup"
        Copy-Item -LiteralPath $Destination -Destination $backup -Force
    }
    # UTF-8 without BOM, same as the repo files
    [System.IO.File]::WriteAllText($Destination, $Content, (New-Object System.Text.UTF8Encoding($false)))
    $pycache = Join-Path (Split-Path $Destination -Parent) "__pycache__"
    if ($Destination -like "*.py" -and (Test-Path -LiteralPath $pycache)) {
        $stem = [System.IO.Path]::GetFileNameWithoutExtension($Destination)
        Remove-Item -Path (Join-Path $pycache "$stem.*") -Force -ErrorAction SilentlyContinue
    }
    $script:UpdatedFiles += [PSCustomObject]@{ RepoPath = $RepoPath; Destination = $Destination; Backup = $backup }
}
# ========== SYNC FILES ==========
foreach ($repoPath in $itemsToCopy.Keys) {
    Write-Host ""
    Write-Host "Processing: $repoPath"
    Write-Host "----------------------------------"
    $content = Get-RepoText -RepoFilePath $repoPath
    Write-WithBackup -Destination (Join-Path $InstallRoot $itemsToCopy[$repoPath]) -Content $content -RepoPath $repoPath
}
# ========== PYTHON PANEL ==========
# Houdini loads the rendered .pypanel, which EMBEDS panel/submitter_panel.py. Copying the .py
# alone has no effect, so render the panel the same way the installer does.
Write-Host ""
Write-Host "Processing: python panel (render template + submitter_panel.py)"
Write-Host "----------------------------------"
$template = Get-RepoText -RepoFilePath "$srcBase/python_panels/deadline_cloud.pypanel.template"
$panelSrc = Get-RepoText -RepoFilePath "$srcBase/panel/submitter_panel.py"
if (-not $template.Contains($panelPlaceholder)) {
    throw "Placeholder '$panelPlaceholder' not found in the pypanel template at $GitRef."
}
$rendered = $template.Replace($panelPlaceholder, $panelSrc)
Write-WithBackup -Destination (Join-Path $InstallRoot "python_panels\deadline_cloud.pypanel") -Content $rendered -RepoPath "$srcBase/python_panels/deadline_cloud.pypanel.template + panel/submitter_panel.py"
# ========== SUMMARY ==========
Write-Host ""
Write-Host "======================================="
Write-Host "Update summary (ref: $GitRef)"
Write-Host "======================================="
foreach ($item in $UpdatedFiles) {
    Write-Host ""
    Write-Host "Repo file    : $($item.RepoPath)"
    Write-Host "Installed at : $($item.Destination)"
    if ($item.Backup) { Write-Host "Backup       : $($item.Backup)" }
    else { Write-Host "Backup       : <none - new file>" }
}
Write-Host ""
Write-Host "Done. Restart Houdini. To undo, copy the .bak-$timestamp files back over the originals."
