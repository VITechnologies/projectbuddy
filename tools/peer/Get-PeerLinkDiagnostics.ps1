<#
Read-only diagnostics for the ProjectBuddy peer link (VITechnologies/projectbuddy#211, P0).
Changes nothing on this machine. Run the file in an elevated Windows PowerShell:
    powershell -NoProfile -ExecutionPolicy Bypass -File .\Get-PeerLinkDiagnostics.ps1 [-OutputDirectory <folder>] [-CopyToClipboard]
Writes peer-diag-<timestamp>.txt to -OutputDirectory (default: Desktop\peer-diag). The file holds
private network details; never put it in a public repository.
#>
param(
    [string]$OutputDirectory,
    [switch]$CopyToClipboard
)

$ErrorActionPreference = 'Stop'
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path ([Environment]::GetFolderPath('Desktop')) 'peer-diag'
}
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$outFile = Join-Path $OutputDirectory "peer-diag-$stamp.txt"
$lines = New-Object System.Collections.Generic.List[string]

function Add-Section {
    param([string]$Title, [scriptblock]$Body)
    $lines.Add('')
    $lines.Add("===== $Title =====")
    try {
        $text = & $Body 2>&1 | Out-String -Width 400
        $lines.Add($text.TrimEnd())
    } catch {
        $lines.Add("ERROR: $($_.Exception.Message)")
    }
}

function Invoke-Native {
    param([string]$Path, [string[]]$Arguments)
    $cmd = Get-Command $Path -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cmd) { return "not found: $Path" }
    # Windows PowerShell 5.1 turns redirected native stderr into a terminating error under Stop.
    $ErrorActionPreference = 'Continue'
    & $cmd.Source @Arguments 2>&1 | ForEach-Object { "$_" }
    "exit code: $LASTEXITCODE"
}

$lines.Add("peer-link diagnostics, schema 1, generated $(Get-Date -Format o)")

Add-Section 'Identity and elevation' {
    $os = Get-CimInstance Win32_OperatingSystem
    "OS: $($os.Caption) $($os.Version) build $($os.BuildNumber)"
    "Last boot: $($os.LastBootUpTime)"
    "PowerShell: $($PSVersionTable.PSVersion)"
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    "Elevated: $($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))"
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    try {
        $admins = Get-LocalGroupMember -SID 'S-1-5-32-544' | Select-Object -ExpandProperty Name
        "Current user is in local Administrators: $($admins -contains $user)"
    } catch {
        "Current user is in local Administrators: unknown ($($_.Exception.Message))"
    }
}

Add-Section 'Network adapters (all, including hidden)' {
    Get-NetAdapter -IncludeHidden | Sort-Object Name |
        Format-Table Name, InterfaceDescription, ifIndex, Status, MediaConnectionState, LinkSpeed, FullDuplex, MacAddress -AutoSize
}

Add-Section 'Wired adapter power management and advanced properties' {
    $wired = Get-NetAdapter -Physical | Where-Object { $_.MediaType -eq '802.3' }
    foreach ($a in $wired) {
        "--- $($a.Name) ($($a.InterfaceDescription))"
        Get-NetAdapterPowerManagement -Name $a.Name -ErrorAction SilentlyContinue | Format-List *
        Get-NetAdapterAdvancedProperty -Name $a.Name -ErrorAction SilentlyContinue | Format-Table DisplayName, DisplayValue -AutoSize
    }
}

Add-Section 'IP interfaces (DHCP, metric, forwarding)' {
    Get-NetIPInterface | Sort-Object AddressFamily, InterfaceAlias |
        Format-Table InterfaceAlias, ifIndex, AddressFamily, Dhcp, ConnectionState, InterfaceMetric, Forwarding, NlMtu -AutoSize
}

Add-Section 'IP addresses' {
    Get-NetIPAddress | Sort-Object AddressFamily, InterfaceAlias |
        Format-Table InterfaceAlias, AddressFamily, IPAddress, PrefixLength, PrefixOrigin, AddressState -AutoSize
}

Add-Section 'Routes (IPv4 and IPv6, all interfaces)' {
    Get-NetRoute | Sort-Object AddressFamily, DestinationPrefix |
        Format-Table AddressFamily, DestinationPrefix, NextHop, InterfaceAlias, RouteMetric, Protocol -AutoSize
}

