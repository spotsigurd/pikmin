param(
    [Parameter(Mandatory = $true)] [string]$BaseFile,
    [Parameter(Mandatory = $true)] [string]$CurrentFile,
    [Parameter(Mandatory = $true)] [string]$OtherFile,
    [Parameter(Mandatory = $true)] [string]$ResultFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Test-IsArray($Value) {
    return $Value -is [System.Collections.IList] -and $Value -isnot [string]
}

function Test-IsObject($Value) {
    if ($null -eq $Value) { return $false }
    return ($Value -is [pscustomobject]) -or ($Value -is [System.Collections.IDictionary])
}

function Get-PropertyNames($Object) {
    if ($Object -is [System.Collections.IDictionary]) {
        return @($Object.Keys)
    }
    return @($Object.PSObject.Properties.Name)
}

function Get-PropertyValue($Object, [string]$Name) {
    if ($Object -is [System.Collections.IDictionary]) {
        if ($Object.Contains($Name)) { return $Object[$Name] }
        return $null
    }

    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function ConvertTo-CanonicalJson($Value) {
    if ($null -eq $Value) { return 'null' }

    if ($Value -is [string]) {
        return ($Value | ConvertTo-Json -Compress)
    }

    if ($Value -is [bool]) {
        return ($(if ($Value) { 'true' } else { 'false' }))
    }

    if ($Value -is [byte] -or $Value -is [int16] -or $Value -is [int32] -or $Value -is [int64] -or
        $Value -is [sbyte] -or $Value -is [uint16] -or $Value -is [uint32] -or $Value -is [uint64] -or
        $Value -is [single] -or $Value -is [double] -or $Value -is [decimal]) {
        return ([System.Convert]::ToString($Value, [System.Globalization.CultureInfo]::InvariantCulture))
    }

    if (Test-IsArray $Value) {
        $items = foreach ($item in $Value) { ConvertTo-CanonicalJson $item }
        return '[' + ($items -join ',') + ']'
    }

    if (Test-IsObject $Value) {
        $names = @(Get-PropertyNames $Value | Sort-Object)
        $parts = foreach ($name in $names) {
            $key = ($name | ConvertTo-Json -Compress)
            $node = Get-PropertyValue $Value $name
            $valueJson = ConvertTo-CanonicalJson $node
            "${key}:$valueJson"
        }
        return '{' + ($parts -join ',') + '}'
    }

    return ($Value | ConvertTo-Json -Compress)
}

function Merge-Arrays($Base, $Current, $Other) {
    $result = New-Object System.Collections.ArrayList
    $seen = @{}

    foreach ($item in $Current) {
        $key = ConvertTo-CanonicalJson $item
        if (-not $seen.ContainsKey($key)) {
            [void]$result.Add($item)
            $seen[$key] = $true
        }
    }

    foreach ($item in $Other) {
        $key = ConvertTo-CanonicalJson $item
        if (-not $seen.ContainsKey($key)) {
            [void]$result.Add($item)
            $seen[$key] = $true
        }
    }

    return ,$result
}

function Merge-Nodes($Base, $Current, $Other) {
    $currentCanonical = ConvertTo-CanonicalJson $Current
    $otherCanonical = ConvertTo-CanonicalJson $Other

    if ($currentCanonical -eq $otherCanonical) { return $Current }

    $baseCanonical = ConvertTo-CanonicalJson $Base
    if ($currentCanonical -eq $baseCanonical) { return $Other }
    if ($otherCanonical -eq $baseCanonical) { return $Current }

    if ((Test-IsObject $Current) -and (Test-IsObject $Other)) {
        $merged = [ordered]@{}
        $allNames = @((Get-PropertyNames $Current) + (Get-PropertyNames $Other) + (Get-PropertyNames $Base) | Sort-Object -Unique)

        foreach ($name in $allNames) {
            $baseValue = Get-PropertyValue $Base $name
            $currentValue = Get-PropertyValue $Current $name
            $otherValue = Get-PropertyValue $Other $name
            $merged[$name] = Merge-Nodes $baseValue $currentValue $otherValue
        }

        return [pscustomobject]$merged
    }

    if ((Test-IsArray $Current) -and (Test-IsArray $Other)) {
        return Merge-Arrays $Base $Current $Other
    }

    return $Current
}

function Read-JsonNode([string]$Path, $Fallback) {
    if (-not (Test-Path $Path)) {
        return $Fallback
    }

    $content = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    if ([string]::IsNullOrWhiteSpace($content)) {
        return $Fallback
    }

    return ($content | ConvertFrom-Json)
}

try {
    $baseNode = Read-JsonNode $BaseFile @()
    $currentNode = Read-JsonNode $CurrentFile @()
    $otherNode = Read-JsonNode $OtherFile @()

    $mergedNode = Merge-Nodes $baseNode $currentNode $otherNode

    $json = $mergedNode | ConvertTo-Json -Depth 100
    [System.IO.File]::WriteAllText($ResultFile, $json + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))
    exit 0
}
catch {
    try {
        Copy-Item -LiteralPath $CurrentFile -Destination $ResultFile -Force
    }
    catch {
    }
    exit 0
}
