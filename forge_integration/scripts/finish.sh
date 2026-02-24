#!/bin/bash
# Usage: ./finish.sh "CHANGES_DESCRIPTION" [PROJECT_NAME] [COMMIT_TITLE]
# Example: ./finish.sh "Added FORGE supplier enum and ChatOpenAI elif branch in from_config()" "Quivr" "feat(llm): add forge provider"
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

# Check for untracked new files that should be staged
UNTRACKED=$(git ls-files --others --exclude-standard)
if [ -n "$UNTRACKED" ]; then
    echo "⚠️  Untracked files detected (not auto-staged):"
    echo "$UNTRACKED"
    echo ""
    echo "If these are intentional new files, stage them with 'git add <file>' before running finish.sh."
    echo "Proceeding with only tracked file changes..."
    echo ""
fi

# Stage tracked changes
git add -u

# Safety check: list what will be committed
echo "=== Files staged for commit ==="
git diff --cached --name-only
echo ""

echo "=== Commit title ==="
echo "$COMMIT_TITLE"
echo ""

# Commit with full PR description in body
git commit -m "$(cat <<EOF
$COMMIT_TITLE

## Changes

- ${CHANGES}
- Environment variable: FORGE_API_KEY
- Base URL: https://api.forge.tensorblock.co/v1
- Model format: Provider/model-name (e.g., OpenAI/gpt-4o)
- Non-breaking: purely additive, existing providers are untouched

## About Forge

Forge (https://github.com/TensorBlock/forge) is an open-source middleware that routes inference across 40+ upstream providers (including OpenAI, Anthropic, Gemini, DeepSeek, and OpenRouter). It is OpenAI API compatible — works with the standard OpenAI SDK by changing base_url and api_key.

## Motivation

We have seen growing interest from users who standardize on Forge for their model management and want to use it natively with ${PROJECT}. This integration aims to bridge that gap.

## Key Benefits

- Self-Hosted & Privacy-First: Forge is open-source and designed to be self-hosted, critical for users who require data sovereignty
- Future-Proofing: acts as a decoupling layer — instead of maintaining individual adapters for every new provider, Forge users can access them immediately
- Compatibility: supports established aggregators (like OpenRouter) as well as direct provider connections (BYOK)

## References

- Repo: https://github.com/TensorBlock/forge
- Docs: https://www.tensorblock.co/api-docs/overview
- Main Page: https://www.tensorblock.co/
EOF
)"

# Push
git push origin "$(git branch --show-current)"

echo "✅ Pushed to origin/$(git branch --show-current)"
