# Phase 0 runbook: recording Binance from AWS Tokyo

What this sets up:

* **One EC2 instance in ap-northeast-1.** It records nine Binance streams around the clock and writes one gzip file per stream per UTC day under `/data/live`:
  * top of book, depth and trades for the 10 spot pairs;
  * top of book, mark price/funding and trades for the 6 perps;
  * top of book and trades for the 87 wide-spread alt pairs found by `research/scan_spreads.py`, plus the USDT books that price them (PLAN.md §16);
  * top of book for the 61 USDⓈ-M perps of those alt coins, the fair value their spot books follow (PLAN.md §17).
* **One S3 bucket in the same region.** Every hour, finished days are moved there under `phase0/<stream>/<day>/`, so the instance's disk stays small.
* **No inbound ports.** You connect through AWS Systems Manager Session Manager (browser shell), and the instance reaches S3 through an IAM role, so no SSH keys or access keys sit on the box.

Perps are recorded even though an Australian retail account may not be able to trade them (PLAN.md §13.5). Their public data is free to record, and perps carry most of the volume, so they are a candidate source of fair value for spot (§13.1).

Rough data volume: on a quiet Sunday the three spot streams came to ~0.4 GB a day compressed (depth 0.24, top of book 0.16, trades 0.04). The alt top of book adds ~0.7 GB a day, mostly from the busy USDT books that price the alt pairs. Perps will add several times the spot streams. The alt perps' top of book is likely the biggest stream (it can't be measured from outside Tokyo), so expect roughly 3–7 GB a day in all, more on busy days; check `ls -la /data/live` after the first day. Check the AWS pricing pages for current rates; a medium Graviton instance plus storage is in the tens of US dollars a month.

## 1. Account basics (once)

1. Turn on MFA for the root user and stop using root. Create a day-to-day admin login (IAM Identity Center, or an IAM user with MFA).
2. Billing → Budgets: create a monthly cost budget with an email alert, e.g. US$50.

## 2. S3 bucket

S3 → Create bucket:

* Region **Asia Pacific (Tokyo) ap-northeast-1**, name e.g. `binance-arb-phase0-<something-unique>`.
* Leave **Block all public access** on and default encryption on. No versioning needed.
* Optional: Management → Lifecycle rule to move objects older than 30 days to a cheaper storage class.

## 3. IAM role for the instance

IAM → Roles → Create role → trusted entity **AWS service: EC2**:

* Attach the managed policy **AmazonSSMManagedInstanceCore** (browser shell access).
* Add an inline policy: paste `ops/iam/recorder-role-policy.json` with `BUCKET` replaced by your bucket name. It can only write under `phase0/` in that bucket.
* Name it `binance-recorder`.

## 4. Pick an availability zone

Tokyo has three AZs. Their names (`ap-northeast-1a/c/d`) map to different physical zones in each AWS account, so compare by **AZ ID** (`apne1-az1`, `apne1-az2`, `apne1-az4`). VPC → Subnets shows both columns. Start the recorder in any of them; step 9 measures which one is closest to Binance.

## 5. Launch the instance

EC2 → Launch instance:

