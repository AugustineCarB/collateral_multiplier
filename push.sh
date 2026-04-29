#!/usr/bin/env bash
# One-shot: initialize git, commit everything, and push to your GitHub repo.
# Run this from inside the collateral-multiplier-proxy folder.
set -euo pipefail

REMOTE="https://github.com/AugustineCarB/collateral_multiplier.git"

git init -q
git add .
git commit -q -m "Initial commit: collateral multiplier proxy (US repo-velocity)"
git branch -M main
git remote remove origin 2>/dev/null || true
git remote add origin "$REMOTE"
git push -u origin main

echo
echo "Pushed. Next:"
echo "  1) GitHub → repo → Settings → Actions → General → 'Workflow permissions' → Read and write."
echo "  2) GitHub → Actions tab → 'Update collateral multiplier proxy' → 'Run workflow' (first manual run)."
echo "  3) After that run finishes, data/collateral_multiplier_proxy.csv will hold the real series."
