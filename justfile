# Show available commands.
default:
    @just --list

alias install := sync

# Install or refresh cubrid-xref and bundled skills via uv for local use and daily-update-cubrid.
sync:
    #!/usr/bin/env bash
    set -euo pipefail
    export UV_TOOL_BIN_DIR="${CUBRID_XREF_BIN_DIR:-$(uv tool dir --bin)}"
    destination="$UV_TOOL_BIN_DIR/cubrid-xref"
    uv tool install --reinstall .
    "$destination" --help >/dev/null
    "$destination" --version >/dev/null
    printf 'Installed %s\n' "$destination"
    "{{just_executable()}}" install-skills

# Install bundled skills (copy to ~/.agents/skills and link for Claude, Codex, and Antigravity).
install-skills:
    #!/usr/bin/env bash
    set -euo pipefail
    skill_root="{{justfile_directory()}}/skills"
    agents_dir="$HOME/.agents/skills"
    claude_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
    codex_dir="${CODEX_HOME:-$HOME/.codex}/skills"
    agy_dir="$HOME/.gemini/config/skills"
    mkdir -p "$agents_dir" "$claude_dir" "$codex_dir" "$agy_dir"

    link_agent() {
        local target="$1"
        local dir="$2"
        local name="$3"
        local link_path="$dir/$name"
        if [ -L "$link_path" ] || [ -e "$link_path" ]; then
            rm -rf "${dir:?}/${name:?}"
        fi
        ln -s "$target" "$link_path"
    }

    for src in "$skill_root"/*; do
        [ -d "$src" ] && [ -f "$src/SKILL.md" ] || continue
        name="$(basename "$src")"
        staging="$(mktemp -d "${agents_dir}/.${name}.XXXXXX")"
        cp -R "$src/." "$staging/"
        find "$staging" -type f -name '*.pyc' -delete
        find "$staging" -type d -name '__pycache__' -empty -delete
        if [ -L "$agents_dir/$name" ] || [ -e "$agents_dir/$name" ]; then
            rm -rf "${agents_dir:?}/${name:?}"
        fi
        mv "$staging" "$agents_dir/$name"
        if [ "$claude_dir" = "$HOME/.claude/skills" ]; then
            link_agent "../../.agents/skills/$name" "$claude_dir" "$name"
        else
            link_agent "$agents_dir/$name" "$claude_dir" "$name"
        fi
        link_agent "$agents_dir/$name" "$codex_dir" "$name"
        link_agent "$agents_dir/$name" "$agy_dir" "$name"
        printf 'Installed skill %s -> %s\n' "$name" "$agents_dir/$name"
    done

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
