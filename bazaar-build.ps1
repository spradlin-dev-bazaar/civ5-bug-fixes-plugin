<#
.SYNOPSIS
build.cmd's steps that require more than a batch file: finding the project's files and the Bazaar SDK, and checking the
package.

.DESCRIPTION
build.cmd runs "start" before building and "check" after building. The plugin template's build.cmd adds -Plugin to
both steps. "start" deletes from Packages any package that an earlier build made for the same mod version, and prints
the mod's name and the paths of the project files that build.cmd builds from. For a plugin, "start" also prints the
folder of the SDK made for the Bazaar version that the mod depends on: the folder given to build.cmd, otherwise the one
in that version's variable, such as BAZAAR_SDK_0_5, otherwise it will check the parent directory. "check" checks the
new package for mistakes that would keep Bazaar from loading the mod's plugin, deletes a package that has any such
mistakes, and prints a warning if any values reserved for the template plugin haven't been updated with your own
values.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [ValidateSet('start', 'check')] [string] $Step,
    [switch] $Plugin
)

$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
# Oldest first. Bazaar's tests check that the last entry is the mod ID of the Bazaar this SDK is for.
$bazaarVersions = [ordered]@{
    '0.5' = 'b829cadb-b570-46c5-a0ba-419434eb6398'
}
$templateIds = @('63a9bd4c-c9fc-4d26-97c7-9a8a86e331be', 'f4d85239-ecea-43dc-b4ff-e64e23ad91b4')
# Split so the scaffold scripts, which put a new mod's names in place of the template's in every file, leave these.
$templateNames = @(('Template' + ' Plugin'), ('Template' + ' Lua Mod'))
$templateDll = 'Template' + 'Plugin.dll'
$templateTeasers = @('A minimal plugin for the Bazaar Multi-DLL Mod Loader.', 'A minimal Lua mod for the Bazaar Multi-DLL Mod Loader.')
$latin1 = [Text.Encoding]::GetEncoding(28591)
$tar = Join-Path $env:SystemRoot 'System32\tar.exe'

function Stop-Build([string] $message) {
    [Console]::Error.WriteLine("build: $message")
    exit 1
}

function Read-RawText([string] $path) {
    return $latin1.GetString([IO.File]::ReadAllBytes($path))
}

# Not run with &: in Windows PowerShell, a program's error output can become errors that stop this script.
function Invoke-Program([string] $file, [string] $arguments) {
    $start = New-Object Diagnostics.ProcessStartInfo $file, $arguments
    $start.UseShellExecute = $false
    $process = [Diagnostics.Process]::Start($start)
    $process.WaitForExit()
    return $process.ExitCode
}

# The document, or the parser's reason the file is not valid XML. Any other failure, such as a locked file, stops the
# script with its own message.
function Read-Xml([string] $path) {
    $xml = New-Object Xml.XmlDocument
    try {
        $xml.Load($path)
    } catch [Xml.XmlException] {
        return $_.Exception.GetBaseException().Message
    }
    return $xml
}

function Find-ProjectElement($xml, [string] $element) {
    return $xml.SelectSingleNode("/*[local-name()='Project']/*[local-name()='PropertyGroup']/*[local-name()='$element']")
}

function Read-SingleProjectFile([string] $folder, [string] $extension) {
    $files = @(Get-ChildItem -LiteralPath (Join-Path $root $folder) -Filter "*$extension" -File -ErrorAction SilentlyContinue)
    if ($files.Count -ne 1) {
        Stop-Build "Expected exactly one $extension file in the $folder folder, but found $($files.Count)."
    }
    $xml = Read-Xml $files[0].FullName
    if ($xml -is [string]) {
        Stop-Build "$folder\$($files[0].Name) is not valid XML, so build.cmd cannot read the file's settings. The XML reader's error: $xml Open the file in a text editor and fix the XML."
    }
    return @{ Path = "$folder\$($files[0].Name)"; Xml = $xml }
}

