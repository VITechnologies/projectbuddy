# escape=`
# Windows SBOM generation image: NI's official LabVIEW Windows container
# + VIPM CLI + PowerShell 7 + Git + Subversion + Grype.
# Built and run by Run-WindowsSbom.ps1; see docs/sbom-pipeline/procedure.md.
ARG LABVIEW_IMAGE=nationalinstruments/labview:latest-windows
FROM ${LABVIEW_IMAGE}

# Pinned VIPM 2026 Q3 release (26.3.2 build 4056) from docs.vipm.io/2026-Q3/installation;
# the preview channel's "latest" URL lags behind it.
ARG VIPM_INSTALLER_URL=https://traffic.libsyn.com/secure/jkinc/vipm-26.3.4056-windows-setup.exe
ARG VIPM_CLI_VERSION=26.3.2
ARG VIPM_DESKTOP_VERSION=2026.3.2.4056
ARG PWSH_VERSION=7.6.8
ARG MINGIT_TAG=v2.56.0.windows.2
ARG MINGIT_VERSION=2.56.0.2
ARG PWSH_WIN_X64_SHA256=b9f074f923c499b51ec99d313da8e9ae90d17a9f3adf00c73d6189f34d908528
ARG MINGIT_WIN_X64_SHA256=da35e72aa21c005a5a0d298cfbae110bc1609a815730ea0dde84b01a1b3cd3be
ARG SVN_MSI_URL=https://sliksvn.com/pub/Slik-Subversion-1.14.5-x64.msi
ARG SVN_MSI_SHA256=21aa09ec9db9868cd56a378c44d276643171bcf5fcd8aa4877dee60c2b756200
ARG GRYPE_VERSION=0.121.0
ARG GRYPE_WINDOWS_AMD64_SHA256=f867bbdc14bd8b0d99b3fc47d839af07480a540072b16896373a8ba32b76f588

SHELL ["powershell", "-NoLogo", "-NoProfile", "-Command", "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue';"]
COPY VipmInstallerSignature.ps1 C:/sbom/VipmInstallerSignature.ps1
COPY Install-Grype.ps1 C:/sbom/Install-Grype.ps1

RUN [Net.ServicePointManager]::SecurityProtocol = 'Tls12'; `
    New-Item -ItemType Directory -Force -Path C:\tools\download | Out-Null; `
    Invoke-WebRequest -UseBasicParsing -OutFile C:\tools\download\pwsh.zip `
        -Uri ('https://github.com/PowerShell/PowerShell/releases/download/v{0}/PowerShell-{0}-win-x64.zip' -f $env:PWSH_VERSION); `
    if ((Get-FileHash C:\tools\download\pwsh.zip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $env:PWSH_WIN_X64_SHA256) { throw 'PowerShell archive SHA-256 mismatch.' }; `
    Expand-Archive C:\tools\download\pwsh.zip -DestinationPath 'C:\Program Files\PowerShell\7'; `
    Invoke-WebRequest -UseBasicParsing -OutFile C:\tools\download\mingit.zip `
        -Uri ('https://github.com/git-for-windows/git/releases/download/{0}/MinGit-{1}-64-bit.zip' -f $env:MINGIT_TAG, $env:MINGIT_VERSION); `
    if ((Get-FileHash C:\tools\download\mingit.zip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $env:MINGIT_WIN_X64_SHA256) { throw 'MinGit archive SHA-256 mismatch.' }; `
    Expand-Archive C:\tools\download\mingit.zip -DestinationPath C:\tools\git; `
    Invoke-WebRequest -UseBasicParsing -OutFile C:\tools\download\svn.msi -Uri $env:SVN_MSI_URL; `
    if ((Get-FileHash C:\tools\download\svn.msi -Algorithm SHA256).Hash.ToLowerInvariant() -ne $env:SVN_MSI_SHA256) { throw 'Subversion MSI SHA-256 mismatch.' }; `
    $p = Start-Process msiexec.exe -Wait -PassThru -ArgumentList '/i', 'C:\tools\download\svn.msi', '/qn', '/norestart'; `
    if ($p.ExitCode -ne 0) { throw "Subversion install failed with exit code $($p.ExitCode)" }; `
    & C:\sbom\Install-Grype.ps1 -Version $env:GRYPE_VERSION -Sha256 $env:GRYPE_WINDOWS_AMD64_SHA256 -InstallDirectory C:\tools\grype; `
    Remove-Item -Recurse -Force C:\tools\download; `
    $path = [Environment]::GetEnvironmentVariable('Path', 'Machine'); `
    $path += ';C:\Program Files\PowerShell\7;C:\tools\git\cmd;C:\Program Files\SlikSvn\bin;C:\tools\grype'; `
    [Environment]::SetEnvironmentVariable('Path', $path, 'Machine')

RUN [Net.ServicePointManager]::SecurityProtocol = 'Tls12'; `
    Invoke-WebRequest -UseBasicParsing -OutFile C:\vipm-setup.exe -Uri $env:VIPM_INSTALLER_URL; `
    . C:\sbom\VipmInstallerSignature.ps1; Assert-VipmInstallerSignature -Path C:\vipm-setup.exe; `
    $p = Start-Process C:\vipm-setup.exe -Wait -PassThru -ArgumentList '/exenoui', '/qn', '/norestart'; `
    if ($p.ExitCode -notin @(0, 3010)) { throw "VIPM install failed with exit code $($p.ExitCode)" }; `
    Remove-Item -Force C:\vipm-setup.exe; `
    $vipm = Get-ChildItem 'C:\Program Files\JKI', 'C:\Program Files (x86)\JKI' -Recurse -Filter vipm.exe -ErrorAction SilentlyContinue | Select-Object -First 1; `
    if (-not $vipm) { throw 'vipm.exe was not found after installing VIPM.' }; `
    $path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + $vipm.DirectoryName; `
    [Environment]::SetEnvironmentVariable('Path', $path, 'Machine')

COPY Test-VipmVersion.ps1 C:/sbom/Test-VipmVersion.ps1
RUN & C:\sbom\Test-VipmVersion.ps1 -CliVersion $env:VIPM_CLI_VERSION -DesktopVersion $env:VIPM_DESKTOP_VERSION

SHELL ["pwsh", "-NoLogo", "-NoProfile", "-Command", "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue';"]

RUN git --version; svn --version --quiet; grype version; vipm --version; `
    git config --system core.longpaths true

COPY entrypoint.ps1 windows_sbom_pipeline.ps1 Initialize-VipmContainer.ps1 C:/sbom/

ENV VIPM_NONINTERACTIVE=1 `
    VIPM_EXPECTED_CLI_VERSION=${VIPM_CLI_VERSION} `
    VIPM_EXPECTED_DESKTOP_VERSION=${VIPM_DESKTOP_VERSION} `
    NO_COLOR=1 `
    GRYPE_DB_CACHE_DIR=C:\grype-db `
    GRYPE_DB_PERSIST_DIR=C:\grype-cache

WORKDIR C:\workspace
ENTRYPOINT ["pwsh", "-NoLogo", "-NoProfile", "-File", "C:\\sbom\\entrypoint.ps1"]
CMD ["C:\\sbom\\windows_sbom_pipeline.ps1"]
