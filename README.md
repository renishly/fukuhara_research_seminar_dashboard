# Fukuhara Research Seminar Dashboard

A website for the Fukuhara Research Seminar. Admins and students sign in, browse the seminar's classes on a side-scrolling timeline, post and submit assignments and exams, and connect their AI agents to a per-student agent dashboard. Visitors without an account can look around as **guests**: read-only, with limited detail, and never seeing students' real names.

It's written in Python with [FastAPI](https://fastapi.tiangolo.com/). The pages are plain HTML built on the server (Jinja2 templates); small interactions use HTML attributes ([HTMX](https://htmx.org/) and [Alpine.js](https://alpinejs.dev/)), so you can understand and change almost everything without knowing JavaScript.

---

## How the pieces connect

```text
  Admins, students, guests                     Students' AI agents
  (a web browser)                              (any program, anywhere)
         │                                              │
         │  pages: /login, /classes, ...                │  the agent API: /api/...
         │  (sign-in cookie)                            │  (X-API-Key header)
         ▼                                              ▼
  ┌───────────────────────────── main.py (FastAPI) ─────────────────────────┐
  │ routes/auth.py  routes/classes.py  routes/coursework.py                 │
  │ routes/agents.py  routes/members.py           routes/agent_api.py       │
  │        └──── permissions.py: "can this viewer see / do this?" ────┘     │
  └──────────────────────┬───────────────────────────────┬──────────────────┘
                         ▼                               ▼
              Database (db.py)                  File storage (storage.py)
              SQLite locally,                   ./local_files/ locally,
              Supabase Postgres online          a private Supabase bucket online
                         │
                         ▼
     each student's agent dashboard (grid, outputs, monitor, activity, Inbox)
```

- **Members and guests** use the pages. Every page, fragment, download and form asks `permissions.py` before showing or changing anything.
- **Agents** never use the pages. They talk to the agent API with their own API key, through the connector (`connector/dashboard_connector.py`). What they send (activity, outputs, monitor) appears on their owner's agent dashboard for that class.
- **The database** holds accounts, classes, assignments, submissions and agent data. **Files** (assignment files, submissions, agent outputs, Inbox files) are kept in file storage, never in the database, because Supabase's free database is small.

### The files

| File | What it does |
|---|---|
| `main.py` | Sets the app up: creates tables at startup, serves `static/`, includes the routes, friendly error replies |
| `db.py` | The database: every table, the connection, `add_missing_columns()`, Gen 1–12 |
| `security.py` | Passwords, sign-ins, CSRF protection, roles, agent API keys, login rate limits |
| `permissions.py` | One place answering "can this viewer see / do X in this class?" |
| `names.py` | Hides students' names from guests (placeholders and the name search) |
| `storage.py` | Saves, streams and deletes files: Supabase Storage or a local folder |
| `uploads.py` | Upload limits, file records, and sending files safely |
| `usage.py` | What takes up file storage and database space, with the Members page's pie charts |
| `timeline.py` | Pure Python: lays class boxes out in lanes on the timeline |
| `web.py` | Templates, times in JST, small reply helpers |
| `routes/` | The pages and the API (see the diagram) |
| `templates/` | The HTML pages; `templates/partials/` are pieces HTMX refreshes or opens |
| `static/` | `tailwind.css` (built), `theme.css` (the "bluepaper" look), `ui.js` (drag-to-pan, centring, the grid) |
| `connector/` | The downloadable Python connector, and a prompt for building one in any language |
| `agent/agent.py` | A demo agent built on the connector |
| `tests/` | Automated tests (`pytest`), and `TEST_REPORT.md` |

---

## Who can do what

| | Admin | Student | Guest |
|---|---|---|---|
| Sign in | Login ID + password | Login ID + password | "Continue as guest", no details |
| See classes | All | All, except ones hidden from them (enrolled classes are always visible) | All not marked "Hidden from guests" |
| Create, edit, delete classes, assignments, exams | ✓ | | |
| Submit in a class | | When enrolled; only their own submissions | |
| Delete any submission (moderation) | ✓ | Own only | |
| See submissions and their files | ✓ | ✓ | Only ones ticked "Visible to guests" |
| Add and control agents | | When enrolled; only their own | |
| See other students' agent pages | ✓ (read-only) | ✓ (read-only) | Only the grid: names, status, descriptions |
| Members page (accounts, gens, storage) | ✓ | | |
| Real names of students | ✓ | ✓ | Never: "Gen 9 Student 7" instead |

**Guests and names.** Each student has a fixed placeholder such as "Gen 9 Student 7". Before any text reaches a guest, the site searches it for students' names (full names, name parts, and the "other names" students list on their Account page) and replaces them. It ignores letter case and full-width/half-width differences. The stored text never changes; members always see the original. **The search can't read inside files**: files ticked "Visible to guests" are shown exactly as uploaded.

---

## Running it on your computer

You need **Python 3.10 or newer**. Open a terminal in the `app/` folder.

### 1. Virtual environment and requirements

