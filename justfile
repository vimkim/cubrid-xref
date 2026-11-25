# Show available commands.
default:
    @just --list

alias install := sync

# Install or refresh cubrid-xref via uv for local use and daily-update-cubrid.
sync:
    #!/usr/bin/env bash
    set -euo pipefail
    export UV_TOOL_BIN_DIR="${CUBRID_XREF_BIN_DIR:-$(uv tool dir --bin)}"
    destination="$UV_TOOL_BIN_DIR/cubrid-xref"
    uv tool install --reinstall .
    "$destination" --help >/dev/null
    "$destination" --version >/dev/null
    printf 'Installed %s\n' "$destination"

# Install an editable global command that follows edits in this checkout.
install-editable:
    uv tool install --force --editable "$PWD"

# Run the unit and CLI test suite.
test *args:
    PYTHONPATH=src python3 -m unittest discover -s tests -v {{args}}

# Run tests and smoke-check the module CLI entrypoint.
check: test
    PYTHONPATH=src python3 -m cubrid_xref --help >/dev/null
    PYTHONPATH=src python3 -m cubrid_xref --version >/dev/null