Add-Section 'VPN connections' {
    $vpn = @(Get-VpnConnection -ErrorAction SilentlyContinue) + @(Get-VpnConnection -AllUserConnection -ErrorAction SilentlyContinue)
    if ($vpn.Count -eq 0) { 'none configured' } else { $vpn | Format-Table Name, ServerAddress, ConnectionStatus, SplitTunneling -AutoSize }
    'Adapters that look virtual or VPN:'
    Get-NetAdapter -IncludeHidden | Where-Object { $_.InterfaceDescription -match 'VPN|TAP|TUN|WireGuard|Virtual|Hyper-V|VirtualBox|Fortinet|Cisco|Pangp|Zscaler|Tailscale' } |
        Format-Table Name, InterfaceDescription, Status -AutoSize
}

Add-Section 'Network connection profiles (firewall category)' {
    Get-NetConnectionProfile | Format-Table Name, InterfaceAlias, NetworkCategory, IPv4Connectivity, IPv6Connectivity -AutoSize
}

Add-Section 'Firewall profiles' {
    Get-NetFirewallProfile | Format-Table Name, Enabled, DefaultInboundAction, DefaultOutboundAction, AllowLocalFirewallRules -AutoSize
}

Add-Section 'Firewall rules touching SSH or TCP 22' {
    $ports = Get-NetFirewallPortFilter -Protocol TCP | Where-Object { $_.LocalPort -contains '22' }
    $byPort = if ($ports) { $ports | Get-NetFirewallRule } else { @() }
    $byName = Get-NetFirewallRule | Where-Object { $_.Name -match 'ssh' -or $_.DisplayName -match 'ssh' }
    $rules = @($byPort) + @($byName) | Sort-Object Name -Unique
    if ($rules.Count -eq 0) { 'none' }
    foreach ($r in $rules) {
        "--- $($r.Name) | $($r.DisplayName) | Enabled=$($r.Enabled) Direction=$($r.Direction) Action=$($r.Action) Profile=$($r.Profile)"
        $addr = $r | Get-NetFirewallAddressFilter
        $if = $r | Get-NetFirewallInterfaceFilter
        $port = $r | Get-NetFirewallPortFilter
        $app = $r | Get-NetFirewallApplicationFilter
        "    Remote=$($addr.RemoteAddress -join ',') Local=$($addr.LocalAddress -join ',') Interface=$($if.InterfaceAlias -join ',') Port=$($port.LocalPort -join ',') Program=$($app.Program)"
    }
}

Add-Section 'OpenSSH capability, services and version' {
    Get-WindowsCapability -Online -Name 'OpenSSH*' | Format-Table Name, State -AutoSize
    Get-Service -Name sshd, ssh-agent -ErrorAction SilentlyContinue | Format-Table Name, Status, StartType -AutoSize
    $sshd = Join-Path $env:WINDIR 'System32\OpenSSH\sshd.exe'
    if (Test-Path $sshd) {
        "sshd.exe file version: $((Get-Item $sshd).VersionInfo.ProductVersion)"
        Invoke-Native $sshd @('-V')
    } else { "absent: $sshd" }
    $ssh = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
    if (Test-Path $ssh) { Invoke-Native $ssh @('-V') }
}

Add-Section 'sshd configuration (non-secret)' {
    $cfg = Join-Path $env:ProgramData 'ssh\sshd_config'
    if (Test-Path $cfg) { Get-Content $cfg | Where-Object { $_ -notmatch '^\s*$' } } else { "absent: $cfg" }
}

Add-Section 'SSH key and host-key files: presence and ACLs only (no contents)' {
    $dir = Join-Path $env:ProgramData 'ssh'
    if (-not (Test-Path $dir)) { "absent: $dir"; return }
    Get-ChildItem $dir -File | Format-Table Name, Length, LastWriteTime -AutoSize
    $ak = Join-Path $dir 'administrators_authorized_keys'
    if (Test-Path $ak) {
        "administrators_authorized_keys non-empty lines: $(@(Get-Content $ak | Where-Object { $_.Trim() }).Count)"
        Invoke-Native 'icacls.exe' @($ak)
    } else { 'administrators_authorized_keys: absent' }
    Get-ChildItem $dir -Filter 'ssh_host_*_key.pub' | ForEach-Object {
        $kg = Join-Path $env:WINDIR 'System32\OpenSSH\ssh-keygen.exe'
        if (Test-Path $kg) { Invoke-Native $kg @('-l', '-f', $_.FullName) }
    }
}

Add-Section 'Listening TCP ports of interest (22, 11434, lbabus)' {
    Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 22, 11434 -or $_.OwningProcess -in (Get-Process -Name lbabus -ErrorAction SilentlyContinue).Id } |
        ForEach-Object { "{0}:{1} pid={2} {3}" -f $_.LocalAddress, $_.LocalPort, $_.OwningProcess, (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName }
}

