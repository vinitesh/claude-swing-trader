# EC2 Deployment — Step-by-Step

Deploy `swing_platform` to a single Ubuntu EC2 instance running Docker.
Daily `run-live` and `sync` jobs are triggered by host cron, each invoking
a one-shot `docker compose run`.

## Topology

```
┌─────────────────────────────────────────┐
│ EC2 instance (Ubuntu, x86_64)           │
│                                         │
│  ┌──────────────────────────────────┐   │
│  │ host cron                        │   │
│  │   16:05 weekday → docker compose │   │
│  │                  run --rm run-live│   │
│  │   19:00 weekday → docker compose │   │
│  │                  run --rm sync   │   │
│  └─────────────┬────────────────────┘   │
│                ↓                        │
│  ┌──────────────────────────────────┐   │
│  │ Docker Engine                    │   │
│  │  ┌────────────────────────────┐  │   │
│  │  │ swing-platform:latest      │  │   │
│  │  │  swingbot run-live / sync  │  │   │
│  │  └────────────────────────────┘  │   │
│  │  Volumes:                        │   │
│  │    swing_platform_swing-state    │   │
│  │    swing_platform_swing-logs     │   │
│  │    swing_platform_swing-cache    │   │
│  └──────────────────────────────────┘   │
│                                         │
│  /home/ubuntu/swing_platform/.env       │
│   (chmod 600, NEVER in git)             │
└─────────────────────────────────────────┘
```

## Prerequisites you need handled

- EC2 instance: Ubuntu 22.04+ x86_64, t3.small or larger (1 GB RAM is the
  floor; pandas+numpy will OOM on t3.nano during scan).
- Security group: outbound 443 to `*.alpaca.markets` and yfinance/Yahoo.
  Inbound only your home IP on SSH (port 22). No public services exposed.
- ~10GB EBS — Python image is ~1GB, data_cache grows ~30MB, plenty of slack.
- IAM: instance role with `CloudWatchAgentServerPolicy` if you want metrics
  (optional, see "Monitoring").

---

## Step 1 — One-time host setup

SSH in and install Docker. **Skip steps you've already done.**

```bash
# update + base utils
sudo apt-get update && sudo apt-get upgrade -y
sudo apt-get install -y ca-certificates curl gnupg git tzdata

# America/New_York to match strategy clock
sudo timedatectl set-timezone America/New_York

# Docker (official Docker repo, NOT docker.io from Ubuntu — older, missing compose)
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
    sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | \
    sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# allow ubuntu user to run docker without sudo
sudo usermod -aG docker ubuntu
# log out and back in for group change to take effect, OR:
newgrp docker

# verify
docker --version
docker compose version
```

## Step 2 — Get the code on EC2

You have two paths.

### Path A (preferred) — git clone from a remote

If you've pushed `swing_platform` to GitHub/GitLab/etc:

```bash
cd ~
git clone https://github.com/YOUR_USERNAME/swing_platform.git
cd swing_platform
```

If the repo is private, set up a deploy key:
```bash
ssh-keygen -t ed25519 -f ~/.ssh/deploy_key -N ""
cat ~/.ssh/deploy_key.pub   # add this to GitHub as a deploy key (read-only)
# Then clone with the key:
GIT_SSH_COMMAND='ssh -i ~/.ssh/deploy_key' git clone git@github.com:USER/swing_platform.git
```

### Path B (no remote) — scp tarball from laptop

On your laptop:
```bash
cd ~/Documents/Personal
tar --exclude='.venv' --exclude='data_cache' --exclude='backtest_results' \
    --exclude='trading.db' --exclude='.env' --exclude='.git' \
    -czf swing_platform.tar.gz swing_platform/
scp -i ~/.ssh/your-key.pem swing_platform.tar.gz ubuntu@EC2_PUBLIC_IP:~/
```

On EC2:
```bash
cd ~ && tar -xzf swing_platform.tar.gz && rm swing_platform.tar.gz
cd swing_platform
```

## Step 3 — Copy the .env file (NEVER commit this)