| Setting | Value |
|---|---|
| Name | `binance-recorder` |
| AMI | Amazon Linux 2023, **64-bit (Arm)** |
| Instance type | `c7g.medium` (steady CPU). `t4g.small` is cheaper and fine for recording alone. |
| Key pair | Proceed without a key pair (you'll use Session Manager) |
| Network | Default VPC, subnet in the AZ you picked, **auto-assign public IP: enable** |
| Security group | New, **no inbound rules** (outbound open is the default) |
| Storage | 30 GiB gp3 |
| Advanced → IAM instance profile | `binance-recorder` |

## 6. Connect

EC2 → Instances → select it → **Connect** → **Session Manager** → Connect. You arrive as `ssm-user`, which has sudo. Run `sudo -i` for the set-up steps below.

## 7. Get the code onto the box

The repo is private, so give the instance a read-only deploy key:

```bash
dnf install -y git
ssh-keygen -t ed25519 -f /root/.ssh/arb_deploy -N "" && cat /root/.ssh/arb_deploy.pub
```

Add the printed key in GitHub: repo → Settings → Deploy keys → Add (leave "Allow write access" off). Then:

```bash
GIT_SSH_COMMAND='ssh -i /root/.ssh/arb_deploy -o StrictHostKeyChecking=accept-new' \
  git clone -b claude/great-dijkstra-n3wu1n git@github.com:vivere7108-lab/BinanceSpotArb.git /opt/binance-arb
```

## 8. Install and start the recorders

```bash
cd /opt/binance-arb && BUCKET=<your-bucket> bash ops/bootstrap.sh
```

This installs Python 3.11, creates a `recorder` user and a venv, and starts one `binance-recorder@<stream>` service per file in `ops/recorders/`. It also starts the hourly upload timer, then prints checks. Afterwards:

| Check | Command |
|---|---|
| All recorders running | `systemctl list-units 'binance-*'` |
| Files growing | `ls -la /data/live` |
| A recorder's log | `journalctl -u binance-recorder@spot-bookticker -n 20` |
| Clock synced to Amazon Time Sync | `chronyc tracking` ("System time" should be well under 1 ms) |
| First upload | after 00:10 UTC the next day: `aws s3 ls s3://<your-bucket>/phase0/ --recursive \| head` |

To update the code later: `cd /opt/binance-arb && git pull && BUCKET=<your-bucket> bash ops/bootstrap.sh`.

## 9. Measure latency per AZ

First run the probe on the recorder box itself:

```bash
/opt/binance-arb/.venv/bin/python /opt/binance-arb/research/latency_probe.py --seconds 120
```

Then test the other AZs from copies of the recorder box:

1. EC2 → the recorder instance → Actions → Image and templates → **Create image** (tick **No reboot**).
2. Launch one `t4g.micro` from that image (My AMIs) in each of the other AZ IDs, with the same role and no key pair.
3. On each copy, **first** stop its recorders and clear its data, so it can't upload duplicates into the bucket:

```bash
sudo systemctl disable --now 'binance-recorder@*' binance-upload.timer && sudo rm -f /data/live/*
/opt/binance-arb/.venv/bin/python /opt/binance-arb/research/latency_probe.py --seconds 120
```

Compare **WS API ping** (the order path) and **trade stream lag** (the data path) at p50 and p99. Terminate the copies when done. If another AZ is clearly faster, move the recorder there (launch from the image in that AZ, keep its services, terminate the old one); that's also where the trading engine will go.

## 10. Working with Claude

There are two ways, and neither involves pasting keys or passwords into a chat.

**A. Claude Code on the instance (recommended).** Claude then works directly on the box: checking the services, reading the data, running analyses and latency tests. Nothing leaves AWS.

Give Claude its own clone, so its edits never touch the code the recorders run (`/opt/binance-arb` only changes when you `git pull` and re-run the bootstrap). As `ssm-user`, not root:

```bash
tmux new -s claude                                # survives Session Manager disconnects
curl -fsSL https://claude.ai/install.sh | bash    # Claude Code native installer
ssh-keygen -t ed25519 -f ~/.ssh/arb_claude -N "" && cat ~/.ssh/arb_claude.pub
```

Add that key as a second deploy key, this time **with** write access if you want Claude to push commits from the box. Then:

```bash
GIT_SSH_COMMAND='ssh -i ~/.ssh/arb_claude -o StrictHostKeyChecking=accept-new' \
  git clone -b claude/great-dijkstra-n3wu1n git@github.com:vivere7108-lab/BinanceSpotArb.git ~/BinanceSpotArb
git -C ~/BinanceSpotArb config core.sshCommand 'ssh -i ~/.ssh/arb_claude'
cd ~/BinanceSpotArb && claude remote-control      # log in when prompted
```

The session then shows up in the Claude Code app, where you can carry on the work. Detach from tmux with `Ctrl-b d`; reattach later with `tmux attach -t claude`. It can read `/data/live` and the service logs, and with `sudo` it can restart services.

**B. Read-only S3 access for cloud sessions like this one.** Create an IAM user `claude-readonly` with an inline policy from `ops/iam/readonly-analysis-policy.json` (bucket name filled in) and an access key. In the Claude Code app, open this cloud environment's settings (environment menu in the session title bar → Edit) and add environment variables:

* `AWS_ACCESS_KEY_ID`
* `AWS_SECRET_ACCESS_KEY`
* `AWS_DEFAULT_REGION=ap-northeast-1`

A new session picks them up and can download recordings to analyse. The key can read `phase0/` in that one bucket and nothing else; delete it in IAM when you're done.

**Never** hand over root or admin credentials. Binance API keys, when you have them, belong only on the trading instance: trade-only, withdrawals disabled, restricted to the instance's Elastic IP.

## 11. Analysing a day

Download one day of recordings (from the instance itself, or anywhere with the read-only key):

```bash
aws s3 cp --recursive s3://<your-bucket>/phase0/spot-bookticker/2026-10-05/ day/
aws s3 cp --recursive s3://<your-bucket>/phase0/spot-depth20/2026-10-05/ day/
aws s3 cp --recursive s3://<your-bucket>/phase0/spot-aggtrade/2026-10-05/ day/
```

Then run the tools in `research/`:

```bash
cd research
python analyze_recording.py --bookticker ../day/spot-bookticker-2026-10-05.jsonl.gz --depth ../day/spot-depth20-2026-10-05.jsonl.gz \
    --fees "7.5,USDCUSDT=0,BTCUSDC=7.125,ETHUSDC=7.125,BNBUSDC=7.125" --packet-usdt 100
python analyze_making.py --bookticker ../day/spot-bookticker-2026-10-05.jsonl.gz --trades ../day/spot-aggtrade-2026-10-05.jsonl.gz \
    --fees "7.5,USDCUSDT=0,BTCUSDC=7.125,ETHUSDC=7.125,BNBUSDC=7.125"
python compare_fair_values.py --bookticker ../day/spot-bookticker-2026-10-05.jsonl.gz --trades ../day/spot-aggtrade-2026-10-05.jsonl.gz
```

For the alt candidates (PLAN.md §16), download `spot-alts-bookticker` and `spot-alts-aggtrade` the same way, then:

```bash
python markouts.py --bookticker ../day/spot-alts-bookticker-2026-10-05.jsonl.gz --trades ../day/spot-alts-aggtrade-2026-10-05.jsonl.gz --maker-bps 7.5
```

The perp-anchor test (PLAN.md §17) runs on Binance's public archive rather than on the recordings, so it works for any past day, from anywhere:

```bash
python anchors.py --day 2026-10-05 --coins PEPE,BONK,CYBER,JASMY,NMR --cache ~/archive_cache
```

If a recorder restarted during the day there will also be numbered files (`…-2026-10-05.1.jsonl.gz`); analyse them separately or concatenate them in order (`cat a.gz b.gz > day.gz` gives a valid gzip). A whole day takes a while in Python (roughly an hour for the depth replay). Start with `analyze_recording.py` without `--depth`.
