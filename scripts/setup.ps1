# Instalación en Windows (PowerShell). Ejecutar desde la raíz del repositorio:
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
# (o desde cualquier carpeta indicando la ruta completa al script).
# Requisitos: Windows 10/11 x64, MetaTrader 5 instalado, Python 3.11 a 3.14 de python.org con el lanzador "py".
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "== 1/5 Python (3.11 a 3.14: el paquete MetaTrader5 solo publica binarios para esas versiones)" -ForegroundColor Cyan
$exe = $null
$pre = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($candidate in @("-3.14", "-3.13", "-3.12", "-3.11")) {
        $probe = & py $candidate -c "print(1)" 2>$null
        if ($LASTEXITCODE -eq 0 -and "$probe" -eq "1") { $exe = "py"; $pre = @($candidate); break }
    }
}
if (-not $exe -and (Get-Command python -ErrorAction SilentlyContinue)) {
    $v = & python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -eq 0 -and $v -and ([version]$v -ge [version]"3.11") -and ([version]$v -lt [version]"3.15")) { $exe = "python" }
}
if (-not $exe) {
    throw "No se encontró Python 3.11-3.14. Instálalo desde https://www.python.org/downloads/windows/ (marca 'Add python.exe to PATH' y el lanzador py) y vuelve a ejecutar este script."
}
$version = & $exe @pre -c "import sys; print('%d.%d' % sys.version_info[:2])"
Write-Host "   Usando $exe $pre (Python $version)"

Write-Host "== 2/5 Entorno virtual (.venv) y dependencias" -ForegroundColor Cyan
if (-not (Test-Path ".venv")) { & $exe @pre -m venv .venv }
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "No se creó $py" }
& $py -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { throw "pip no pudo actualizarse" }
& $py -m pip install -e ".[dev]" --quiet
if ($LASTEXITCODE -ne 0) { throw "La instalación de dependencias falló (mcp, MetaTrader5, pytest)" }
Write-Host "   Dependencias instaladas"

Write-Host "== 3/5 Pruebas del motor de riesgo" -ForegroundColor Cyan
& $py -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "Las pruebas fallaron; no sigas hasta resolverlo" }

Write-Host "== 4/5 Variable de usuario MT5_MCP_PYTHON (la usa .mcp.json para lanzar el servidor)" -ForegroundColor Cyan
[Environment]::SetEnvironmentVariable("MT5_MCP_PYTHON", $py, "User")
$env:MT5_MCP_PYTHON = $py
Write-Host "   MT5_MCP_PYTHON = $py"
Write-Host "   IMPORTANTE: solo la ven los programas abiertos DESPUÉS de ahora. Cierra y vuelve a abrir la terminal"
Write-Host "   (o VS Code / la app de Claude) antes de lanzar 'claude'. Sin ella, .mcp.json intentaría usar 'python' a secas."

Write-Host "== 5/5 Conexión con MetaTrader 5" -ForegroundColor Cyan
Write-Host "   1) Abre MetaTrader 5 con tu cuenta DEMO."
Write-Host "   2) Herramientas > Opciones > Asesores Expertos: DESMARCA 'Deshabilitar el trading automático a través de la API externa de Python'."
Write-Host "   3) Deja encendido el botón 'Algo Trading' de la barra de herramientas."
Write-Host "   4) Ejecuta:  & `"$py`" scripts\check_connection.py" -ForegroundColor Yellow
Write-Host ""
Write-Host "Después, en una terminal NUEVA dentro de esta carpeta: claude   (y dentro: /mcp para ver el servidor 'mt5')" -ForegroundColor Green
