#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: scripts/install-uv.sh [--dev] [--gui] [--windows] [--venv DIR]

Create a uv-managed virtual environment and install Juicer into it.

Options:
  --dev        Install development dependencies (group: dev)
  --gui        Include the GUI extra (PySide6)
  --windows    Include the Windows extra (pywin32)
  --venv DIR   Virtual environment path (default: .venv)
  -h, --help   Show this help message
USAGE
}

main() {
    local dev=0
    local venv_dir=.venv
    local extras=()
    local groups=()

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dev)
                dev=1
                groups+=(dev)
                ;;
            --gui)
                extras+=(gui)
                ;;
            --windows)
                extras+=(windows)
                ;;
            --venv)
                if [[ $# -lt 2 ]]; then
                    echo "error: --venv requires a path argument" >&2
                    return 2
                fi
                venv_dir=$2
                shift
                ;;
            -h|--help)
                usage
                return 0
                ;;
            *)
                echo "error: unknown option: $1" >&2
                usage >&2
                return 2
                ;;
        esac
        shift
    done

    if ! command -v uv >/dev/null 2>&1; then
        echo "error: uv is not installed. Install it from https://docs.astral.sh/uv/getting-started/installation/" >&2
        return 1
    fi

    local script_dir
    script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
    local repo_root
    repo_root=$(cd -- "$script_dir/.." && pwd)
    cd "$repo_root"

    # Build the uv sync argument list.
    local sync_args=()
    if [[ $dev -eq 0 ]]; then
        sync_args+=(--no-group dev --no-group build)
    fi
    for extra in "${extras[@]+"${extras[@]}"}"; do
        sync_args+=(--extra "$extra")
    done
    for group in "${groups[@]+"${groups[@]}"}"; do
        sync_args+=(--group "$group")
    done
    if [[ -n "$venv_dir" && "$venv_dir" != ".venv" ]]; then
        sync_args+=(--venv "$venv_dir")
    fi

    uv sync "${sync_args[@]+"${sync_args[@]}"}"

    cat <<DONE

Juicer installed in $venv_dir.
Activate the environment in a Unix-like shell with:
  source $venv_dir/bin/activate
Then run:
  juicer --help
Windows PowerShell users should run scripts/install-uv.ps1 instead.
DONE
}

main "$@"
