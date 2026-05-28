Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$paths = @(
    "src/core",
    "src/exporters",
    "src/ui",
    "src/ui/workers",
    "migration_core/src"
)

$legacyPattern = "pymysql|psycopg|mysqlsh|MySQLShell|MySQL Shell|mysql_shell|mysqlsh_exporter|mysql_worker|check_mysqlsh|migration-core|migration_core_executable"
$engineLockedPattern = "from src\.core\.db_connector import MySQLConnector"
$allowedEngineLocked = @(
    "src/core/db_connector.py",
    "src/core/migration_analyzer.py",
    "src/core/migration_auto_recommend.py",
    "src/core/migration_fix_wizard.py",
    "src/core/migration_preflight.py",
    "src/core/migration_validator.py",
    "src/exporters/rust_dump_exporter.py",
    "src/ui/dialogs/db_dialogs.py",
    "src/ui/dialogs/diff_dialog.py",
    "src/ui/dialogs/fix_wizard_dialog.py",
    "src/ui/dialogs/migration_dialogs.py",
    "src/ui/dialogs/oneclick_migration_dialog.py",
    "src/ui/workers/fix_wizard_worker.py",
    "src/ui/workers/metadata_worker.py",
    "src/ui/workers/migration_worker.py"
)

Push-Location $root
try {
    $legacyHits = & rg -n $legacyPattern @paths
    if ($LASTEXITCODE -eq 0) {
        Write-Error "Rust Core regression gate failed: legacy DB/export helper reference found.`n$legacyHits"
    }
    if ($LASTEXITCODE -gt 1) {
        exit $LASTEXITCODE
    }

    $engineHits = & rg -n $engineLockedPattern src/core src/ui
    if ($LASTEXITCODE -eq 0) {
        $unexpected = @()
        foreach ($line in $engineHits) {
            $file = ($line -split ":", 2)[0].Replace("\", "/")
            if ($allowedEngineLocked -notcontains $file) {
                $unexpected += $line
            }
        }
        if ($unexpected.Count -gt 0) {
            Write-Error "Rust Core regression gate failed: product path imports MySQLConnector directly.`n$($unexpected -join "`n")"
        }
    } elseif ($LASTEXITCODE -gt 1) {
        exit $LASTEXITCODE
    }

    if ($env:RUST_CORE_REQUIRE_PERF_EVIDENCE -eq "1") {
        $perfFiles = @(
            "migration_core/target/perf_pg_mysql_1m_migrate.jsonl",
            "migration_core/target/perf_pg_mysql_1m_verify.jsonl",
            "migration_core/target/perf_stress_10m_resume.jsonl",
            "migration_core/target/perf_stress_10m_verify.jsonl"
        )
        foreach ($file in $perfFiles) {
            if (-not (Test-Path $file)) {
                Write-Error "Rust Core performance gate failed: missing evidence file $file"
            }
            $result = Get-Content $file | Where-Object { $_ -match '"event"\s*:\s*"result"' } | Select-Object -Last 1 | ConvertFrom-Json
            if (-not $result.success) {
                Write-Error "Rust Core performance gate failed: $file did not finish successfully"
            }
        }
    }

    $securityForbidden = @(
        "mysql_local_infile_sql",
        "ensure_mysql_local_infile_for_import",
        "restore_mysql_local_infile_after_import",
        "input_path.join(&table_manifest.path)",
        "remove_dir_all(output_path)"
    )
    foreach ($pattern in $securityForbidden) {
        $hits = & rg -n -F $pattern migration_core/src src tests
        if ($LASTEXITCODE -eq 0) {
            Write-Error "Rust Core security regression gate failed: forbidden pattern '$pattern' found.`n$hits"
        }
        if ($LASTEXITCODE -gt 1) {
            exit $LASTEXITCODE
        }
    }

    Write-Output "Rust Core regression gate passed."
    exit 0
} finally {
    Pop-Location
}
