[Diagnostics.CodeAnalysis.SuppressMessageAttribute("PSAvoidUsingPlainTextForPassword", "GuestCredentialFile", Justification = "This parameter is a protected credential file path, never a password value.")]
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$VmName,

    [Parameter(Mandatory)]
    [guid]$VmUuid,

    [Parameter(Mandatory)]
    [string]$GuestCredentialFile,

    [Parameter(Mandatory)]
    [string]$ScreenshotPath,

    [switch]$RepairSsh,

    [string]$GuestUser = "actor"
)

$ErrorActionPreference = "Stop"

if (-not $IsWindows) { throw "Console fingerprint capture requires Windows" }
if ($VmName -notmatch '^[A-Za-z0-9._-]+$') { throw "VM name is invalid" }
if ($GuestUser -notmatch '^[a-z_][a-z0-9_-]*$') { throw "Guest user is invalid" }
if (-not [IO.Path]::IsPathFullyQualified($GuestCredentialFile) -or
    -not [IO.Path]::IsPathFullyQualified($ScreenshotPath)) {
    throw "Console capture paths must be absolute"
}
if (-not (Test-Path -LiteralPath $GuestCredentialFile -PathType Leaf)) {
    throw "Guest credential file is missing"
}

$vbox = Get-Command VBoxManage.exe -ErrorAction SilentlyContinue
if (-not $vbox) { throw "VBoxManage.exe is missing" }
$vmInfo = & $vbox.Source showvminfo $VmName --machinereadable
if ($LASTEXITCODE -ne 0) { throw "Cannot read the requested VM" }
$uuidLine = $vmInfo | Where-Object { $_ -match '^UUID="([0-9A-Fa-f-]+)"$' } | Select-Object -First 1
$actualUuid = [guid]([regex]::Match($uuidLine, '^UUID="([0-9A-Fa-f-]+)"$').Groups[1].Value)
if ($actualUuid -ne $VmUuid) { throw "Registered VM UUID does not match the expected identity" }
$stateLine = $vmInfo | Where-Object { $_ -match '^VMState="(.+)"$' } | Select-Object -First 1
$vmState = [regex]::Match($stateLine, '^VMState="(.+)"$').Groups[1].Value
if ($vmState -ne "running") { throw "VM must be running for console fingerprint capture" }

$scanCodes = @{
    '1'='02'; '2'='03'; '3'='04'; '4'='05'; '5'='06'; '6'='07'; '7'='08'; '8'='09'; '9'='0a'; '0'='0b'
    '-'='0c'; '='='0d'; 'q'='10'; 'w'='11'; 'e'='12'; 'r'='13'; 't'='14'; 'y'='15'; 'u'='16'; 'i'='17'
    'o'='18'; 'p'='19'; '['='1a'; ']'='1b'; 'a'='1e'; 's'='1f'; 'd'='20'; 'f'='21'; 'g'='22'; 'h'='23'
    'j'='24'; 'k'='25'; 'l'='26'; ';'='27'; "'"='28'; '`'='29'; '\'='2b'; 'z'='2c'; 'x'='2d'; 'c'='2e'
    'v'='2f'; 'b'='30'; 'n'='31'; 'm'='32'; ','='33'; '.'='34'; '/'='35'; ' '='39'
}
$shifted = @{
    '!'='1'; '@'='2'; '#'='3'; '$'='4'; '%'='5'; '^'='6'; '&'='7'; '*'='8'; '('='9'; ')'='0'
    '_'='-'; '+'='='; '{'='['; '}'=']'; ':'=';'; '"'="'"; '~'='`'; '|'='\'; '<'=','; '>'='.'; '?'='/'
}

function Send-ScanCodeSequence {
    param([Parameter(Mandatory)][string[]]$Codes)
    & $vbox.Source controlvm $VmName keyboardputscancode @Codes | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "VirtualBox keyboard injection failed" }
}

