#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'AgentA2A'),
    [string]$TraeMcpConfig = (Join-Path $env:APPDATA 'Trae CN\User\mcp.json'),
    [string]$TraeSkillsDir = (Join-Path $env:USERPROFILE '.trae-cn\skills'),
    [string]$OrcaSkillsDir = (Join-Path $env:USERPROFILE '.agents\skills'),
    [string]$GeminiModel,
    [switch]$NoPath
)

$ErrorActionPreference = 'Stop'
$installDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($InstallDir)
if ($installDir -eq [IO.Path]::GetPathRoot($installDir)) {
    throw 'Choose an installation directory below the drive root.'
}
$installDir = $installDir.TrimEnd('\', '/')
$TraeMcpConfig = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($TraeMcpConfig)
$TraeSkillsDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($TraeSkillsDir)
$OrcaSkillsDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OrcaSkillsDir)
$appDir = Join-Path $installDir 'app'
$binDir = Join-Path $installDir 'bin'
$stageId = [Guid]::NewGuid().ToString('N')
$stageDir = Join-Path $installDir ".stage-$stageId"
$previousDir = Join-Path $installDir ".previous-$stageId"
$tokenFile = Join-Path $installDir 'relay-token'
$configFile = Join-Path $installDir 'config.json'
$toolFile = Join-Path $appDir 'tools\trae_gemini_mcp.py'
$serverName = 'agent-a2a-bridge'
$skillMarker = '<!-- Installed by trae-gemini-a2a. -->'
$utf8 = New-Object Text.UTF8Encoding($false)

function Read-JsonObject([string]$File) {
    if (!(Test-Path -LiteralPath $File)) {
        return [pscustomobject]@{}
    }
    $parsed = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($File))
    if ($parsed -isnot [pscustomobject]) {
        throw "Expected a JSON object in $File."
    }
    return $parsed
}

function Set-JsonProperty([pscustomobject]$Object, [string]$Name, $Value) {
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        $Object | Add-Member -MemberType NoteProperty -Name $Name -Value $Value
    } else {
        $property.Value = $Value
    }
}

function Write-Utf8File([string]$File, [string]$Content) {
    $directory = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($File))
    [void][IO.Directory]::CreateDirectory($directory)
    if ([IO.File]::Exists($File) -and [IO.File]::ReadAllText($File) -ceq $Content) {
        return
    }

    $temporary = Join-Path $directory ('.agent-a2a-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temporary, $Content, $utf8)
        if ([IO.File]::Exists($File)) {
            [IO.File]::Replace($temporary, $File, [NullString]::Value)
        } else {
            [IO.File]::Move($temporary, $File)
        }
    } finally {
        [IO.File]::Delete($temporary)
    }
}

function Update-TraeConfig([switch]$Write) {
    $settings = Read-JsonObject $TraeMcpConfig
    if ($null -eq $settings.PSObject.Properties['mcpServers']) {
        Set-JsonProperty $settings 'mcpServers' ([pscustomobject]@{})
    }
    if ($settings.mcpServers -isnot [pscustomobject]) {
        throw "Expected mcpServers to be an object in $TraeMcpConfig."
    }

    $server = $settings.mcpServers.PSObject.Properties[$serverName]
    if ($null -eq $server) {
        $entry = [pscustomobject]@{}
        Set-JsonProperty $settings.mcpServers $serverName $entry
    } else {
        $entry = $server.Value
        if ($entry -isnot [pscustomobject] -or $entry.args -notcontains $toolFile -or $entry.args -notcontains '--managed') {
            throw "The existing TRAE MCP entry '$serverName' belongs to another installation in $TraeMcpConfig."
        }
    }

    $settingsEnv = if ($null -eq $entry.env) { [pscustomobject]@{} } else { $entry.env }
    if ($settingsEnv -isnot [pscustomobject]) {
        throw "Expected an env object in the TRAE MCP entry '$serverName'."
    }
    Set-JsonProperty $settingsEnv 'RUN_MCP_TIMEOUT_MS' '120000'
    Set-JsonProperty $settingsEnv 'AGENT_A2A_HOME' $installDir
    Set-JsonProperty $settingsEnv 'AGENT_A2A_TRAE_WORKSPACE' '${workspaceFolder}'
    Set-JsonProperty $entry 'command' $python
    Set-JsonProperty $entry 'args' @('-B', $toolFile, '--managed')
    Set-JsonProperty $entry 'env' $settingsEnv
    if ($Write) {
        Write-Utf8File $TraeMcpConfig ((ConvertTo-Json -InputObject $settings -Depth 100) + [Environment]::NewLine)
    }
}

