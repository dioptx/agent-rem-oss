#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

link_file() {
  local source_path="$1" destination="$2" temp_link current_target
  mkdir -p "$(dirname "$destination")"
  if [[ -e "$destination" || -L "$destination" ]]; then
    if [[ ! -L "$destination" ]]; then
      echo "Refusing to replace existing non-symlink: $destination" >&2
      return 1
    fi
    current_target="$(readlink "$destination")"
    if [[ "$current_target" != "$source_path" ]]; then
      echo "Refusing to replace symlink owned by another source: $destination" >&2
      return 1
    fi
    rm "$destination"
  fi
  temp_link="${destination}.tmp.$$"
  ln -s "$source_path" "$temp_link"
  mv "$temp_link" "$destination"
}

link_file "$repo_dir/bin/rem" "$HOME/.local/bin/rem"
link_file "$repo_dir/commands/agent-rem.md" "$HOME/.claude/commands/agent-rem.md"
link_file "$repo_dir/weft-templates/rem-review-act.json" \
  "$HOME/.claude/weft/templates/rem-review-act.json"
link_file "$repo_dir/weft-templates/rem-review-act.json" \
  "$HOME/.codex/weft-templates/rem-review-act.json"
link_file "$repo_dir/skills/agent-rem" "$HOME/.codex/skills/agent-rem"

echo "Agent REM installed for Claude Code and Codex."
