#!/bin/bash
# Usage: ./finish.sh "CHANGES_DESCRIPTION" [PROJECT_NAME] [COMMIT_TITLE] [COMMIT_BODY]
# Example: ./finish.sh "Added FORGE supplier enum and ChatOpenAI elif branch in from_config()" "Quivr" "feat(llm): add forge provider"
# If COMMIT_BODY is omitted, a default body listing changes and files is generated.
# Run from inside the repo directory after changes are made.
# Non-interactive — safe for automated use by Claude Code.

set -euo pipefail

CHANGES=${1:-"Add Forge as LLM provider option"}
PROJECT=${2:-$(basename "$(pwd)")}
COMMIT_TITLE=${3:-"feat: Add Forge LLM provider support"}

if [[ "$COMMIT_TITLE" == *$'\n'* ]]; then
    echo "❌ COMMIT_TITLE must be a single line."
    exit 1
fi

if [[ -z "${COMMIT_TITLE// }" ]]; then
    echo "❌ COMMIT_TITLE cannot be empty."
    exit 1
fi

# Show what changed
echo "=== Files changed ==="
git diff --stat
echo ""
echo "=== Changed files ==="
git diff --name-only
echo ""

# Stage tracked changes
git add -u

# Unstage lock files (should never be committed)
LOCK_FILES="uv.lock package-lock.json bun.lockb yarn.lock poetry.lock pnpm-lock.yaml Cargo.lock go.sum Gemfile.lock composer.lock"
for lockfile in $LOCK_FILES; do
    if git diff --cached --name-only | grep -qx "$lockfile"; then
        git reset HEAD "$lockfile" 2>/dev/null
        echo "🔒 Unstaged lock file: $lockfile"
    fi
done

# Auto-stage whitelisted new source/doc files
SOURCE_EXT_PATTERN='\.(py|pyi|ts|tsx|js|jsx|mjs|md|mdx|rst)$'
UNTRACKED=$(git ls-files --others --exclude-standard)
if [ -n "$UNTRACKED" ]; then
    STAGED_NEW=""
    SKIPPED=""
    while IFS= read -r f; do
        if echo "$f" | grep -qE "$SOURCE_EXT_PATTERN"; then
            git add "$f"
            STAGED_NEW="${STAGED_NEW}  ${f}\n"
        else
            SKIPPED="${SKIPPED}  ${f}\n"
        fi
    done <<< "$UNTRACKED"
    if [ -n "$STAGED_NEW" ]; then
        echo "✅ Auto-staged new source/doc files:"
        echo -e "$STAGED_NEW"
    fi
    if [ -n "$SKIPPED" ]; then
        echo "ℹ️  Skipped non-source untracked files:"
        echo -e "$SKIPPED"
    fi
fi

# Safety check: list what will be committed
echo "=== Files staged for commit ==="
git diff --cached --name-only
echo ""

# Check if there are any staged changes before attempting commit
if git diff --cached --quiet; then
    echo "❌ No staged changes to commit."
    echo "Hint: If changes are in untracked files, stage them with 'git add <file>' first."
    exit 2
fi

echo "=== Commit title ==="
echo "$COMMIT_TITLE"
echo ""

# Build commit body
CHANGED_FILES=$(git diff --cached --name-only)
if [ -n "${4:-}" ]; then
    COMMIT_BODY="$4"
else
    COMMIT_BODY="## Changes

- ${CHANGES}

Files modified:
${CHANGED_FILES}"
fi

git commit -m "$(cat <<EOF
$COMMIT_TITLE

$COMMIT_BODY
EOF
)"

# Push
BRANCH="$(git branch --show-current)"
git push origin "$BRANCH"

# Verify push succeeded by comparing local and remote HEAD
LOCAL_HEAD="$(git rev-parse HEAD)"
REMOTE_HEAD="$(git ls-remote origin "refs/heads/$BRANCH" 2>/dev/null | cut -f1)"
if [ -n "$REMOTE_HEAD" ] && [ "$LOCAL_HEAD" != "$REMOTE_HEAD" ]; then
    echo "❌ Push verification failed: local=$LOCAL_HEAD remote=$REMOTE_HEAD"
    exit 3
fi

echo "✅ Pushed to origin/$BRANCH (verified: $LOCAL_HEAD)"
