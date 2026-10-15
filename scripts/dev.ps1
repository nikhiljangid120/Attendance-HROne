[CmdletBinding()]
param(
    [switch]$StartLocalMongo,
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,
    [switch]$Reload
)

$ErrorActionPreference = 'Stop'
$taskProjectRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw 'Create the virtual environment and install requirements.txt first. See README.md.'
}

Push-Location -LiteralPath $taskProjectRoot
try {
    # Resolve this project's .env through the app, preserving injected values.
    # The connection string is kept in the environment and never printed.
    $taskPingCode = @'
from app.main import db
from pymongo.errors import PyMongoError
try:
    db.command('ping')
except PyMongoError:
    raise SystemExit(1)
'@
    & $taskPython -c $taskPingCode
    if ($LASTEXITCODE -ne 0) {
        if (-not $StartLocalMongo) {
            throw 'MongoDB is unavailable. Start your database, or use -StartLocalMongo for the existing portable local installation.'
        }
        # Read configuration without opening a connection: do not accidentally
        # replace a remote/Atlas configuration with the portable local server.
        & $taskPython -c "import os; from pathlib import Path; from dotenv import load_dotenv; load_dotenv(Path('.env'), override=False); uri=os.getenv('MONGO_URI','mongodb://localhost:27017'); raise SystemExit(0 if uri in ('mongodb://127.0.0.1:27017','mongodb://localhost:27017','mongodb://127.0.0.1:27017/','mongodb://localhost:27017/') else 1)"
        if ($LASTEXITCODE -ne 0) {
            throw '-StartLocalMongo requires MONGO_URI to point to localhost:27017 or 127.0.0.1:27017.'
        }
        $taskMongoRoot = Join-Path $taskProjectRoot '.local\mongodb'
        $taskMongoBinary = Get-ChildItem -LiteralPath $taskMongoRoot -Filter mongod.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $taskMongoBinary) {
            throw 'Portable MongoDB is not present. Start MongoDB 6+ separately (for example Docker or Atlas), as described in README.md.'
        }
        $taskDataPath = Join-Path $taskProjectRoot '.local\data'
        $taskLogPath = Join-Path $taskProjectRoot '.local\mongod.log'
        New-Item -ItemType Directory -Path $taskDataPath -Force | Out-Null
        $taskMongoProcess = Start-Process -FilePath $taskMongoBinary.FullName -ArgumentList @(
            '--dbpath', ('"' + $taskDataPath + '"'), '--bind_ip', '127.0.0.1',
            '--port', '27017', '--logpath', ('"' + $taskLogPath + '"'), '--logappend'
        ) -WindowStyle Hidden -PassThru
        & $taskPython -c "import os; from app.main import db; from pymongo import MongoClient; MongoClient(os.getenv('MONGO_URI','mongodb://localhost:27017'),serverSelectionTimeoutMS=15000).admin.command('ping')"
        if ($LASTEXITCODE -ne 0) {
            throw "MongoDB did not become ready. Inspect .local\mongod.log (started process $($taskMongoProcess.Id))."
        }
    }
    Write-Host "Starting the attendance API. Docs: http://127.0.0.1:$Port/docs"
    Write-Host 'Press Ctrl+C to stop the API. The local MongoDB process stays running.'
    $taskServerArgs = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', [string]$Port)
    if ($Reload) {
        $taskServerArgs += '--reload'
        $taskServerArgs += '--reload-dir'
        $taskServerArgs += (Join-Path $taskProjectRoot 'app')
    }
    & $taskPython @taskServerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "API exited with code $LASTEXITCODE. Check whether port $Port is already occupied."
    }
}
finally {
    Pop-Location
}
