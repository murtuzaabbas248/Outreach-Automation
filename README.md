# Guest Post Outreach Dashboard

A local web dashboard for preparing, reviewing, approving and sending personalized guest post
outreach emails, one at a time, **only after your explicit approval and a final confirmation**.

- Python + FastAPI backend, SQLite database, plain HTML/CSS/JavaScript dashboard
- No paid AI API: topics and emails come from curated templates and rule-based personalization
- Sending via **Gmail API (OAuth 2.0)**, **SMTP**, or **Dry run** (sends nothing; this is the default)

> This tool cannot guarantee inbox placement or delivery. Follow your email provider's terms and
> sending limits, and the anti-spam and privacy laws that apply to you (e.g. CAN-SPAM, GDPR, CASL).

---

## 1. One-time setup (Windows, VS Code)

You need **Python 3.11+**. This project's `.venv` and the Docker image use Python 3.14
(`winget install Python.Python.3.14`).

Open this folder in VS Code, then open a terminal (**Terminal → New Terminal**) and run:

```powershell
# 1. create a virtual environment (an isolated place for this project's packages)
py -3.14 -m venv .venv

# 2. activate it (your prompt will start with (.venv))
.\.venv\Scripts\Activate.ps1

# 3. install the packages
pip install -r requirements.txt

# 4. create your settings file
Copy-Item .env.example .env

# 5. create the database
python -m app.init_db
```

If step 2 says *"running scripts is disabled"*, run this once and try again:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

In VS Code, press `Ctrl+Shift+P` → **Python: Select Interpreter** → choose the one in `.venv`.

Open `.env` and set at least `TEST_RECIPIENT` and `SENDER_EMAIL` to your own address.