On your laptop:
```bash
scp -i ~/.ssh/your-key.pem \
    ~/Documents/Personal/swing_platform/.env \
    ubuntu@EC2_PUBLIC_IP:~/swing_platform/.env
```

On EC2, lock down permissions:
```bash
chmod 600 ~/swing_platform/.env
ls -la ~/swing_platform/.env   # should show -rw-------
```

If `.env` is missing, paper-trading credentials won't load and `run-live`
exits at preflight with a clear message — that's the safety net.

## Step 4 — Build the image

```bash
cd ~/swing_platform
docker compose --profile jobs build
# First build pulls the python:3.11-slim base + uv-installs all deps.
# Takes 3-5 minutes the first time, ~30s thereafter.
```

Verify image:
```bash
docker images swing-platform
```
Expect: ~563MB image, tagged `swing-platform:latest`.

## Step 5 — Smoke test

```bash
# 1. Verify env loads cleanly (does NOT print the secret)
docker compose --profile jobs run --rm run-live-dry --help
# (should print usage and exit 0)

# 2. Real dry-run on full S&P 500 — no orders go to broker
docker compose --profile jobs run --rm run-live-dry
```

Expected output:
```
Run complete — mode=dry-run
  Signals found:     N         # 5-50 typical
  Orders submitted:  N         # all dry-run
  Skipped (dedupe):  0
  Rejected (risk):   0
```

If you see auth errors, the `.env` isn't being read. Check `chmod 600 .env`
and that the file is in `~/swing_platform/.env`.

## Step 6 — Cron scheduling

Edit ubuntu's crontab on EC2:

```bash
crontab -e
```

Add (timezone is system, which we set to America/New_York in Step 1):

```cron
# swing_platform — paper trading
PATH=/usr/bin:/usr/local/bin:/bin

# 4:05pm ET on weekdays — submit signals to Alpaca paper
5 16 * * 1-5 cd /home/ubuntu/swing_platform && /usr/bin/docker compose --profile jobs run --rm run-live >> /home/ubuntu/swing_platform/logs/cron.log 2>&1

# 7:00pm ET on weekdays — reconcile DB ↔ broker
0 19 * * 1-5 cd /home/ubuntu/swing_platform && /usr/bin/docker compose --profile jobs run --rm sync >> /home/ubuntu/swing_platform/logs/cron.log 2>&1
```

Verify:
```bash
crontab -l
sudo systemctl status cron     # should be 'active (running)'
```

## Step 7 — Log rotation

`cron.log` will grow. Add a logrotate config:

```bash
sudo tee /etc/logrotate.d/swing_platform > /dev/null <<'EOF'
/home/ubuntu/swing_platform/logs/cron.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    create 0644 ubuntu ubuntu
}
EOF

# Test
sudo logrotate -d /etc/logrotate.d/swing_platform
```

## Step 8 — Verify cron will fire correctly

Wait for the next 4:05pm ET, OR manually simulate the cron environment:

```bash
# This runs the same command cron will run, with the same env (no shell aliases etc.)
env -i HOME=/home/ubuntu PATH=/usr/bin:/usr/local/bin:/bin SHELL=/bin/sh \
    bash -c 'cd /home/ubuntu/swing_platform && \
             /usr/bin/docker compose --profile jobs run --rm run-live-dry'
```

If this works, the actual cron job will work.

## Step 9 — Monitoring (start minimal)

### Daily check (manual, 30 sec)
```bash
ssh ubuntu@EC2 'cd swing_platform && docker compose --profile jobs run --rm report'
```

### Cron failure detection (10 min one-time setup)

The cleanest signal: was there a row in the `runs` table in the last 36 hours?
If not, cron broke or the job crashed at preflight. Set this up as a daily
sanity check that emails you (or pushes Telegram, if configured):

