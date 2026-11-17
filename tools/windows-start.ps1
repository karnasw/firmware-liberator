<#
Start Autoflash, the conversion tool, on Windows - what start.cmd in the
repository's root runs, from a double-click or a terminal:

    .\start.cmd                 convert a device (the same as: start.cmd run)
    .\start.cmd run --verbose   any of the tool's own commands and options

It runs the tool on python.org's portable Python, unpacked into .python\ in
the repository - nothing is installed. Only if that cannot be used does it
fall back to a Python already installed, and only if there is none does it
install one, quietly, for your account; it then offers to remove that again
when the tool exits.
#>
$ErrorActionPreference = "Stop"

# start.cmd and liberate.ps1 run this as text, not as a script file: a policy
# that stops script files - even one set for the whole computer, which no
# -ExecutionPolicy can lift - does not stop that. Run so, it has no
# $PSScriptRoot, and its arguments sit on the command line after the command's
# closing "#" (start.cmd says why): they are split there as Windows splits a
# command line.
function Get-StartArguments {
    $line = [Environment]::CommandLine
    $at = $line.IndexOf('#"')
    $rest = if ($at -ge 0) { $line.Substring($at + 2).Trim() } else { "" }
    if (-not $rest) { return @() }
    Add-Type -Namespace Autoflash -Name CommandLine -MemberDefinition @'
[DllImport("shell32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
static extern IntPtr CommandLineToArgvW(string line, out int count);
[DllImport("kernel32.dll")]
static extern IntPtr LocalFree(IntPtr memory);
public static string[] Split(string line) {
    int count;
    IntPtr argv = CommandLineToArgvW(line, out count);
    if (argv == IntPtr.Zero) throw new System.ComponentModel.Win32Exception();
    try {
        var parts = new string[count];
        for (int i = 0; i < count; i++) parts[i] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(argv, i * IntPtr.Size));
        return parts;
    } finally { LocalFree(argv); }
}
'@
    # A program's name first, which Windows reads by rules of its own.
    return @([Autoflash.CommandLine]::Split("autoflash $rest") | Select-Object -Skip 1)
}
if ($PSScriptRoot) {
    $Tools = $PSScriptRoot
    $startArguments = @($args)
} else {
    $Tools = Split-Path $env:AUTOFLASH_START
    $startArguments = @(Get-StartArguments)
}
# windows-python.ps1, as text too: where it keeps the portable Python, which as
# a file it finds by its own $PSScriptRoot.
if (-not $env:AUTOFLASH_PYTHON_HOME) { $env:AUTOFLASH_PYTHON_HOME = Join-Path (Split-Path $Tools) ".python" }
. ([scriptblock]::Create([IO.File]::ReadAllText((Join-Path $Tools "windows-python.ps1"))))
$Root = Split-Path $Tools

# Opened by a double-click (Explorer started the cmd that started this), the
# window would close before the last words are read, so wait for a key first.
# From a terminal the words stay on screen, and a key would only be in the way.
function Test-DoubleClicked {
    try {
        $me = Get-CimInstance Win32_Process -Filter "ProcessId = $PID"
        $launcher = Get-CimInstance Win32_Process -Filter "ProcessId = $($me.ParentProcessId)"
        $opener = Get-CimInstance Win32_Process -Filter "ProcessId = $($launcher.ParentProcessId)"
        return $opener.Name -eq "explorer.exe"
    } catch { return $false }
}
function Stop-Tool([int] $Code) {
    if (Test-DoubleClicked) {
        Write-Host ""
        Write-Host " Press any key to continue . . ."   # set in, as the tool's words are
        [void][Console]::ReadKey($true)
    }
    exit $Code
}

try { $python = Get-ToolPython }
catch {
    Write-Host " No Python could be found or set up: $($_.Exception.Message)"
    Write-Host " Install Python 3.10 or newer yourself (https://www.python.org/downloads/),"
    Write-Host " then run this again."
    Stop-Tool 1
}

# @(...): an if-expression would unwrap a single option to a bare string,
# which then reaches Python mangled.
$arguments = @(if ($startArguments.Count) { $startArguments } else { "run" })
$code = 1
try {
    & $python (Join-Path $Root "tools\autoflash\cli.py") @arguments
    $code = $LASTEXITCODE
} finally {
    Request-PythonRemoval
}
Stop-Tool $code