function Read-Project {
    $file = Read-SingleProjectFile 'mod' '.civ5proj'
    $xml = $file.Xml
    $name = ([string] (Find-ProjectElement $xml 'Name').InnerText).Trim()
    if (-not $name) {
        Stop-Build "No mod name was found in $($file.Path)."
    }
    $modVersion = Find-ProjectElement $xml 'ModVersion'
    if (-not $modVersion -or $modVersion.InnerText.Trim() -notmatch '^[1-9][0-9]{0,8}$') {
        Stop-Build "$($file.Path) has no <ModVersion> line with a whole number above 0."
    }
    $dependencies = $xml.SelectNodes("/*[local-name()='Project']/*[local-name()='PropertyGroup']/*[local-name()='ModDependencies']/*[local-name()='Association']/*[local-name()='Id']")
    return @{
        Path         = $file.Path
        Name         = $name
        Id           = ([string] (Find-ProjectElement $xml 'Guid').InnerText).Trim().ToLowerInvariant()
        Authors      = ([string] (Find-ProjectElement $xml 'Authors').InnerText).Trim()
        Teaser       = [string] (Find-ProjectElement $xml 'Teaser').InnerText
        Description  = [string] (Find-ProjectElement $xml 'Description').InnerText
        ModVersion   = [int] $modVersion.InnerText.Trim()
        Dependencies = @($dependencies | ForEach-Object { $_.InnerText.Trim().ToLowerInvariant() })
    }
}

function Read-PluginProject {
    $file = Read-SingleProjectFile 'src' '.vcproj'
    $release = $file.Xml.SelectSingleNode("/VisualStudioProject/Configurations/Configuration[@Name='Release|Win32']")
    $output = if ($release) { $release.SelectSingleNode("Tool[@Name='VCLinkerTool']/@OutputFile") } else { $null }
    $value = if ($output) { $output.Value.Trim() } else { '' }
    if ($value -notmatch '^\$\(OutDir\)[\\/]([^\\/$]+\.dll)$') {
        Stop-Build ("$($file.Path) does not give the file name of the DLL that the project's Release|Win32 configuration builds. " +
            'In Visual C++, set the linker''s Output File for Release|Win32 to $(OutDir)\ followed by the DLL''s file name, such as $(OutDir)\ScoreDoubler.dll.')
    }
    $dll = $Matches[1]
    $outDir = $release.GetAttribute('OutputDirectory').Trim()
    if ($outDir -notmatch '^\$\(ProjectDir\)[\\/]?out[\\/]?$') {
        Stop-Build ("$($file.Path) does not set the Output Directory of the project's Release|Win32 configuration to src\out. " +
            'In Visual C++, set the General page''s Output Directory for Release|Win32 to $(ProjectDir)out, which is the folder src\out.')
    }
    return @{ Path = $file.Path; Dll = $dll }
}