```bash
# Add to crontab on EC2
0 20 * * 1-5 cd /home/ubuntu/swing_platform && \
    docker compose --profile jobs run --rm shell -c \
    "python -c 'from persistence.db import session_scope; from persistence.models import Run; from datetime import datetime, timedelta; from sqlalchemy import select; \
    s = session_scope().__enter__(); \
    last = s.execute(select(Run).order_by(Run.started_at.desc()).limit(1)).scalar_one_or_none(); \
    age = (datetime.utcnow() - last.started_at).total_seconds() / 3600 if last else 999; \
    print(f\"OK: last run {age:.1f}h ago\") if age < 36 else exit(1)' " \
    || curl -s "https://api.telegram.org/bot$TG_TOKEN/sendMessage" \
            -d "chat_id=$TG_CHAT&text=⚠️ swing_platform: no run in 36h"
```

(Adjust the Telegram credential injection to taste; pulling from `.env` works.)

### CloudWatch (optional, ~$0.30/mo)

If you want OS-level metrics in AWS console:

```bash
sudo apt-get install -y amazon-cloudwatch-agent
# Configure via /etc/amazon-cloudwatch-agent/config.json (skip — minimal default fine)
sudo systemctl enable --now amazon-cloudwatch-agent
```

## Step 10 — Updates / redeploys

To push code changes:

### If using Path A (git):
```bash
ssh ubuntu@EC2 'cd swing_platform && git pull && docker compose --profile jobs build'
```

### If using Path B (tarball):
```bash
# laptop
cd ~/Documents/Personal && tar --exclude='.venv' --exclude='.git' \
    --exclude='data_cache' --exclude='backtest_results' --exclude='trading.db' \
    --exclude='.env' -czf swing_platform.tar.gz swing_platform/
scp -i KEY swing_platform.tar.gz ubuntu@EC2:~/

# EC2
ssh ubuntu@EC2
cd ~/swing_platform-new
tar -xzf ~/swing_platform.tar.gz
# Quick swap — keeps state by reusing same volumes
cd ~/swing_platform && docker compose --profile jobs build
```

The `swing-state` volume is unchanged across rebuilds, so `trading.db`
persists.

## Common issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `permission denied while trying to connect to the Docker daemon` | User not in docker group | `newgrp docker` or log out and back in |
| `cannot connect to docker daemon` | Service not running | `sudo systemctl start docker` |
| `no space left on device` | EBS full (build cache + images) | `docker system prune -af` (removes unused images + cache) |
| Cron fires but no run in DB | Path issue — `docker` not in cron's PATH | Confirm `PATH=...` line at top of crontab includes `/usr/bin` |
| `auth failed` from Alpaca | `.env` wrong or not mounted | `docker compose --profile jobs run --rm shell` then `env \| grep ALPACA` to inspect |
| Container exits with `Permission denied: /app/logs/swingbot.log` | Volume mount permissions | The named volume should handle this — verify you used `docker compose` not direct `docker run` with bind mounts |

## Kill switch (panic close)

If something goes wrong and you need to flatten all paper positions immediately:

```bash
ssh ubuntu@EC2
cd ~/swing_platform
docker compose --profile debug run --rm shell <<'EOF'
python -c "
from core.live_runner import LiveRunner
from core.config import init
s, c = init()
runner = LiveRunner(settings=s, config=c, dry_run=False)
for p in runner.broker.get_positions():
    print(f'closing {p.symbol}')
    runner.broker.close_position(p.symbol)
print('all positions closed')
"
EOF

# Also disable cron so nothing re-opens
crontab -l | grep -v swing_platform | crontab -
```

## Cost estimate

| Item | Monthly |
|------|---------|
| t3.small (2 vCPU, 2 GB) on-demand | $14 |
| t3.small with 1yr no-upfront RI | ~$8 |
| 10 GB EBS gp3 | $0.80 |
| Data transfer out (~50 MB/day to Alpaca) | $0 (under 100 GB free tier) |
| CloudWatch logs (optional) | $0.30 |
| **Total (no RI)** | **~$15/mo** |
| **Total (with RI)** | **~$9/mo** |

If $14/mo feels steep for paper trading, you can downsize to `t4g.small`
(ARM Graviton, $11/mo on-demand) — but you'd need to either build the
image on the instance from scratch (your code is pure Python, so this works)
or use buildx multi-arch builds. We chose x86_64 to keep the first deploy
simple.
