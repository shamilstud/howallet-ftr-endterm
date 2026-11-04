"""Create the synthetic lab users (register -> mark verified in SQL -> login), paced
under the /auth limit. Tokens go to evidence/lab-users.json (git-ignored)."""
import sys
sys.path.insert(0, __import__("os").path.dirname(__file__))
from ftlab import ensure_user, ensure_account, load_users, log, GW, BASE  # noqa: E402

NAMES = ["main", "other", "lock", "l1", "l2", "l3", "l4", "l5"]
for i, n in enumerate(NAMES):
    ensure_user(n, base=GW if i % 2 == 0 else BASE)   # alternate entry points: separate /auth counters
ensure_account("main", "probe-acc")
ensure_account("other", "other-acc")
u = load_users()
for n in NAMES:
    log(n, u[n]["email"], "user", u[n]["user_id"], "household", u[n]["household_id"])