## 2. Run the app

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8000
```

Open **http://127.0.0.1:8000** in your browser. Press `Ctrl+C` in the terminal to stop it.
Or double-click **Start Outreach App.bat** (runs it in the background and opens the browser) and
**Stop Outreach App.bat** to stop it.

Locally, the app only listens on your own computer (127.0.0.1) and needs no password. Set
`APP_PASSWORD` in `.env` to turn on the login page. On a server a password is required: without
`APP_PASSWORD` the hosted app refuses every request. To host it on Render, see [DEPLOY.md](DEPLOY.md).

## 3. Daily workflow

| Step | Page | What happens |
|---|---|---|
| 1 | **Import contacts** | Upload `.csv` / `.xlsx`. Duplicates are skipped and invalid emails flagged. Try `sample_data/sample_contacts.csv`. |
| 2 | **Manage contacts** | Fix issues, edit and remove contacts. Tick **verified** only for addresses the site publishes for editorial enquiries. Unverified addresses can't be sent to. |
| 3 | **Guest post topics** | Get 3 suggestions per site (from its niche + category URL), or type your own. |
| 4 | **Generate drafts** | Up to 30 at a time. **Generating never sends.** |
| 5 | **Review & approve** | Preview, edit, approve, reject or delete. Editing an approved draft removes its approval. |
| 6 | **Sending queue** | Select approved drafts → **Prepare batch** → check the confirmation screen (count, recipients, subjects, full messages) → type `SEND` → start. **Cancel sending** stops anything not yet sent. |
| 7 | **Sent history** | Every attempt with timestamp, recipient, subject, provider response and result. |
| 8 | **Follow-ups & replies** | Follow-ups are optional and off by default. One per email, each needs its own approval. With Gmail connected, the app also detects replies and bounces (bell icon): a reply marks the contact *Replied* and cancels its follow-up, a bounce marks it *Bounced*. Nothing in Gmail is sent, deleted or changed. |

### Contact file columns

`Website Name, Website URL, Contact Name, Email, Niche, Relevant Article URL, Suggested Topic,
Personalization Notes, Outreach Status, Email Verified`. Header spelling is flexible ("Contact Email",
"Editor Name", …). Download a template from the Import page.

**Niche** can be any text. It's matched to one of: Education, EdTech, Technology & Digital Innovation,
Business Services, Business Growth & Research, Software & Digital Transformation.

**Personalization notes** are private reminders. They are shown while you edit, but never inserted
into emails automatically, so nothing you didn't write ends up in a message.

## 4. Safety rules built into the code

1. Generating drafts never sends anything.
2. Approval is stored in the database with a fingerprint (hash) of recipient + subject + body.
3. Only approved, unchanged, valid, verified, non-suppressed and not-previously-contacted drafts can enter a batch.
4. Preparing a batch shows a full confirmation screen and creates a one-time token.
5. Sending starts only when the token **and** the typed word `SEND` are submitted.
6. Every email is re-checked immediately before it is sent. Edited, rejected or unapproved drafts are skipped.
7. **Cancel sending** cancels everything not yet sent.
8. If the delivery result is uncertain (timeout, server error, app crash), the email is marked
   **uncertain**, the queue stops and nothing is retried. Check your Sent folder, then click
   *It was sent* / *It was not sent* in **Sent history**.
9. The queue also stops on expired authorization, provider rate limits, or the daily limit.
10. Each message goes to exactly one recipient, never as a group/CC/BCC mailing.
11. Batches are capped at 30. The delay between emails (min 5s) and the daily limit are configurable.
12. Tokens, passwords and secrets are never written to logs or the database.

The test `tests/test_sending_safety.py::test_unapproved_drafts_cannot_be_sent_via_direct_api_calls`
proves unapproved drafts can't be sent even when the dashboard is bypassed and the API, the
database, or the sending worker are called directly.

## 5. Recommended first run

1. Leave **Provider = Dry run**, import the sample file, and go through every step. Nothing is sent.
2. Configure Gmail (below), set **Provider = Gmail API** and keep **Test mode ON**. Send a small
   batch: every email arrives in *your* inbox with `[TEST]` in the subject.
3. Only then turn test mode off for real outreach. The badge at the top shows the current mode.

## 6. Gmail API setup (OAuth 2.0)

The app asks for two permissions: `gmail.send` (to send your approved emails) and `gmail.readonly`
(to detect replies and bounces to emails it sent). It never deletes, changes or sends anything on its
own. Older tokens that only have `gmail.send` still work for sending; reply detection then stays off
until you click **Reconnect Gmail**.

This section is for running the app on your computer (**Desktop app** client). For the hosted app,
DEPLOY.md uses a **Web application** client instead.

1. Go to <https://console.cloud.google.com/> and create a project (e.g. "Outreach").
2. **APIs & Services → Library** → search **Gmail API** → **Enable**.
3. **APIs & Services → OAuth consent screen** (shown as "Google Auth Platform" in newer consoles):
   - User type **External**, app name e.g. "Outreach Dashboard", your email as support/developer contact.
   - Under **Audience / Test users**, add your own Gmail address.
   - Under **Data access / Scopes**, you may add `.../auth/gmail.send`.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID** → type **Desktop app**.
5. Click **Download JSON**, create a folder named `credentials` in this project, and save the file as
   `credentials/client_secret.json`. (The folder is git-ignored.)
6. Start the app → **Settings** → **Connect Gmail**. A browser tab opens. Sign in and allow access.
   (Google may warn that the app is unverified. That's expected for your own test-mode app: click
   *Continue*.) Alternatively run `python -m app.gmail_auth` in the terminal.
7. Set **Provider = Gmail API**, keep **Test mode** on, and save.

While your Google Cloud app is in "Testing" status, the authorization expires after about 7 days.
When it does, the queue stops safely with an "authorization expired" message. Just click
**Reconnect Gmail**.

Gmail limits how much you can send per day (lower for new and free accounts). Set **Daily sending
limit** in Settings well below that limit.

### SMTP alternative

Set in `.env`:

```
EMAIL_PROVIDER=smtp
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=your-16-character-app-password
```

For Gmail this requires 2-Step Verification and an **App password**
(Google Account → Security → App passwords). Restart the app after editing `.env`, then choose
**SMTP** in Settings.

## 7. Compliance features

- **Do-not-contact list** (bottom of Manage contacts). Suppressed addresses can't get drafts or emails.
- Marking a contact **opted out** / **do not contact** adds them to the list and cancels their pending drafts and follow-ups.
- Marking a contact **replied** cancels its pending follow-up.
- Every email ends with a clear opt-out sentence and your real name and signature. Add your
  postal address in Settings if the laws that apply to you require it.
- An address that was contacted can't be contacted again by a new initial email (duplicate prevention across batches).
- Removing a contact removes its unsent drafts. Sent history is kept for your records.

## 7a. Website discovery

The **Website discovery** page finds websites in your four niches (Education, Technology, Biometric
Devices, Business Services) that publish contributor or "write for us" pages. It records their contact
details, DA and DR, and saves everything to `output/niche_websites_outreach.csv`.

**Discovery never creates drafts and never sends email.** A website only enters the outreach workflow
when you select it and click **Add selected to outreach contacts**. The new contact starts *unverified*,
so it can't be sent to until you check the address's source and mark it verified in **Manage contacts**.

```
discovery → database + CSV → topic & notes → Add to outreach contacts → verify source
→ Generate drafts → Review & approve → Prepare batch → type SEND
```

### What it does on each run

1. Runs search queries for each niche (e.g. `biometrics "write for us"`). Results are cached for 14 days,
   so you don't pay for the same query twice.
2. Normalizes domains (`https://www.Site.com/?utm_source=x` → `site.com`) and skips duplicates, social
   networks, blog platforms and "500 sites that accept guest posts" lists.
