# Hauskern – Python von python.org laden und für den aktuellen Benutzer installieren.
# Wird von Hauskern-Setup.cmd aufgerufen, wenn kein Python da ist und winget fehlt.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

$base = 'https://www.python.org/ftp/python/'
$arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'amd64' }
try {
    $links = (Invoke-WebRequest $base -UseBasicParsing).Links | ForEach-Object { $_.href }
} catch {
    Write-Host "  python.org ist nicht erreichbar: $($_.Exception.Message)"
    exit 1
}
# neueste 3.13.x zuerst (die Version, mit der Hauskern getestet ist)
$versions = $links | Where-Object { $_ -match '^3\.13\.\d+/$' } | ForEach-Object { $_.TrimEnd('/') } |
    Sort-Object { [version]$_ } -Descending

$file = $null
foreach ($v in $versions) {
    $url = "$base$v/python-$v-$arch.exe"
    $out = Join-Path $env:TEMP "python-$v-$arch.exe"
    try {
        Invoke-WebRequest $url -OutFile $out -UseBasicParsing
        $file = $out
        Write-Host "  Geladen: Python $v"
        break
    } catch { }   # z. B. Vorabversion ohne Installer – nächste probieren
}
if (-not $file) { Write-Host "  Kein passender Python-Installer gefunden."; exit 1 }

# Sicherheit: nur einen echt signierten Installer der Python Software Foundation ausführen
$sig = Get-AuthenticodeSignature $file
if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
    Write-Host "  Signatur stimmt nicht – Installation abgebrochen."
    Remove-Item $file -ErrorAction SilentlyContinue
    exit 1
}
Write-Host "  Installiere ... (das Fenster kann kurz hängen)"
$p = Start-Process $file -ArgumentList '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1', 'Include_test=0' -Wait -PassThru
Remove-Item $file -ErrorAction SilentlyContinue
exit $p.ExitCode
