#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
marker_file="${1:-/tmp/prado-watch-update-$(id -un)}"

cd "$repo_dir"

# Never overwrite local edits or switch branches behind the owner's back.
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "Update skipped: tracked files have local changes."
  exit 0
fi

current_branch="$(git symbolic-ref --quiet --short HEAD || true)"
if [[ -z "$current_branch" ]]; then
  echo "Update skipped: repository is in detached HEAD state."
  exit 0
fi

git fetch --quiet origin "$current_branch"
local_commit="$(git rev-parse HEAD)"
remote_commit="$(git rev-parse "origin/$current_branch")"

if [[ "$local_commit" == "$remote_commit" ]]; then
  exit 0
fi

git merge-base --is-ancestor "$local_commit" "$remote_commit" || {
  echo "Update skipped: local and remote histories have diverged."
  exit 0
}

git merge --ff-only "$remote_commit"
"$repo_dir/.venv/bin/pip" install --quiet -r "$repo_dir/requirements.txt"
touch "$marker_file"
echo "Updated from $local_commit to $remote_commit."
