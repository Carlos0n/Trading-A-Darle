# Instalación en Windows (PowerShell). Ejecutar desde cualquier carpeta:
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
# Requisitos: Windows 10/11 x64, MetaTrader 5 instalado, Python 3.11 a 3.14 (https://www.python.org/downloads/windows/).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "== 1/5 Python" -ForegroundColor Cyan
$launcher = Get-Command py -ErrorAction SilentlyContinue
if ($launcher) { $pyCmd = @("py", "-3") } else { $pyCmd = @("python") }
$version = & $pyCmd[0] $pyCmd[1..($pyCmd.Length-1)] -c "import sys; print('%d.%d' % sys.version_info[:2])"
Write-Host "   Python $version"
if ([version]$version -lt [version]"3.11") { throw "Se necesita Python 3.11 o superior (encontrado $version)" }

Write-Host "== 2/5 Entorno virtual (.venv)" -ForegroundColor Cyan
if (-not (Test-Path ".venv")) { & $pyCmd[0] $pyCmd[1..($pyCmd.Length-1)] -m venv .venv }
$py = Join-Path $root ".venv\Scripts\python.exe"
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -e ".[dev]" --quiet
Write-Host "   Dependencias instaladas (mcp, MetaTrader5, pytest)"

Write-Host "== 3/5 Pruebas del motor de riesgo" -ForegroundColor Cyan
& $py -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "Las pruebas fallaron; no sigas hasta resolverlo" }

Write-Host "== 4/5 Variable de entorno MT5_MCP_PYTHON (usuario)" -ForegroundColor Cyan
[Environment]::SetEnvironmentVariable("MT5_MCP_PYTHON", $py, "User")
$env:MT5_MCP_PYTHON = $py
Write-Host "   MT5_MCP_PYTHON = $py"
Write-Host "   (.mcp.json usa esta variable para lanzar el servidor; abre una terminal nueva para que Claude Code la vea)"

Write-Host "== 5/5 Conexión con MetaTrader 5" -ForegroundColor Cyan
Write-Host "   Abre MetaTrader 5 con tu cuenta DEMO, pulsa 'Algo Trading' y ejecuta:"
Write-Host "   & `"$py`" scripts\check_connection.py" -ForegroundColor Yellow
Write-Host ""
Write-Host "Listo. Luego, en esta carpeta: claude   (y dentro: /mcp para ver el servidor 'mt5')" -ForegroundColor Green
