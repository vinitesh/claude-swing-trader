# EC2 Migration & Backup Plan

This document covers (a) how state is laid out on the current EC2, (b) how to
back it up cheaply to a private GitHub repo, and (c) how to migrate to a fresh
EC2 instance without losing data.

**Status: planning document only — no automation built yet.** Read this first
when you're ready to migrate or set up backups; the runbook is here for you to
follow manually OR I can build the scripts when you're ready.

---

## What lives where on the box

The platform's state is in three layers, with very different importance:

| Layer | Location | Loss impact | Recovery |
|---|---|---|---|
| **Code** | `~/swing_platform/` | None | `git clone` from your laptop or GitHub |
| **Secrets** | `~/swing_platform/.env` | None — same `.env` is on your laptop | `scp .env` from laptop |
| **Trade DB** | Docker volume `swing_platform_swing-state` → `trading.db` | **High once paper-trading starts** | This is what backups protect |
| **Earnings cache** | Docker volume `swing_platform_swing-cache` → parquet files | None — auto-rebuilds, slower for one day | Just let it warm up |
| **Logs** | Docker volume `swing_platform_swing-logs`, host `~/swing_platform/logs/cron.log` | Low — debugging only | Accept loss |
| **Cron schedule** | `crontab -l` (user-level) | Mild | Re-install from `docs/ec2-deployment.md` |
| **Logrotate** | `/etc/logrotate.d/swing_platform` | Trivial | Re-install from runbook |
| **AWS Security Group** | AWS console — port 8082 → your IP, port 22 → your IP | Mild | Recreate manually |

**Single thing that absolutely cannot be lost: `trading.db` from the swing-state volume.**

---

## Backup strategy: GitHub private repo