function Send-Character {
    param([Parameter(Mandatory)][char]$Character)
    $text = [string]$Character
    $shift = $false
    $base = $text
    if ($text -cmatch '^[A-Z]$') {
        $shift = $true
        $base = $text.ToLowerInvariant()
    } elseif ($shifted.ContainsKey($text)) {
        $shift = $true
        $base = $shifted[$text]
    }
    if (-not $scanCodes.ContainsKey($base)) { throw "Guest credential contains an unsupported console character" }
    $make = $scanCodes[$base]
    $break = '{0:x2}' -f (([Convert]::ToInt32($make, 16)) -bor 0x80)
    if ($shift) { Send-ScanCodeSequence @('2a') }
    Send-ScanCodeSequence @($make, $break)
    if ($shift) { Send-ScanCodeSequence @('aa') }
    Start-Sleep -Milliseconds 40
}

function Send-Text {
    param([Parameter(Mandatory)][string]$Text)
    foreach ($character in $Text.ToCharArray()) { Send-Character $character }
}

function Send-Enter {
    Send-ScanCodeSequence @('1c', '9c')
    Start-Sleep -Milliseconds 250
}

function Save-ConsoleScreenshot {
    param([Parameter(Mandatory)][string]$Name)
    $path = Join-Path (Split-Path -Parent $ScreenshotPath) $Name
    & $vbox.Source controlvm $VmName screenshotpng $path
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Cannot capture staged console screenshot"
    }
}

$password = (Get-Content -LiteralPath $GuestCredentialFile -Raw).TrimEnd("`r", "`n")
if ([string]::IsNullOrWhiteSpace($password)) { throw "Guest credential file is empty" }

# Switch to tty3, terminate any stale command, and log out any prior capture session.
Send-ScanCodeSequence @('1d', '38', '3d', 'bd', 'b8', '9d')
Start-Sleep -Seconds 3
New-Item -ItemType Directory -Path (Split-Path -Parent $ScreenshotPath) -Force | Out-Null
Send-ScanCodeSequence @('1d', '2e', 'ae', '9d')
Send-ScanCodeSequence @('1d', '20', 'a0', '9d')
Start-Sleep -Seconds 2
Send-ScanCodeSequence @('1d', '2e', 'ae', '9d')
Start-Sleep -Seconds 1
Save-ConsoleScreenshot 'login-ready.png'
Send-Text $GuestUser
Send-Enter
Start-Sleep -Seconds 2
Save-ConsoleScreenshot 'password-prompt.png'
Send-Text $password
Send-Enter
Start-Sleep -Seconds 4
Save-ConsoleScreenshot 'authentication-result.png'
Send-Text "clear"
Send-Enter
Send-Text "ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256"
Send-Enter
Start-Sleep -Seconds 3

& $vbox.Source controlvm $VmName screenshotpng $ScreenshotPath
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $ScreenshotPath -PathType Leaf)) {
    throw "Cannot capture console fingerprint screenshot"
}

Send-Text "clear"
Send-Enter
Send-Text "systemctl is-active ssh; systemctl is-enabled ssh; ss -ltn | grep ':22 '; journalctl -u ssh -n 10 --no-pager"
Send-Enter
Start-Sleep -Seconds 4
Save-ConsoleScreenshot 'ssh-service-console.png'
if ($RepairSsh) {
    Send-Text "sudo -S env DEBIAN_FRONTEND=noninteractive sh -c 'dpkg --configure -a && apt-get install -y openssh-server && systemctl enable --now ssh'; systemctl is-active ssh; ss -ltn | grep ':22 '"
    Send-Enter
    Start-Sleep -Seconds 2
    Send-Text $password
    Send-Enter
    Start-Sleep -Seconds 120
    Save-ConsoleScreenshot 'ssh-repair-console.png'
}
Send-ScanCodeSequence @('1d', '20', 'a0', '9d')