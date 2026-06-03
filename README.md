# ⬡ Control Tower V1

AI-powered GitHub PR reviewer and deployment gatekeeper for Creator Campaign OS.

Watches your GitHub repo, reviews PRs via GPT-4, pings you on Telegram, and only pulls you in when a real human decision is needed.

---

## What It Does

- Polls GitHub every 60 seconds for new open PRs
- Skips drafts and already-reviewed PRs
- Waits for CI checks to complete before reviewing
- Sends PR context to GPT-4 for structured review
- Posts review decision as a GitHub PR comment
- Pings you on Telegram with: decision, summary, risks, and what it needs from you
- You reply on Telegram: `approve 14` / `reject 14` / `details 14` / `skip 14`
- On approve: merges PR, Railway auto-deploys from main

---

## Setup (Local)

### 1. Clone and install

```bash
git clone https://github.com/cheuckolate-sketch/control-tower.git
cd control-tower
pip install -r requirements.txt
```

### 2. Create your .env file

```bash
cp .env.example .env
```

Fill in your `.env`:

```
GITHUB_TOKEN=ghp_your_token_here
GITHUB_REPO=cheuckolate-sketch/creator-campaign-os-backend
OPENAI_API_KEY=sk-proj-your_key_here
OPENAI_MODEL=gpt-4o
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=        # leave blank first run
```

### 3. Get your Telegram Chat ID

Leave `TELEGRAM_CHAT_ID` blank. Run the script:

```bash
python main.py
```

It will print your chat ID. Add it to `.env` and restart.

### 4. Run

```bash
python main.py
```

You'll get a Telegram message: **⬡ Control Tower Online**

---

## Deploy to Railway (24/7)

### 1. Push to GitHub

```bash
git init
git add .
git commit -m "Control Tower V1"
git remote add origin https://github.com/cheuckolate-sketch/control-tower.git
git push -u origin main
```

### 2. Create new Railway project

- Go to railway.app
- New Project → Deploy from GitHub repo → select `control-tower`
- Railway detects the Procfile automatically

### 3. Set environment variables in Railway

In Railway → your project → Variables, add all keys from your `.env`:

```
GITHUB_TOKEN
GITHUB_REPO
OPENAI_API_KEY
OPENAI_MODEL
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
POLL_INTERVAL_SECONDS
DAILY_OPENAI_CALL_LIMIT
```

Do NOT commit your `.env` file to GitHub. Add `.env` to `.gitignore`.

### 4. Deploy

Railway auto-deploys on push to main. Check logs in Railway dashboard.

---

## Telegram Commands

| Command | What it does |
|---|---|
| `approve 14` | Merge PR #14 into main |
| `reject 14` | Close PR #14 without merging |
| `details 14` | Show files changed + CI status |
| `skip 14` | Stop alerting about PR #14 |
| `/status` | Show tower health and daily stats |
| `/help` | Show all commands |

---

## Cost Guard

Set `DAILY_OPENAI_CALL_LIMIT` in `.env` to cap GPT-4 calls per day.
Default: 50 calls/day. Each PR review = 1 call when PR AI review is explicitly enabled.
For manual mode, set `DAILY_OPENAI_CALL_LIMIT=0` so Control Tower cannot spend OpenAI calls by default.
When limit is hit, all PRs go to HOLD and Cheuck is alerted.

---

## Files

```
control-tower/
├── main.py                  # orchestrator — run this
├── requirements.txt
├── Procfile                 # Railway worker config
├── .env.example             # copy to .env and fill in
├── .gitignore
└── app/
    ├── github_client.py     # GitHub API wrapper
    ├── reviewer.py          # GPT-4 review engine
    ├── telegram_bot.py      # Telegram notifier + command handler
    └── state.py             # state tracker (avoids duplicate reviews)
```

---

## What Pulls Cheuck In

The Control Tower alerts you for:

**Build issues**
- CI checks failed
- Governance Review failed
- PR has merge conflicts
- Codex changed files outside scope
- Railway deploy failed

**Approval required**
- Live Airtable changes
- Make scenario changes
- Railway env/secrets changes
- OpenAI/Apify paid call logic
- Production writeback
- Merging any risky PR

**Business logic**
- Ranking/scoring logic changes
- AI recommendation logic changes
- Client-facing output changes
- What "shortlisted" means

**Cost/security**
- Missing API keys
- Unexpected usage
- Daily OpenAI limit hit

---

## This is V1 of the AI Agent Incubator

The Control Tower is the execution engine of a broader system:

```
AI Agent Incubator
├── Incubator UI (HTML) — drop PRD, get Build Brief
├── Control Tower (this) — manages build loop, reviews, deploys
└── Deployed Agents — each one a Railway service
    ├── Creator Campaign OS backend ← current
    ├── ABMB Social Listening Agent ← next
    └── ...
```