3. Visits each new website politely: it obeys robots.txt, waits between requests, and makes at most
   6 requests per site. It looks at the result page, the homepage, the guidelines page, a contact page
   and one related article.
4. Qualifies it. **Qualified** means a relevant niche, a guidelines page that says it accepts
   contributions, and a public email. **Needs Review** means something is missing, unclear, or the site
   blocked automated access. **Rejected** means irrelevant, spam or link-selling, or the site says it
   doesn't accept guest posts. The evidence and a confidence score are shown under **Details**.
5. Writes one topic and short personalization notes, based only on what was found. It says whether an
   article page was actually retrieved or only linked.
6. Checks emails and fetches DA/DR within your limits, then updates the database and the CSV.

It never bypasses CAPTCHAs, logins or bot checks. Never decodes hidden or obfuscated emails. Never
fills a missing field with a guess. Missing values stay blank or `Unknown`.

### Claude auto-discovery (automatic, uses your Claude plan)

On *Website discovery*, **Find new websites with Claude** starts Claude Code (`claude -p`) in the
background. Claude runs web searches for your niches, skips domains already in the database and searches
it ran before, and looks up DR with Ahrefs' free Domain Rating lookup. It replies with the URLs and DR
values. The app then imports the URLs, inspects each site with its own crawler, and stores the DR with
its source. New sites appear in the table after about 5 to 15 minutes.

* **What Claude can do:** web search and the free Ahrefs DR lookup only. Shell commands, file edits,
  web fetching, Gmail, Drive and Sheets are disallowed. It runs in an empty folder (`data/claude_agent`),
  not the project, and never writes to the database.
* **Cost:** it uses your Claude subscription's usage allowance, not an API key. `ANTHROPIC_API_KEY` is
  removed from its environment, so it can't fall back to paid API billing.
* **Daily:** tick *Run automatically once a day when the app starts* to get a round each day. It's off by
  default.
* **Settings** (`.env`, optional): `CLAUDE_DISCOVERY_PER_NICHE` (default 8), `CLAUDE_DISCOVERY_MAX_TURNS`
  (40), `CLAUDE_DISCOVERY_TIMEOUT_SECONDS` (1500), `CLAUDE_CLI_PATH` (blank = find `claude` automatically).

### Free discovery workflow (default, no API keys, no cost)

Without a search API key, discovery uses the free **manual search + URL import** method. The
dashboard's **Discovery method** panel shows which method is active and why automatic search is off.

1. **Generate searches.** In *Website discovery → Free discovery workflow*, pick a niche (and optionally
   type your own keywords, e.g. `smart classroom, attendance system`), then click **Generate searches**.
   Each query has **Google / Bing / DuckDuckGo / Brave** buttons that open it in *your* browser.
