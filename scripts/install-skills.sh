#!/usr/bin/env sh
# Copy bundled skills into a Hermes skills tree.
#
# Plugin-registered skills resolve through skill_view/skills_list but are NOT
# listed in the system prompt's <available_skills> and do not create slash
# commands. Copying them into $HERMES_HOME/skills/<category>/<name> makes them
# first-class skills; re-run after editing a bundled SKILL.md.
set -eu

plugin_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
hermes_home=${HERMES_HOME:-$HOME/.hermes}
skills_root="$hermes_home/skills"

for src in "$plugin_dir"/skills/*/; do
    [ -f "$src/SKILL.md" ] || continue
    name=$(basename "$src")
    category=$(awk -F': *' '/^[[:space:]]*category:/ {print $2; exit}' "$src/SKILL.md")
    category=${category:-software-development}
    dest="$skills_root/$category/$name"
    mkdir -p "$(dirname "$dest")"
    rm -rf "$dest"
    cp -a "$src" "$dest"
    printf 'installed %s -> %s\n' "$name" "$dest"
done