Add-Section 'Power and sleep (cable link stability)' {
    Invoke-Native 'powercfg.exe' @('/a')
    Invoke-Native 'powercfg.exe' @('/getactivescheme')
    Invoke-Native 'powercfg.exe' @('/query', 'SCHEME_CURRENT', 'SUB_SLEEP', 'STANDBYIDLE')
    Invoke-Native 'powercfg.exe' @('/query', 'SCHEME_CURRENT', 'SUB_SLEEP', 'HIBERNATEIDLE')
}

Add-Section 'Recent wired link events (last 6 hours)' {
    $since = (Get-Date).AddHours(-6)
    Get-WinEvent -FilterHashtable @{ LogName = 'System'; StartTime = $since } -ErrorAction SilentlyContinue |
        Where-Object { $_.Message -match 'link|disconnect|connected' -and $_.ProviderName -notmatch 'Kernel-Power|DCOM' } |
        Select-Object -First 40 |
        ForEach-Object { '{0:o} {1} id={2} {3}' -f $_.TimeCreated, $_.ProviderName, $_.Id, (($_.Message -split "`r?`n")[0]) }
}

Add-Section 'VirtualBox (counts only; VM names stay on this machine)' {
    $ErrorActionPreference = 'Continue'
    $vbm = Join-Path $env:ProgramFiles 'Oracle\VirtualBox\VBoxManage.exe'
    if (-not (Test-Path $vbm)) { "absent: $vbm"; return }
    "version: $(& $vbm --version)"
    "registered VMs (this user): $(@(& $vbm list vms).Count)"
    "running VMs (this user): $(@(& $vbm list runningvms).Count)"
    $pbPrefix = @(& $vbm list vms | Where-Object { $_ -match '^"(projectbuddy|pb-)' }).Count
    "VMs named with a projectbuddy or pb- prefix: $pbPrefix"
    Get-Process -Name VBoxSVC, VBoxSDS -IncludeUserName -ErrorAction SilentlyContinue | Format-Table Name, Id, UserName -AutoSize
}

Add-Section 'VirtualBox registries of all local profiles (counts only)' {
    Get-CimInstance Win32_UserProfile | Where-Object { -not $_.Special -and $_.LocalPath } | ForEach-Object {
        $profileDir = Get-Item -LiteralPath $_.LocalPath -ErrorAction SilentlyContinue
        if (-not $profileDir) { return }
        $xml = Join-Path $profileDir.FullName '.VirtualBox\VirtualBox.xml'
        if (Test-Path -LiteralPath $xml) {
            [xml]$doc = Get-Content -LiteralPath $xml -Raw
            $sources = @($doc.VirtualBox.Global.MachineRegistry.MachineEntry | Where-Object { $_ -and $_.src } | ForEach-Object { $_.src })
            $pb = @($sources | Where-Object { (Split-Path $_ -Leaf) -match '^(projectbuddy|pb-)' }).Count
            $drives = ($sources | ForEach-Object { if ($_ -match '^([A-Za-z]):') { $Matches[1].ToUpper() } else { '?' } } | Sort-Object -Unique) -join ','
            '{0}: registered={1} projectbuddy-prefixed={2} drives={3}' -f $profileDir.Name, $sources.Count, $pb, $drives
        } else {
            '{0}: no VirtualBox registry' -f $profileDir.Name
        }
    }
}

Add-Section 'Ollama' {
    Invoke-Native 'ollama' @('--version')
    Invoke-Native 'ollama' @('list')
    "OLLAMA_HOST (user): $([Environment]::GetEnvironmentVariable('OLLAMA_HOST', 'User'))"
    "OLLAMA_HOST (machine): $([Environment]::GetEnvironmentVariable('OLLAMA_HOST', 'Machine'))"
    "OLLAMA_MODELS (user): $([Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'User'))"
}

Add-Section '.NET SDKs and runtimes' {
    Invoke-Native 'dotnet' @('--list-sdks')
    Invoke-Native 'dotnet' @('--list-runtimes')
}

Add-Section 'Free space on fixed volumes' {
    Get-Volume | Where-Object { $_.DriveType -eq 'Fixed' -and $_.DriveLetter } |
        Format-Table DriveLetter, FileSystemLabel, FileSystem, @{ n = 'FreeGB'; e = { [math]::Round($_.SizeRemaining / 1GB, 1) } }, @{ n = 'SizeGB'; e = { [math]::Round($_.Size / 1GB, 1) } } -AutoSize
}

$lines | Set-Content -Path $outFile -Encoding UTF8
if ($CopyToClipboard) {
    Get-Content -LiteralPath $outFile -Raw | Set-Clipboard
    Write-Host "Done: $outFile (also copied to the clipboard)"
} else {
    Write-Host "Done: $outFile"
}