2. **Collect the result URLs** in any of these ways:
   * select the results on the page, copy, and paste them into the box (URLs are picked out of the text);
   * press `Ctrl+S` on a results page, or on a "blogs that accept guest posts" list, and upload the saved
     `.html` (search-engine redirect links are decoded automatically);
   * drag the **Collect links** button to your bookmarks bar, click it on a results page, and paste;
   * paste a plain list of domains or URLs, one per line.
3. Optionally click **Import results for this** next to the query, so it's recorded as the
   *Discovery Query*. Then click **Import & inspect**.
4. Duplicates are removed by root domain against your database and the queue. Search engines, social
   networks and blog platforms are skipped. Every new site is then inspected politely, as described
   below: guidelines page, contact page, related article, public emails.
5. Results appear in the table with a status of Qualified / Needs Review / Rejected. Up to
   `DISCOVERY_IMPORT_MAX_PER_RUN` (default 50) imported sites are inspected per run. The rest wait and
   are processed by **Process pending imports now** or the next run.

From the terminal: `python -m app.discovery queries --niche education` prints queries and search links;
`python -m app.discovery import urls.txt --niche education` queues a file; `python -m app.discovery run`
inspects the queue.

The app never sends queries to search engines itself, never scrapes result pages, and never bypasses
CAPTCHAs, logins or rate limits. A free *automatic* option is your own SearXNG server
(`SEARCH_PROVIDER=searxng`). A paid provider without a key falls back to the free workflow; nothing ever
switches to a paid service on its own.

**DA / DR** stay **Not Checked** unless a real provider supplied them: an API key below, Claude
auto-discovery (free Ahrefs DR lookup), or values you import with
`python -m app.discovery import-metrics FILE.json --metric dr --source "..."` (JSON `{domain: value}`).
Each website's *Details* links to the free Ahrefs and Moz checkers so you can look a site up yourself. **Email status** stays **Not
Checked** unless a verification service is configured.

### API keys (all optional, set in `.env`)

| What | Setting | Notes |
|---|---|---|
| Web search | `SEARCH_PROVIDER=manual` (default) | Free manual workflow above. No key. |
| | `SEARCH_PROVIDER=brave` + `BRAVE_API_KEY` | Brave Search API, <https://api-dashboard.search.brave.com>. Pay-per-request (about $5 per 1,000), with a monthly free credit. A card is required. |
| | `google_cse` + `GOOGLE_CSE_API_KEY`, `GOOGLE_CSE_ID` | Google closed this API to new customers. Existing keys stop working on **1 Jan 2027**. |
| | `searxng` + `SEARXNG_URL` | Free, but you run your own SearXNG server, with JSON output enabled. |
| DA (Moz) | `MOZ_API_TOKEN` | Moz API token (Moz account → API). Plans and quotas are set by Moz. |
| DR (Ahrefs) | `AHREFS_API_KEY` | Ahrefs API v3 key (Account settings → API keys). The default `AHREFS_DR_ENDPOINT=free` uses Ahrefs' free Domain Rating endpoint. `paid` uses the Site Explorer endpoint, which costs API units on a paid plan. Data shown as "Domain Rating by Ahrefs". |
| Email checks | `EMAIL_VERIFY_PROVIDER=hunter` + `HUNTER_API_KEY`, or `zerobounce` + `ZEROBOUNCE_API_KEY` | Without one, addresses stay **Not Checked**. |

Without any keys, discovery still runs. It inspects the URLs you import, re-checks known websites and
keeps the CSV in sync. Restart the app after editing `.env`.

**Email Verified** values: `Verified` (a service confirmed the mailbox), `Not Checked` (well formed, no
service checked it), `Syntax Valid` (same meaning; only on older records, which are left unchanged), `Catch-All` (domain accepts everything, so the mailbox is unconfirmed), `Invalid` (the
service said so), `Unknown`. Syntax alone is never reported as Verified.

DA (Moz) and DR (Ahrefs) are different metrics from different companies. They are shown separately and
are not a measure of quality on their own.

### When it runs

* **When the dashboard starts:** once per day (local date), in the background. Set
  `DISCOVERY_AUTO_ON_STARTUP=false` to turn this off.