function Get-BazaarVersion($project) {
    $found = @($bazaarVersions.Keys | Where-Object { $project.Dependencies -contains $bazaarVersions[$_] })
    if ($found.Count -gt 1) {
        Stop-Build "$($project.Path) depends on Bazaar $($found -join ' and '). A mod is built for a single Bazaar version. Keep the dependency on just one of those versions."
    }
    if (-not $found.Count) {
        $known = ($bazaarVersions.Keys | ForEach-Object { "Bazaar $_" }) -join ', '
        Stop-Build "$($project.Path) does not depend on a Bazaar version that this build script can build for ($known). Add a dependency on the mod ID of the Bazaar version that the mod runs on, from the table of mod IDs in the guide. To build for a newer Bazaar version, follow the steps in the guide's section `"A build for a new Bazaar version`"."
    }
    return $found[0]
}

function Test-SdkFolder([string] $folder) {
    return (Test-Path -LiteralPath (Join-Path $folder 'CivPlugin.vsprops') -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $folder 'include\CivPluginApi.h') -PathType Leaf)
}

function Get-SdkApiVersion([string] $folder) {
    $header = Read-RawText (Join-Path $folder 'include\CivPluginApi.h')
    $major = [regex]::Match($header, '#define CIV_HOST_API_MAJOR (\d+)')
    $minor = [regex]::Match($header, '#define CIV_HOST_API_MINOR (\d+)')
    if (-not $major.Success -or -not $minor.Success) {
        return $null
    }
    return '{0}.{1}' -f [int] $major.Groups[1].Value, [int] $minor.Groups[1].Value
}

function Find-Sdk([string] $version) {
    $variable = 'BAZAAR_SDK_' + $version.Replace('.', '_')
    $named = [Environment]::GetEnvironmentVariable($variable)
    # build.cmd's argument. On a command line, a folder ending in \ or holding a ^ would not arrive intact.
    if ($env:BAZAAR_SDK_GIVEN) {
        $folder, $source = $env:BAZAAR_SDK_GIVEN, 'given to build.cmd'
    } elseif ($named) {
        $folder, $source = $named, "in $variable"
    } else {
        $folder = Split-Path $root
        if (-not (Test-SdkFolder $folder)) {
            $visualCpp = if ($env:BAZAAR_SDK) { " BAZAAR_SDK, which is set on this computer, is the variable that Visual C++ reads. build.cmd reads $variable." } else { '' }
            Stop-Build "No Bazaar SDK was found for Bazaar $version, which the mod depends on. Give build.cmd the SDK's folder, as in build.cmd `"C:\Bazaar $version SDK`", or set $variable to the SDK's folder.$visualCpp"
        }
        $source = 'above the project'
    }
    if (-not (Test-SdkFolder $folder)) {
        Stop-Build "The folder $source, '$folder', is not a Bazaar SDK: it has no CivPlugin.vsprops or include\CivPluginApi.h."
    }
    $api = Get-SdkApiVersion $folder
    if (-not $api) {
        Stop-Build "The SDK's include\CivPluginApi.h, in '$folder', names no API version."
    }
    if ($api -ne $version) {
        Stop-Build "The SDK $source, '$folder', is for Bazaar $api, but the mod depends on Bazaar $version. Build with Bazaar $version's SDK."
    }
    # Combining with '.' drops a separator at the end of the folder but keeps a drive root such as C:\ whole,
    # because C: alone means the current folder on drive C.
    return [IO.Path]::GetFullPath([IO.Path]::Combine($folder, '.'))
}

function Get-BuiltPackages([int] $modVersion) {
    Get-ChildItem -LiteralPath (Join-Path $root 'Packages') -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name.EndsWith(" (v $modVersion).civ5mod", [StringComparison]::OrdinalIgnoreCase) }
}

function Get-SqlDllPluginRows([string] $sql) {
    # Quoted text is matched first and kept, so '--' inside a DLL's name is not taken for a comment.
    $code = [regex]::Replace($sql, "'[^']*'|--[^\r\n]*|/\*[\s\S]*?\*/", { param($found) if ($found.Value[0] -eq "'") { $found.Value } else { '' } })
    foreach ($insert in [regex]::Matches($code, '(?i)(?:INSERT\s+(?:OR\s+\w+\s+)?|REPLACE\s+)INTO\s+["`\[]?DllPlugins\b[^;]*')) {
        foreach ($value in [regex]::Matches($insert.Value, "'([^']+)'")) {
            $value.Groups[1].Value
        }
    }
}

# The game takes a row's column from an attribute or from a child element.
function Get-XmlDllPluginRows($xml) {
    foreach ($row in $xml.SelectNodes('/GameData/DllPlugins/Row | /GameData/DllPlugins/Replace')) {
        $file = $row.GetAttribute('File')
        if (-not $file -and $row.SelectSingleNode('File')) {
            $file = $row.SelectSingleNode('File').InnerText
        }
        if ($file.Trim()) {
            $file.Trim()
        }
    }
}

