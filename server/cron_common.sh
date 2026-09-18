# Shared setup for server-side scheduled runs (systemd timers), replacing
# the GitHub Actions `schedule:` triggers that used to drive these jobs.
# Sourced by cron_daily_agent.sh / cron_opening_observer.sh; never run
# directly. Expects REPO_DIR to already be the current directory.
#
# Requires on the host:
#   - /etc/alpha-shadow.env with TUSHARE_TOKEN / XIAODEFA_TOKEN (in addition
#     to the ADMIN_PASSWORD etc. the webapp already reads from there).
#   - /root/.ssh/market_data_push: an SSH deploy key with *write* access to
#     this repo, used only to push the market-data branch.
# See server/RACKNERD_CRON.md for the one-time setup script.

HISTORY_DIR="$PWD/.history"
MARKET_DATA_REMOTE="git@github.com:brycehu129/a-share-alpha-agent.git"

git fetch origin master --quiet
git reset --hard origin/master --quiet

export GIT_SSH_COMMAND="ssh -i /root/.ssh/market_data_push -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

if [ ! -d "$HISTORY_DIR/.git" ]; then
  git clone --branch market-data --single-branch "$MARKET_DATA_REMOTE" "$HISTORY_DIR"
fi
git -C "$HISTORY_DIR" remote set-url origin "$MARKET_DATA_REMOTE"
git -C "$HISTORY_DIR" fetch origin market-data --quiet
git -C "$HISTORY_DIR" reset --hard origin/market-data --quiet
git -C "$HISTORY_DIR" config user.name 'alpha-shadow-cron'
git -C "$HISTORY_DIR" config user.email 'alpha-shadow-cron@localhost'

set -a
[ -f /etc/alpha-shadow.env ] && source /etc/alpha-shadow.env
set +a

REPORT_ID="$(date -u +%Y%m%d%H%M%S)-1"
export REPORT_ID

# Mirrors the GitHub Actions "git add --all; commit; push" step, but skips
# quietly instead of erroring when a run produced nothing new to archive.
# Callers pass either --all or specific paths as "$@" — no `--` separator
# here, since one would make git treat a literal `--all` argument as a
# (nonexistent) pathspec instead of the flag.
commit_and_push() {
  local message="$1"; shift
  git -C "$HISTORY_DIR" add "$@"
  if git -C "$HISTORY_DIR" diff --cached --quiet; then
    echo "Nothing to commit for: $message"
    return 0
  fi
  git -C "$HISTORY_DIR" commit -m "$message"
  git -C "$HISTORY_DIR" push origin HEAD:market-data
}
