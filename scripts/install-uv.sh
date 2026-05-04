#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: scripts/install-uv.sh [--dev] [--gui] [--windows] [--venv DIR]

Create a uv-managed virtual environment and install Juicer into it.

Options:
  --dev        Install in editable mode with development dependencies
  --gui        Include the GUI extra
  --windows    Include the Windows extra
  --venv DIR   Virtual environment path (default: .venv)
  -h, --help   Show this help message
USAGE
}

main() {
    local dev=0
    local venv_dir=.venv
    local extras=()

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dev)
                dev=1
                extras+=(dev)
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

    uv venv "$venv_dir"

    local python_bin="$venv_dir/bin/python"
    local package_spec=.
    if [[ ${#extras[@]} -gt 0 ]]; then
        local extra_list
        extra_list=$(IFS=,; echo "${extras[*]}")
        package_spec=".[$extra_list]"
    fi

    local install_args=()
    if [[ $dev -eq 1 ]]; then
        install_args+=(-e)
    fi

    uv pip install --python "$python_bin" "${install_args[@]}" "$package_spec"

    cat <<DONE

Juicer installed in $venv_dir.
Activate the environment with:
  source $venv_dir/bin/activate
Then run:
  juicer --help
DONE
}

main "$@"