function Update-ManagedFile([string]$Source, [string]$Destination, [switch]$AllowMarker, [switch]$Write) {
    $desired = [IO.File]::ReadAllText($Source)
    if ([IO.File]::Exists($Destination)) {
        $current = [IO.File]::ReadAllText($Destination)
        $sameContents = ($current -replace '\r(?=\n)', '') -ceq ($desired -replace '\r(?=\n)', '')
        if (!$sameContents -and !($AllowMarker -and $current.Contains($skillMarker))) {
            throw "An unmanaged file already exists at $Destination."
        }
    }
    if ($Write) {
        Write-Utf8File $Destination $desired
    }
}

function Add-PathEntry([string]$Current, [string]$Directory) {
    $windowsSeparator = [char]92
    $entry = $Directory.TrimEnd($windowsSeparator)
    foreach ($part in ($Current -split ';')) {
        if ([Environment]::ExpandEnvironmentVariables($part.Trim(' ', '"')).TrimEnd($windowsSeparator) -eq $entry) {
            return $Current
        }
    }
    if ([string]::IsNullOrWhiteSpace($Current)) {
        return $Directory
    }
    return "$Directory;$Current"
}

function Remove-InstallScratch([string]$Directory) {
    $fullPath = [IO.Path]::GetFullPath($Directory)
    if ([IO.Path]::GetDirectoryName($fullPath) -ne $installDir -or
        [IO.Path]::GetFileName($fullPath) -notmatch '^\.(stage|previous)-[0-9a-f]{32}$') {
        throw "Refusing to remove directory outside the installer scratch paths: $Directory"
    }
    if (Test-Path -LiteralPath $fullPath) {
        if (([IO.File]::GetAttributes($fullPath) -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Refusing to remove a link at the installer scratch path: $fullPath"
        }
        Remove-Item -LiteralPath $fullPath -Recurse -Force
    }
}

$python = (Get-Command python.exe -CommandType Application | Select-Object -First 1).Source
$node = (Get-Command node.exe -CommandType Application | Select-Object -First 1).Source
$npm = (Get-Command npm.cmd -CommandType Application | Select-Object -First 1).Source
& $python -B -c 'import sys; sys.exit(sys.version_info < (3, 10))'
if ($LASTEXITCODE -ne 0) {
    throw 'Python 3.10 or later is required.'
}
& $node -e "if(typeof require('node:module').registerHooks !== 'function') process.exit(1)"
if ($LASTEXITCODE -ne 0) {
    throw 'Node.js 22.15 or later with module.registerHooks is required.'
}

$traeSkillSource = Join-Path $PSScriptRoot 'skills\trae-gemini-bridge\SKILL.md'
$traeSkillTarget = Join-Path $TraeSkillsDir 'trae-gemini-bridge\SKILL.md'
$orcaSkillSource = Join-Path $PSScriptRoot 'skills\agent-a2a\SKILL.md'
$orcaSkillTarget = Join-Path $OrcaSkillsDir 'agent-a2a\SKILL.md'
$geminiAgentSource = Join-Path $PSScriptRoot 'gemini\agents\trae-bridge.md'
$geminiHome = if ($env:GEMINI_CLI_HOME) { $env:GEMINI_CLI_HOME } else { $env:USERPROFILE }
$geminiAgentTarget = Join-Path $geminiHome '.gemini\agents\trae-bridge.md'

$payload = @(
    'LICENSE',
    'package.json',
    'package-lock.json',
    'tools\agent_a2a.py',
    'tools\trae_a2a_gateway.py',
    'tools\trae_gemini_mcp.py',
    'node\gemini-loopback.mjs',
    'node\run-gemini-a2a.mjs'
)
$payload += @(Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'tools') -File -Filter '*.py' |
    ForEach-Object { Join-Path 'tools' $_.Name })
$payload += @(Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'node') -File -Filter '*.mjs' |
    ForEach-Object { Join-Path 'node' $_.Name })
$payload = $payload | Sort-Object -Unique
$sourceHashes = [ordered]@{}
foreach ($relative in $payload) {
    $sourceHashes[$relative] = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot $relative) -Algorithm SHA256).Hash
}
$payloadManifest = ConvertTo-Json -InputObject $sourceHashes -Compress
$appMarker = Join-Path $appDir '.agent-a2a-payload.json'