macOS / Linux:

```bash
python3 -m venv .venv
```

```bash
source .venv/bin/activate
```

```bash
pip install -r requirements.txt
```

Windows (PowerShell):

```bash
python -m venv .venv
```

```bash
.venv\Scripts\Activate.ps1
```

```bash
pip install -r requirements.txt
```

### 2. Your `.env` file (secrets)

Copy the example file, then fill in your own values. `.env` is never committed (it's in `.gitignore`).

```bash
cp .env.example .env
```

(Windows: `copy .env.example .env`.)

Make random values for the two registration keys (run it twice, one for each; **they must be different**):

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

`KEY_ENCRYPTION_SECRET` encrypts agents' API keys and needs Fernet's own format. Make it with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

- Keep the **admin** key private: anyone with it can create an admin account.
- Change the **student** key each year and give it to the new students; existing accounts keep working, because keys are only checked when registering.
- If a key is empty, registration for that role is switched off.
- Don't lose or change `KEY_ENCRYPTION_SECRET`: keys saved with an old one can't be viewed any more (each agent's "Replace key" fixes it).

Leave `DATABASE_URL`, `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` empty locally: the site then uses `./local.db` (SQLite) and `./local_files/`.

### 3. Run the site

```bash
fastapi dev main.py
```

Then open:

- <http://127.0.0.1:8000/login>: sign in, register, or continue as a guest
- <http://127.0.0.1:8000/classes>: the classes timeline (the first page after signing in)
- <http://127.0.0.1:8000/docs>: the agent API, explorable in the browser

`fastapi dev` restarts the site whenever you save a Python file. Stop it with Ctrl+C.

### 4. Create the first admin account

Go to `/register`, choose **Admin**, and enter a Login ID, your full name, a password (twice) and the admin registration key. Students register the same way with **Student**, their gen and the student key.

Gen 1 to Gen 12 exist from the start. Add Gen 13 and later on the **Members** page.

### Changing the styles (Tailwind)

`static/tailwind.css` is built ahead of time from `tailwind.input.css` and the class names used in the templates, and it is committed, so the running site never needs Node.js. **After using a class name that isn't used anywhere yet** (for example `mt-7` in a template), rebuild it. You need [Node.js](https://nodejs.org/) on your computer for this, once:

```bash
npm install
```

```bash
npm run build:css
```

`npm run watch:css` rebuilds automatically while you edit. The colours of the daisyUI theme "bluepaper" are in `tailwind.input.css`; borders, shadows, the background grid and animations are plain CSS in `static/theme.css` (no rebuild needed).

---

## Connecting an agent

1. Sign in as a student who is **enrolled** in a class. Open the class, the **Agents** tab, and your own cell.
2. Use the **Add Agent** card. Leave the key blank to generate one, and copy it.
3. "Implement connector" (bottom left of the sidebar) downloads the Python connector, or a prompt that tells an AI coding assistant how to build one in any language.

### Running the demo agent

With the site running, and the key from step 2:

macOS / Linux:

```bash
DASHBOARD_API_KEY=paste-the-key-here python agent/agent.py
```

Windows (PowerShell):

```bash
$env:DASHBOARD_API_KEY="paste-the-key-here"; python agent\agent.py
```

(Or put `DASHBOARD_API_KEY=...` in your `.env` file.) Its card turns **online**, it reports "Agent online", sends a "ticks" output every 20 seconds, and replies to **Start**, **Pause** and messages. Its `handle()` function is where a real agent's AI logic goes. Set `DASHBOARD_URL` to talk to a deployed site instead of `http://127.0.0.1:8000`.

---

## Running the tests

```bash
pytest -v
```

The tests use an in-memory database, a temporary folder for files and test secrets, so they never touch `local.db`, `local_files/` or `.env`. `tests/TEST_REPORT.md` lists what each test checks.

---

## Putting it online: Supabase + Render

The site runs on Render's free web service; the database and file storage are a free Supabase project. Create both in **Singapore**, the closest region to Japan, so they sit next to each other.

### 1. Supabase: database and private file bucket

1. Sign up at <https://supabase.com> and create a **new project**. Region: **Southeast Asia (Singapore)**. Choose a strong database password and keep it (for example in a password manager).
2. **Database connection string.** Click **Connect** at the top of the project. Under **Session pooler**, copy the connection string (it looks like `postgresql://postgres.abcdefghijkl:[YOUR-PASSWORD]@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres`). Put your database password in place of `[YOUR-PASSWORD]` and add `?sslmode=require` to the end. That's your `DATABASE_URL`.
   *Why the Session pooler?* Render's free service can only reach IPv4 addresses, and Supabase's direct connection is IPv6. The site converts `postgresql://` to the driver it needs by itself.
3. **File bucket.** Go to **Storage** → **New bucket**. Name it `seminar-files` and leave **Public bucket off** (private). Files are only ever read by the site, which checks permissions first.
4. **Project URL and service key.** Go to **Project Settings** → **API Keys** (or **API**). Copy the project URL (`https://abcdefghijkl.supabase.co`), and the **secret** key (called `service_role`, or a `sb_secret_...` key in newer projects). These are `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`.
   **The service key can read and change everything. Never put it in a page, a browser, a screenshot, or the repository**: only in Render's Environment settings and your own `.env`.

The site creates its tables (and Gen 1–12) by itself the first time it starts.

### 2. Push the project to GitHub

The project's git repository is the `app/` folder. The repository on GitHub is `renishly/fukuhara_research_seminar_dashboard` (private; it must be empty before the first push). Then, in `app/`:

```bash
git add -A
```

```bash
git commit -m "First version of the seminar dashboard"
```

```bash
git remote add origin https://github.com/renishly/fukuhara_research_seminar_dashboard.git
```

```bash
git push -u origin main
```

Check first that `git status` doesn't list `.env`, `local.db` or `local_files/` (they're ignored).

### 3. Render: deploy with the Blueprint

1. Sign up at <https://render.com> and connect your GitHub account.
2. **New** → **Blueprint**, and choose the `fukuhara_research_seminar_dashboard` repository. Render reads `render.yaml`: one free web service called `fukuhara-seminar` in Singapore.
3. Render asks for the secret values (they're marked `sync: false`, so they're never stored in GitHub): `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `ADMIN_REGISTRATION_KEY`, `STUDENT_REGISTRATION_KEY` and `KEY_ENCRYPTION_SECRET`. Use new random values for the keys (see above), not your local ones. `SUPABASE_BUCKET` is already set to `seminar-files`.
4. Click **Apply**. The first build takes a few minutes. Your site is then at `https://fukuhara-seminar.onrender.com` (or similar: see the service's page).
5. Register the first admin there, as you did locally.

From now on, **every push to `main` redeploys the site**. You can change secrets later under the service's **Environment** tab (it restarts the site).

### 4. A custom domain (optional)

On the service's page in Render: **Settings** → **Custom Domains** → **Add Custom Domain**, and type your domain (e.g. `seminar.example.com`). Render shows the DNS record to add (a `CNAME` pointing at your `onrender.com` address, or an `A` record for a bare domain). Add it at the company your domain is registered with, wait for Render to verify it, and Render sets up HTTPS by itself. Agents should then use the new address as `DASHBOARD_URL`.

---

## Warnings: free plans and guests

- **Render's free service sleeps** after about 15 minutes without visits. The next visit takes **about a minute** to wake it up. Agents' connectors wait and keep their outputs in an offline buffer meanwhile.
- **Supabase's free plan** has a **500 MB database** and **1 GB of file storage**, with uploads of up to 50 MB. The **Members** page shows how much of each is used, and **Show breakdown** under each bar shows what is taking up the space, as a pie chart and a table: files by type, class, person and agent, plus the largest files; the database by table (exact, on Supabase) and by class, person and agent (estimated from the text stored).
  - **Files:** the site refuses new uploads above 900 MB, with a message asking people to tell an admin. Nothing else is affected.
  - **Database:** files never go in it, so it holds only text and grows slowly. To keep one busy agent from filling it, each agent keeps only its newest **1,000 outputs** and **2,000 Activity lines** (older ones, and their files, are deleted automatically; change `MAX_OUTPUTS_PER_AGENT` and `MAX_EVENTS_PER_AGENT` in `routes/agent_tools.py`). If the database does reach 500 MB, Supabase makes it read-only: pages still load, but nothing can be saved, not even sign-ins. People then see a message saying the database is full instead of an error, and agents' connectors keep their outputs in their offline buffer. Free space by deleting old classes or agents, or move to a paid Supabase plan.
- **Free Supabase projects pause** after about **a week without activity** (for example over a semester break). The site then can't reach its database. Restore the project from the Supabase dashboard (it takes a few minutes).
- **The free plans have no automatic backups.** Export what matters yourself now and then.
- These limits change: check **Render's and Supabase's current terms**.
- **Anything guests can see is visible to the whole internet**: class information, assignments and submissions ticked "Visible to guests", and agent names and descriptions. Students' names are hidden from guests in text, but **files are shown to guests exactly as uploaded**, so names inside a file are not hidden. Untick "Visible to guests", or mark a class "Hidden from guests", when in doubt.

---

## Changing the database later

At every start, the site creates missing tables and **adds** columns that exist in `db.py` but not yet in the database (each with its default value), so adding a new field to a table just works. **Renaming, removing or changing** a column needs a migration tool such as [Alembic](https://alembic.sqlalchemy.org/).

---

## Suggested next steps

- **Instant updates** with Server-Sent Events, instead of pages checking every few seconds.
- **Alembic migrations**, for renaming or removing columns safely.
- **Switch off `/docs` in production** (`FastAPI(docs_url=None, redoc_url=None)` in `main.py`), or only show it to admins.
- **Discord notifications** for the notify bells on output categories (the settings are already saved in the `NotifySetting` table).
- **Grading and feedback** on submissions.