### Why GitHub
- Free, unlimited private repos
- `trading.db` compresses to ~1-5 MB (well under GitHub's 100 MB hard limit)
- git history IS the backup history — every push is a versioned snapshot
- No new vendor accounts, no OAuth dance
- Restore from any past day: `git checkout <commit>; gunzip trading.db.gz`

### What it would look like

Separate repo `swing-platform-backups` (NOT the same repo as your code):

```
github.com/<you>/swing-platform-backups (private)
├── trading.db.gz       ← always the latest snapshot
└── README.md           ← restore instructions
```

Each nightly commit is a snapshot in git history. After 30 days you have 30
restorable points. After a year, ~365.

### Daily cron job (when you're ready to build it)

The shape would be:

```bash
# 01:00 ET — backup trading.db to the private GitHub repo
0 1 * * * cd /home/ubuntu/swing-platform-backups && \
    /home/ubuntu/scripts/backup-trading-db.sh \
    >> /home/ubuntu/swing_platform/logs/backup.log 2>&1
```

The script would:
1. Pause the web service for ~5 seconds (SQLite-on-Linux can be backed up
   live with `.backup` API, no need to actually stop it; this gives an
   atomic snapshot without disrupting trading runs)
2. Run `sqlite3 .../trading.db ".backup /tmp/trading.db"` for an atomic copy
3. `gzip /tmp/trading.db` → `~/swing-platform-backups/trading.db.gz`
4. `git -C ~/swing-platform-backups add trading.db.gz`
5. `git commit -m "backup $(date -Is)"`
6. `git push`

### Auth: deploy key

GitHub deploy key with write access to ONLY the backup repo:
1. On EC2: `ssh-keygen -t ed25519 -f ~/.ssh/backup_key -N ""`
2. Copy `~/.ssh/backup_key.pub` to GitHub → backup repo → Settings → Deploy keys → Allow write access
3. Configure git to use this key for backup pushes only:
   ```
   ~/.ssh/config:
   Host github-backup
     HostName github.com
     User git
     IdentityFile ~/.ssh/backup_key
     IdentitiesOnly yes
   ```
4. Clone with the alias: `git clone git@github-backup:USER/swing-platform-backups.git`

### Restore from backup

If `trading.db` corrupts or you want yesterday's state:

```bash
# On EC2 (or any new EC2 during migration)
cd /tmp
git clone git@github-backup:USER/swing-platform-backups.git
cd swing-platform-backups
# To restore from N days ago:
git log --oneline | head -10
git checkout <commit-hash>
gunzip < trading.db.gz > trading.db

# Drop into the volume
docker compose --profile jobs run --rm \
    -v "$(pwd):/restore" shell \
    -c "cp /restore/trading.db /app/state/trading.db && chmod 644 /app/state/trading.db"

# Verify
docker compose --profile jobs run --rm report
```

### Cost / size budget

- Per night: 1-5 MB committed
- Per year: ~365 commits, ~1-2 GB total in repo
- GitHub starts grumbling around 1 GB but enforces hard limits at 5 GB
- **Annual cleanup**: every Jan 1, `git filter-branch` to drop commits older than 6 months

---

## Migration runbook (manual, one-time)

When you actually need to migrate to a new EC2 box, follow these steps **in order**. Time estimate: ~1 hour.

### Pre-flight (on your laptop)
```bash
# Make sure your local repo is clean and pushed to wherever you keep it
cd ~/Documents/Personal/swing_platform
git status
git log --oneline | head -5
```

### Step 1: provision new EC2
- Ubuntu 26.04 LTS, x86_64, t3.small or larger (1 GB RAM minimum; more is fine)
- 30+ GB EBS gp3
- Same VPC / subnet / SG as old box, OR new SG with these inbound rules:
  - 22/tcp from your IP
  - 8082/tcp from your IP

### Step 2: install Docker on new box
Same one-liner from `docs/ec2-deployment.md`. ~5 min.

### Step 3: deploy code to new box
Two options:

**Option A — git clone (preferred)** if you've pushed to GitHub:
```bash
ssh new-ec2 'git clone https://github.com/USER/swing_platform.git ~/swing_platform'
```

**Option B — tarball (no git remote)**:
```bash
# laptop
cd ~/Documents/Personal
tar --exclude='swing_platform/.venv' --exclude='swing_platform/data_cache' \
    --exclude='swing_platform/backtest_results' --exclude='swing_platform/.env' \
    --exclude='swing_platform/.git' \
    -czf /tmp/swing_platform.tar.gz swing_platform/

scp -i KEY /tmp/swing_platform.tar.gz ubuntu@new-ec2:~/

# new ec2
ssh new-ec2 'cd ~ && tar xzf swing_platform.tar.gz && find swing_platform -name "._*" -delete'
```

### Step 4: copy `.env` from laptop
```bash
scp -i KEY ~/Documents/Personal/swing_platform/.env ubuntu@new-ec2:~/swing_platform/.env
ssh new-ec2 'chmod 600 ~/swing_platform/.env'
```

### Step 5: build the image on new box
```bash
ssh new-ec2 'cd ~/swing_platform && docker compose build web 2>&1 | tail -3'
```
~3-5 min.

### Step 6: migrate `trading.db`

#### Option A — from GitHub backup (if backups are running)
```bash
ssh new-ec2 'git clone git@github-backup:USER/swing-platform-backups.git /tmp/restore
cd /tmp/restore
gunzip < trading.db.gz > trading.db
# Create the volume by running any service once, then drop the file in
cd ~/swing_platform
docker compose --profile jobs run --rm shell -c "true"  # creates volumes
docker run --rm \
    -v swing_platform_swing-state:/state \
    -v /tmp/restore:/restore alpine \
    cp /restore/trading.db /state/trading.db'
```

#### Option B — direct from old box (most reliable)

On old EC2:
```bash
ssh old-ec2 'cd ~/swing_platform && \
    docker compose --profile jobs run --rm shell -c \
    "sqlite3 /app/state/trading.db .backup /app/state/migrate.db"
docker run --rm -v swing_platform_swing-state:/state alpine \
    cat /state/migrate.db > /tmp/trading.db'

# pull to laptop
scp -i KEY ubuntu@old-ec2:/tmp/trading.db /tmp/trading.db
# push to new
scp -i KEY /tmp/trading.db ubuntu@new-ec2:/tmp/trading.db
ssh new-ec2 'cd ~/swing_platform && docker compose --profile jobs run --rm shell -c "true"
docker run --rm -v swing_platform_swing-state:/state -v /tmp:/in alpine \
    cp /in/trading.db /state/trading.db'
```

### Step 7: smoke test the new box
```bash
ssh new-ec2 'cd ~/swing_platform && \
    docker compose --profile jobs run --rm run-live-dry 2>&1 | tail -5
docker compose --profile jobs run --rm report 2>&1 | tail -10'
```
Expected: dry-run finds the same number of signals as old box; report shows
your historical data.

### Step 8: install cron on new box
Re-run the crontab block from `docs/ec2-deployment.md` Step 6.

### Step 9: STOP cron on the old box
**This is the critical step. Do NOT skip.** If both boxes' crons fire at 4:05 PM,
both submit orders for the same signals to your single Alpaca account, doubling
positions.

```bash
ssh old-ec2 'crontab -l > /tmp/old-cron-backup.txt
crontab -r
echo "old cron removed; backup at /tmp/old-cron-backup.txt"'
```

### Step 10: start the web service on new box
```bash
ssh new-ec2 'cd ~/swing_platform && docker compose up -d web
sleep 5
curl -sS http://127.0.0.1:8082/healthz'
```

### Step 11: update AWS Security Group
- Old SG: keep open (you may need SSH to old box for verification)
- New SG: add port 8082 inbound from your IP

### Step 12: verify from your browser
- New EC2 public hostname:8082
- Login with WEB_PASSWORD from `.env`
- Dashboard should show all historical runs, strategies, P&L
- /charts page should render with data

### Step 13: terminate old EC2
**Wait at least 24 hours** to verify the new box runs through one full day's
cron cycle correctly. Only then terminate the old instance.

```
AWS console → EC2 → old instance → Instance state → Terminate
```

---

## Gotchas list (read before migrating)

These are the things that bite people during EC2 migrations:

### 1. Cron double-fire
The single biggest risk. Old box keeps running until you `crontab -r` it. **Always disable old box's cron BEFORE enabling new box's cron** for the SAME trading day.

### 2. Public IP changes
New EC2 = new public IP. Update:
- `~/.ssh/config` — your `ec2-swing` alias HostName
- AWS SG inbound rules — your IP is unchanged but the URL you bookmark for the dashboard moves
- Any browser bookmarks
- If you set up a static Elastic IP earlier, you can transfer it. Otherwise the IP is ephemeral.

### 3. Cron has a "missed slot" problem
If old box stops at 3:00 PM and new box doesn't have cron installed until 4:30 PM, the 4:05 PM run-live for that day is just lost. Either:
- Migrate on a weekend (no cron jobs to lose)
- Manually trigger `run-live` post-migration to compensate

### 4. Container UID mismatches
Container's `swing` user is UID 1000. Ubuntu's default user is also UID 1000. If the new EC2 uses a different OS (Amazon Linux, Debian, RHEL) the UIDs may differ. The container's bind-mount on `./config` could break with permission errors. **Test with dry-run before trusting**, and if needed adjust the entrypoint.

### 5. Idempotency only protects within ONE database
If old DB has signal `(2026-06-01, rsi2, AAPL)` and you fail to migrate the DB cleanly, the new box thinks today's signal is fresh and re-submits. **Always migrate the DB BEFORE running anything on the new box.**

### 6. `.env` must be valid before image build
The Docker image bake doesn't depend on `.env`, but the FIRST `compose run` command does. Test order: build image → scp `.env` → run dry-run.

### 7. yfinance cache is throwaway
The new box will need to re-fetch ~503 symbol bars on first run. This typically takes ~5 min and may temporarily hit yfinance soft rate limits. Acceptable on migration day; not catastrophic.

### 8. Telegram + Alpaca credentials are NOT IP-bound
Same `.env` on a different box works fine. No need to rotate keys for migration.

### 9. AWS Security Group: closing the old SG too early
Don't delete the old box's SG until the old box is terminated. AWS sometimes lets you delete a SG referenced by a still-running instance, then complains about it later.

### 10. EBS snapshots are an alternative
For "literal byte-for-byte clone", you can EBS-snapshot the old box's root volume and attach it to a new instance. This skips the whole migration process. Caveat: same Ubuntu version, same kernel, less portable.

---

## Backup retention policy (when you build it)

Suggested cron jobs:

| Time | Job |
|---|---|
| 01:00 ET daily | git push trading.db to backup repo |
| 02:00 ET daily | warm-earnings (already running) |
| Sunday 03:00 ET | `git gc --aggressive` on backup repo |
| 1st of month | log rotate trading-N.db.gz files older than 30 days into a `history/` folder |

Total cost: $0.

---

## When YOU say "let's build it"

I'll:
1. Write `scripts/backup-trading-db.sh` — the daily snapshot script
2. Write `scripts/restore-from-backup.sh` — for restore-from-N-days-ago
3. Write `scripts/migrate-to-new-ec2.sh` — automates the steps in this runbook
4. Set up the deploy key + private repo (you create the repo, I configure)
5. Add the cron entry on EC2
6. Smoke-test by simulating a full migration to a temp instance

Estimated build time: ~2-3 hours. We'd do it after some real paper trading
data has accumulated so we have something meaningful to back up.
