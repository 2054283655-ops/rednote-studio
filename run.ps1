$ErrorActionPreference = "Stop"
$project = $PSScriptRoot
$python = Join-Path $project ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    & py -3.12 -m venv (Join-Path $project ".venv")
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.12 is required. See README.md for setup instructions."
    }
    & $python -m pip install -r (Join-Path $project "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed. See the pip output above."
    }
}

& $python -m streamlit run (Join-Path $project "app.py") --server.address 127.0.0.1 --server.port 8502 --browser.gatherUsageStats false