# $null means no such export; a file that is not a 32-bit DLL throws. Offsets: Microsoft's PE format specification.
# CivPluginApiVersion's address holds two ints, the major and minor API version (CivPluginApi.h).
function Get-ExportedApiVersion([byte[]] $data) {
    $peHeader = [BitConverter]::ToInt32($data, 0x3C)
    if ($peHeader -lt 0 -or [BitConverter]::ToUInt32($data, $peHeader) -ne 0x4550) {
        throw 'No PE header.'
    }
    $sectionCount = [BitConverter]::ToUInt16($data, $peHeader + 6)
    $optionalHeaderSize = [BitConverter]::ToUInt16($data, $peHeader + 20)
    $optionalHeader = $peHeader + 24
    if ([BitConverter]::ToUInt16($data, $optionalHeader) -ne 0x10B) {
        throw 'Not a 32-bit DLL.'
    }
    $sections = @(for ($i = 0; $i -lt $sectionCount; $i++) {
            $entry = $optionalHeader + $optionalHeaderSize + 40 * $i
            [pscustomobject]@{
                Size    = [Math]::Max([BitConverter]::ToUInt32($data, $entry + 8), [BitConverter]::ToUInt32($data, $entry + 16))
                Address = [long] [BitConverter]::ToUInt32($data, $entry + 12)
                Start   = [long] [BitConverter]::ToUInt32($data, $entry + 20)
            }
        })
    $toFilePosition = {
        param([long] $address)
        foreach ($section in $sections) {
            if ($address -ge $section.Address -and $address -lt $section.Address + $section.Size) {
                return $section.Start + $address - $section.Address
            }
        }
        throw "No section holds address $address."
    }
    $exportTableAddress = [BitConverter]::ToUInt32($data, $optionalHeader + 96)
    if ($exportTableAddress -eq 0) {
        return $null
    }
    $exportTable = & $toFilePosition $exportTableAddress
    $nameCount = [BitConverter]::ToUInt32($data, $exportTable + 24)
    $functions = & $toFilePosition ([BitConverter]::ToUInt32($data, $exportTable + 28))
    $names = & $toFilePosition ([BitConverter]::ToUInt32($data, $exportTable + 32))
    $ordinals = & $toFilePosition ([BitConverter]::ToUInt32($data, $exportTable + 36))
    for ($i = 0; $i -lt $nameCount; $i++) {
        $nameStart = & $toFilePosition ([BitConverter]::ToUInt32($data, $names + 4 * $i))
        $nameEnd = [Array]::IndexOf($data, [byte] 0, [int] $nameStart)
        if ([Text.Encoding]::ASCII.GetString($data, $nameStart, $nameEnd - $nameStart) -ceq 'CivPluginApiVersion') {
            $ordinal = [BitConverter]::ToUInt16($data, $ordinals + 2 * $i)
            $value = & $toFilePosition ([BitConverter]::ToUInt32($data, $functions + 4 * $ordinal))
            return '{0}.{1}' -f [BitConverter]::ToInt32($data, $value), [BitConverter]::ToInt32($data, $value + 4)
        }
    }
    return $null
}

function Get-UnpackedModProblems([string] $folder, [bool] $isPlugin, [string] $sdkApi) {
    $problems = New-Object 'System.Collections.Generic.List[string]'
    $modinfo = @(Get-ChildItem -LiteralPath $folder -Filter '*.modinfo' -File)
    if ($modinfo.Count -ne 1) {
        $problems.Add("it has $($modinfo.Count) mod descriptions (.modinfo files) instead of a single mod description")
        return $problems
    }
    $xml = Read-Xml $modinfo[0].FullName
    if ($xml -is [string]) {
        $problems.Add("its mod description is not valid XML. The XML reader's error: $xml")
        return $problems
    }
    $mod = $xml.SelectSingleNode('/Mod')
    if (-not $mod) {
        $problems.Add('its mod description has no <Mod> element, so it does not describe a mod')
        return $problems
    }
    $updates = @($mod.SelectNodes('Actions//UpdateDatabase') | ForEach-Object { $_.InnerText.Trim() })
    $affects = $mod.SelectSingleNode('Properties/AffectsSavedGames')
    if ($updates.Count -and -not ($affects -and $affects.InnerText -eq '1')) {
        $problems.Add('it changes the game''s database but is not set to affect saved games, so loading a save would leave those changes out. Set <AffectsSavedGames> to true in the .civ5proj')
    }
    $dlls = @(Get-DllPluginRowsInUpdates $folder $updates $problems)
    if ($isPlugin -and -not $dlls.Count) {
        $problems.Add('none of the package''s database updates adds the plugin''s DLL to the DllPlugins table, so Bazaar would never load the plugin (the check recognizes only SQL rows written as INSERT INTO DllPlugins (File) VALUES (''Name.dll''), as in the template, and XML rows inside <GameData><DllPlugins>)')
    }
    $listed = @($mod.SelectNodes('Files/File') | ForEach-Object { ($_.InnerText.Trim() -split '[\\/]')[-1] })
    $packed = @(Get-ChildItem -LiteralPath $folder -Recurse -File)
    foreach ($dll in $dlls) {
        Add-DllProblems $folder $dll $packed $listed $sdkApi $problems
    }
    return $problems
}

