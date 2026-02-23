#!/bin/bash
# Usage: ./prepare.sh OWNER REPO [BASE_BRANCH] [FEATURE_BRANCH]
# Example: ./prepare.sh virattt dexter
# Example: ./prepare.sh assafelovic gpt-researcher master feature/forge-run-001
# If BASE_BRANCH is not specified, auto-detects from upstream HEAD.
# If FEATURE_BRANCH is not specified, generates a unique branch name per run.
# Set AGENTPR_BASE_DIR to change workspace root (default: /Users/yi/Documents/Career/TensorBlcok/agentpr/workspaces).
# Non-interactive — safe for automated use by Claude Code.

set -euo pipefail

OWNER=${1:-}
REPO=${2:-}
OVERRIDE_BRANCH=${3:-}
FEATURE_BRANCH=${4:-}
BASE_DIR=${AGENTPR_BASE_DIR:-"/Users/yi/Documents/Career/TensorBlcok/agentpr/workspaces"}

if [ -z "$OWNER" ] || [ -z "$REPO" ]; then
  echo "Usage: ./prepare.sh OWNER REPO [BASE_BRANCH] [FEATURE_BRANCH]"
  exit 1
fi

if [ -z "$FEATURE_BRANCH" ]; then
  FEATURE_BRANCH="feature/forge-$(date +%Y%m%d-%H%M%S)"
fi

mkdir -p "$BASE_DIR"
cd "$BASE_DIR"

if [ -d "$REPO/.git" ]; then
  echo "Directory $REPO already exists. Skipping fork/clone."
  cd "$REPO"
elif [ -d "$REPO" ]; then
  echo "❌ Directory $REPO exists but is not a git repository."
  exit 1
else
  gh repo fork "$OWNER/$REPO" --clone=true --remote=true
  cd "$REPO"
fi

# Determine base branch
if [ -n "$OVERRIDE_BRANCH" ]; then
  DEFAULT_BRANCH="$OVERRIDE_BRANCH"
  echo "⚠️  Using manually specified branch: $DEFAULT_BRANCH"
else
  DEFAULT_BRANCH=$(git remote show upstream | awk '/HEAD branch/ {print $NF}')
  if [ -z "$DEFAULT_BRANCH" ]; then
    echo "❌ Failed to detect upstream default branch."
    exit 1
  fi
  echo "Auto-detected default branch: $DEFAULT_BRANCH"
fi

echo "Using feature branch: $FEATURE_BRANCH"

# Sync with upstream
git fetch upstream
git checkout "$DEFAULT_BRANCH" 2>/dev/null || git checkout -b "$DEFAULT_BRANCH" "upstream/$DEFAULT_BRANCH"
git merge "upstream/$DEFAULT_BRANCH"
git push origin "$DEFAULT_BRANCH"

# Create or switch to feature branch
if git show-ref --verify --quiet "refs/heads/$FEATURE_BRANCH"; then
  echo "Branch $FEATURE_BRANCH already exists. Switching to it."
  git checkout "$FEATURE_BRANCH"
  git rebase "$DEFAULT_BRANCH"
else
  git checkout -b "$FEATURE_BRANCH"
fi

echo "✅ Ready: $BASE_DIR/$REPO (branch: $FEATURE_BRANCH, base: $DEFAULT_BRANCH)"
echo "⚠️  IMPORTANT: Verify this base branch matches CONTRIBUTING.md requirements!"
