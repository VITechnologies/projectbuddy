function Assert-VipmInstallerSignature {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [object]$Signature
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "VIPM installer is missing." }
    if ($null -eq $Signature) { $Signature = Get-AuthenticodeSignature -LiteralPath $Path }
    $signer = if ($Signature.SignerCertificate) {
        $Signature.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)
    } else { "" }
    if ($Signature.Status -ne "Valid" -or $signer -ne "James Kring, Inc.") {
        throw "VIPM installer is not validly signed by James Kring, Inc."
    }
}