* **When you open this folder in VS Code:** the task *Website discovery (once per day)* in
  `.vscode/tasks.json`. Allow it once: `Ctrl+Shift+P` → **Tasks: Manage Automatic Tasks** → **Allow
  Automatic Tasks**.
* **Any time:** the **Run Website Discovery Now** button, the VS Code task *Run Website Discovery Now*, or
  `python -m app.discovery run`.
* **Optional, even when VS Code is closed:** `.\scripts\register_daily_discovery_task.ps1 -Time 09:00`
  creates a Windows Task Scheduler job for your user, which runs only while you're logged in.
  Remove it with `.\scripts\unregister_daily_discovery_task.ps1`.

Only one run happens at a time, and a run that completed today is not repeated automatically. If the
app or computer stops mid-run, the run is marked *interrupted*. Every website already processed was
saved individually, and the next run carries on. Nothing runs while the app and VS Code are closed,
unless you set up the scheduled task.

Limits (`.env`): `DISCOVERY_DAILY_LIMIT` (new websites per run), `DISCOVERY_MAX_SEARCHES` (searches per
run), `DISCOVERY_MAX_SEARCHES_MONTH`, `DISCOVERY_MAX_REQUESTS_PER_DOMAIN`, `METRICS_MAX_LOOKUPS_PER_RUN`,
`EMAIL_VERIFY_MAX_PER_RUN`. Results and API usage per run are shown on the page and appended to
`data/discovery.log` for terminal and scheduled runs.

### The CSV file

`output/niche_websites_outreach.csv` (UTF-8 with a BOM so Excel shows accents correctly). Columns, in
this order:

```
Website Name,Website URL,Contact Name,Email,Niche,Relevant Article URL,Suggested Topic,Personalization Notes,Outreach Status,Email Verified,DA,DR,Guest Post Guidelines URL,Contact Source URL,DA Source,DR Source,Last Checked,Discovery Query,Discovery Status
```

* The database is the source of truth. Each export contains **all** websites, including your existing
  outreach contacts, never just the new ones. The previous file is kept as
  `niche_websites_outreach.backup.csv`.
* Edits you make in the CSV are merged back on the next export. Added rows become *Needs Review*, and
  blank contact names, notes or topics you filled in are copied over. Nothing in the database is
  overwritten from the CSV, and DA/DR typed into the CSV are ignored.
* If the file is open in Excel, the export is saved under a timestamped name instead.
* Check the column order: `python -c "from app import csv_sync; print(csv_sync.read_header())"`
* The page has **Export all** and **Export filtered** buttons. The file's first 10 columns match the
  Import contacts format.

Outreach Status in the CSV comes from the real contact, draft and sending records: New, Draft Generated,
Awaiting Approval, Approved, Sent, Replied, Rejected, Bounced, Do Not Contact. Discovery never resets it.
Marking a website **Do Not Contact** adds its email to your suppression list.

A website already contacted at one address can't be pitched again through another address at the same
domain, in addition to the existing per-address duplicate check.