function Get-DllPluginRowsInUpdates([string] $folder, [string[]] $updates, $problems) {
    foreach ($update in $updates) {
        $path = Join-Path $folder $update
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            Get-FileDllPluginRows $path $update $problems
        }
    }
}

# $problems may be $null, to ignore an XML file that is not valid XML.
function Get-FileDllPluginRows([string] $path, [string] $name, $problems) {
    if (-not $path.EndsWith('.xml', [StringComparison]::OrdinalIgnoreCase)) {
        return Get-SqlDllPluginRows (Read-RawText $path)
    }
    $rows = Read-Xml $path
    if ($rows -isnot [string]) {
        return Get-XmlDllPluginRows $rows
    }
    if ($null -ne $problems) {
        $problems.Add("the database update $name is not valid XML. The XML reader's error: $rows")
    }
}

function Add-DllProblems([string] $folder, [string] $dll, $packed, [string[]] $listed, [string] $sdkApi, $problems) {
    # Bazaar matches a plugin's DLL by file name among the mod's files, in any folder (host/CivHostMods.cpp).
    $file = @($packed | Where-Object { $_.Name -eq $dll })
    if (-not $file.Count) {
        $problems.Add("it has no $dll, the DLL that its DllPlugins row names")
        return
    }
    if ($file.Count -gt 1) {
        $prefix = (Get-Item -LiteralPath $folder).FullName.TrimEnd('\') + '\'
        $paths = @($file | ForEach-Object { $_.FullName.Substring($prefix.Length) } | Sort-Object)
        $list = if ($paths.Count -lt 3) { $paths -join ' and ' } else { ($paths[0..($paths.Count - 2)] -join ', ') + ', and ' + $paths[-1] }
        $problems.Add("the package holds more than one file named $($dll): $list. Bazaar 0.5 finds the plugin's DLL by its file name among the mod's files and loads the last file that has that name in the mod description's list of files, so which copy loads depends on the order of that list. Keep a single file named $dll in the mod")
        return
    }
    if ($listed -notcontains $dll) {
        $problems.Add("its mod description does not list $dll among the mod's files, so Bazaar would not find the DLL. Keep the DLL's <Content> entry in the .civ5proj")
    }
    try {
        $api = Get-ExportedApiVersion ([IO.File]::ReadAllBytes($file[0].FullName))
    } catch {
        $problems.Add("$dll could not be read as a 32-bit Windows DLL, the format that Civilization V requires")
        return
    }
    if (-not $sdkApi) {
        return
    }
    if (-not $api) {
        $problems.Add("$dll declares no API version. Add the line CIV_PLUGIN_EXPORT_API_VERSION; to one of its .cpp files")
    } elseif ($api -ne $sdkApi) {
        $problems.Add("$dll declares API $api, but the SDK that it was built with is for $sdkApi")
    }
}

function Get-PackageProblems([string] $package, [bool] $isPlugin, [string] $sdkApi) {
    $unpacked = Join-Path ([IO.Path]::GetTempPath()) ('bazaar-build-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $unpacked | Out-Null
    try {
        if ((Invoke-Program $tar "-xf `"$package`" -C `"$unpacked`"") -ne 0) {
            return "Windows' tar could not unpack the package"
        }
        return (Get-UnpackedModProblems $unpacked $isPlugin $sdkApi)
    } finally {
        Remove-Item -LiteralPath $unpacked -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Get-TemplateLeftovers($project) {
    if ($templateIds -contains $project.Id) {
        "the .civ5proj still has the template's mod ID. Two mods with the same mod ID are treated as a single mod by the game, and Bazaar would mix their saved data. Put a new GUID on the <Guid> line (PowerShell's New-Guid makes one)"
    }
    if ($templateNames -contains $project.Name.Trim()) {
        "the mod is still called '$($project.Name)', the template's name. Change the .civ5proj's <Name> line"
    }
    if ($project.Authors -eq 'Your name') {
        "the .civ5proj's author is still 'Your name'. Change its <Authors> line"
    }
    foreach ($teaser in $templateTeasers) {
        if ($project.Teaser.Contains($teaser) -or $project.Description.Contains($teaser)) {
            "the .civ5proj's <Teaser> or <Description> still has the template's text '$teaser'. Describe your mod in those elements"
        }
    }
    foreach ($update in Get-ChildItem -LiteralPath (Join-Path $root 'mod') -File | Where-Object { $_.Extension -in '.sql', '.xml' }) {
        if (@(Get-FileDllPluginRows $update.FullName $update.Name $null) -contains $templateDll) {
            "mod\$($update.Name) still adds the template's DLL, $templateDll, to DllPlugins. No two plugins may ship a DLL with the same name. Rename the DLL and its row as the guide's Step 2 explains"
        }
    }
}

$project = Read-Project
if ($Step -eq 'start') {
    if (-not (Test-Path -LiteralPath $tar -PathType Leaf)) {
        Stop-Build "Windows' tar.exe, which checks the package, is not at $tar. tar.exe comes with Windows 10 version 1803 and later."
    }
    $found = [ordered]@{}
    if ($Plugin) {
        $found.SDK = Find-Sdk (Get-BazaarVersion $project)
        $pluginProject = Read-PluginProject
        $found.VCPROJ = $pluginProject.Path
        $found.DLL = $pluginProject.Dll
    }
    $found.NAME = $project.Name
    $found.PROJECT = $project.Path
    Get-BuiltPackages $project.ModVersion | Remove-Item -Force
    # build.cmd sets BUILD_<name> from each line, and takes a missing PROJECT line, printed last, for a failed start.
    foreach ($name in $found.Keys) {
        Write-Output "$name=$($found[$name])"
    }
    exit 0
}

$built = @(Get-BuiltPackages $project.ModVersion)
if ($built.Count -ne 1) {
    Stop-Build "The build made $($built.Count) packages ending in '(v $($project.ModVersion)).civ5mod' in Packages, not one."
}
# build.cmd sets BAZAAR_SDK to the SDK start found, and the plugin's .vcproj builds with that same variable.
$sdkApi = if ($Plugin) { Get-SdkApiVersion $env:BAZAAR_SDK } else { $null }
$problems = @(Get-PackageProblems $built[0].FullName $Plugin.IsPresent $sdkApi)
foreach ($leftover in Get-TemplateLeftovers $project) {
    Write-Output "build: warning: $leftover."
}
if ($problems.Count) {
    Remove-Item -LiteralPath $built[0].FullName -Force -ErrorAction SilentlyContinue
    $fate = if (Test-Path -LiteralPath $built[0].FullName) { 'Windows did not let build.cmd delete the package. Delete it yourself before you upload anything.' } else { 'build.cmd deleted the package, so that nobody can upload a broken package by mistake.' }
    Stop-Build ("Packages\$($built[0].Name) has these problems:`n" + (($problems | ForEach-Object { "  - $_" }) -join "`n") + "`n$fate")
}
Write-Output "build: checked Packages\$($built[0].Name)"
exit 0