[void][IO.Directory]::CreateDirectory($installDir)
$installLock = [IO.File]::Open((Join-Path $installDir 'install.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
try {
    Update-TraeConfig
    Update-ManagedFile $traeSkillSource $traeSkillTarget -AllowMarker
    Update-ManagedFile $orcaSkillSource $orcaSkillTarget -AllowMarker
    Update-ManagedFile $geminiAgentSource $geminiAgentTarget

    if ((Test-Path -LiteralPath $appDir) -and !(Test-Path -LiteralPath $appMarker -PathType Leaf)) {
        throw "The existing application directory is not managed by this installer: $appDir"
    }
    $payloadCurrent = [IO.File]::Exists($appMarker) -and [IO.File]::ReadAllText($appMarker) -ceq $payloadManifest
    if ($payloadCurrent) {
        foreach ($relative in $payload) {
            $installedFile = Join-Path $appDir $relative
            if (!(Test-Path -LiteralPath $installedFile -PathType Leaf) -or
                (Get-FileHash -LiteralPath $installedFile -Algorithm SHA256).Hash -cne $sourceHashes[$relative]) {
                $payloadCurrent = $false
                break
            }
        }
        $dependencies = (Read-JsonObject (Join-Path $appDir 'package.json')).dependencies
        foreach ($dependency in $dependencies.PSObject.Properties) {
            $manifest = Join-Path $appDir ("node_modules\" + $dependency.Name.Replace('/', '\') + '\package.json')
            if (!(Test-Path -LiteralPath $manifest -PathType Leaf) -or
                (Read-JsonObject $manifest).version -ne $dependency.Value) {
                $payloadCurrent = $false
                break
            }
        }
    }

    if (!$payloadCurrent) {
        foreach ($relative in $payload) {
            $destination = Join-Path $stageDir $relative
            [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination))
            Copy-Item -LiteralPath (Join-Path $PSScriptRoot $relative) -Destination $destination
        }
        Push-Location -LiteralPath $stageDir
        try {
            & $npm ci --ignore-scripts --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) {
                throw "npm ci exited with code $LASTEXITCODE."
            }
        } finally {
            Pop-Location
        }
        Write-Utf8File (Join-Path $stageDir '.agent-a2a-payload.json') $payloadManifest

        if (Test-Path -LiteralPath $appDir) {
            [IO.Directory]::Move($appDir, $previousDir)
        }
        try {
            [IO.Directory]::Move($stageDir, $appDir)
        } catch {
            if (Test-Path -LiteralPath $previousDir) {
                [IO.Directory]::Move($previousDir, $appDir)
            }
            throw
        }
        Remove-InstallScratch $previousDir
    }

    if (![IO.File]::Exists($tokenFile)) {
        $token = New-Object byte[] 32
        $random = [Security.Cryptography.RandomNumberGenerator]::Create()
        try {
            $random.GetBytes($token)
            Write-Utf8File $tokenFile ([BitConverter]::ToString($token).Replace('-', '').ToLowerInvariant())
        } finally {
            $random.Dispose()
        }
    }
    $runtimeConfig = Read-JsonObject $configFile
    Set-JsonProperty $runtimeConfig 'node' $node
    if ($PSBoundParameters.ContainsKey('GeminiModel')) {
        if ([string]::IsNullOrWhiteSpace($GeminiModel)) {
            $runtimeConfig.PSObject.Properties.Remove('gemini_model')
        } else {
            Set-JsonProperty $runtimeConfig 'gemini_model' $GeminiModel.Trim()
        }
    }
    Write-Utf8File $configFile ((ConvertTo-Json -InputObject $runtimeConfig -Depth 100) + [Environment]::NewLine)

    $pythonForCmd = $python.Replace('%', '%%')
    $launcher = @"
@echo off
setlocal DisableDelayedExpansion
for %%I in ("%~dp0..") do set "AGENT_A2A_HOME=%%~fI"
"$pythonForCmd" -B "%AGENT_A2A_HOME%\app\tools\agent_a2a.py" %*
exit /b %errorlevel%
"@
    Write-Utf8File (Join-Path $binDir 'a2a.cmd') (($launcher -replace '\r?\n', [Environment]::NewLine) + [Environment]::NewLine)

    Update-TraeConfig -Write
    Update-ManagedFile $traeSkillSource $traeSkillTarget -AllowMarker -Write
    Update-ManagedFile $orcaSkillSource $orcaSkillTarget -AllowMarker -Write
    Update-ManagedFile $geminiAgentSource $geminiAgentTarget -Write

    if (!$NoPath) {
        $current = [Environment]::GetEnvironmentVariable('Path', 'User')
        $updated = Add-PathEntry $current $binDir
        if ($updated -cne $current) {
            [Environment]::SetEnvironmentVariable('Path', $updated, 'User')
        }
        $env:Path = Add-PathEntry $env:Path $binDir
    }
} finally {
    try {
        Remove-InstallScratch $stageDir
    } finally {
        $installLock.Dispose()
    }
}

Write-Host "Installed: $(Join-Path $binDir 'a2a.cmd')"
Write-Host "TRAE: $serverName MCP and /trae-gemini-bridge"
Write-Host "Gemini: trae-bridge remote subagent"
Write-Host "Orca/Codex: agent-a2a skill"