## 8. Running the tests

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest -q
```

Tests use a temporary database and a fake email provider, so they never send real email. They cover:
CSV/Excel import, duplicate detection, email validation, topic and draft generation, approval
recording, rejection, edit-after-approval, unauthorized-sending attempts, batch and daily limits,
definite/uncertain/auth/rate-limit failures, crash recovery, test and dry-run modes, suppression,
opt-out and follow-up cancellation.

Other test files cover free discovery (`test_free_discovery.py`), Claude auto-discovery
(`test_claude_agent.py`), reply and bounce detection (`test_replies.py`) and the hosted login
(`test_hosting.py`).

`tests/test_discovery.py` (all network traffic mocked) covers search providers and errors, domain
normalization, qualification of relevant, spam, irrelevant, closed and robots-blocked sites, duplicate
detection, DA/DR parsing, validation and caching, email-verification statuses, CSV column order,
escaping and merging, run-once-per-day, failed and interrupted runs, concurrent runs, preservation of
outreach history, website-level duplicate outreach, and that discovery never drafts or sends email.

## 9. Project structure

```
app/
  main.py         API routes (FastAPI)
  config.py       settings from .env
  db.py           SQLite schema and helpers
  importer.py     CSV / Excel import
  validation.py   email and contact checks
  topics.py       niche detection and topic suggestions
  templates.py    email wording (rule-based)
  drafts.py       draft generation, editing, approval, follow-ups
  sender.py       batch preparation, confirmation, sending worker
  providers.py    Dry run, Gmail API and SMTP providers
  contacts.py     contacts, statuses and suppression
  gmail_auth.py   Gmail OAuth connection
  init_db.py      database setup command
  discovery.py    website discovery pipeline, daily run-once logic, CLI
  search_providers.py  Brave / Google CSE / SearXNG search
  crawler.py      polite fetching (robots.txt, limits, no bot-check bypass)
  qualify.py      niche, guidelines, email and spam detection
  seo_metrics.py  Moz DA and Ahrefs DR
  email_verify.py Hunter / ZeroBounce / syntax-only email checks
  websites.py     discovered websites, filters, link to contacts
  csv_sync.py     niche_websites_outreach.csv export and merge
  domains.py      URL and domain normalization
  free_discovery.py  free search-query generator and URL import queue
  claude_agent.py Claude auto-discovery (runs `claude -p`)
  replies.py      reply, bounce and follow-up-due alerts
  auth.py         login page for the hosted app
static/           dashboard (index.html, app.css, app.js)
scripts/          optional Windows Task Scheduler setup
output/           niche_websites_outreach.csv (git-ignored)
.vscode/tasks.json  run discovery on folder open, start the app, run tests
tests/            automated tests
sample_data/      example contact file (fictional example.com addresses)
data/             your database (created automatically, git-ignored)
credentials/      Gmail OAuth files (you create it, git-ignored)
Dockerfile, render.yaml, DEPLOY.md   hosting on Render
Start/Stop Outreach App.bat          start or stop the app with a double-click
```

## 10. Troubleshooting

| Problem | Fix |
|---|---|
| `python` not found | Use the full path shown in setup step 1, or reinstall Python with "Add to PATH" ticked. |
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `ModuleNotFoundError` | Activate the venv (`.\.venv\Scripts\Activate.ps1`) and run `pip install -r requirements.txt`. |
| Port 8000 in use | `uvicorn app.main:app --port 8001` |
| "Connect Gmail" button disabled | `credentials/client_secret.json` is missing. See Gmail setup step 5. |
| `access_denied` / "app not verified" | Add your address as a **Test user** on the OAuth consent screen. |
| Queue stopped: "authorization expired" | Settings → **Reconnect Gmail**, then prepare a new batch. Unsent emails stay approved. |
| Queue stopped: "limit reached" | You hit the provider's or your daily limit. Wait 24 hours. Don't try to bypass it. |
| Email shows **uncertain** | Check your Gmail **Sent** folder, then resolve it in **Sent history**. |
| Draft won't go into a batch | The confirmation dialog lists the reason (not approved, edited, unverified, suppressed, already contacted…). |
| Import says "Could not find an email column" | Add a header such as `Email` to the first row. |
| Discovery finds nothing new | Check **Services and limits** on the Website discovery page. Without a search key, automatic search is off: use the free workflow (generate searches, import URLs) or **Find new websites with Claude**. Results for a query are reused for 14 days. |
| "Discovery already completed today" | That's the once-a-day rule. Click **Run Website Discovery Now** to run again. |
| A site is *Needs Review*: "refused automated access" / robots.txt | The site doesn't allow automated visits. Check it by hand in your browser. |
| DA or DR blank | The key isn't set, the quota is used up, or the provider returned no value. See *DA Source* / *DR Source* and the run's warnings. |
| CSV saved with a timestamp in its name | `niche_websites_outreach.csv` was open in Excel. Close it and click **Update CSV file**. |
| Scheduled task didn't run | Task Scheduler → *Outreach Website Discovery* → History. It runs only while you're logged in. See `data/discovery.log`. |
| Start over with an empty database | Stop the app and delete `data/outreach.db`, then run `python -m app.init_db`. |
