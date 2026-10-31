param([Parameter(Mandatory=$true)][string]$Url)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 45
    if ($response.Content -is [byte[]]) {
        [Console]::Write([System.Text.Encoding]::UTF8.GetString($response.Content))
    } else {
        [Console]::Write($response.Content)
    }
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
