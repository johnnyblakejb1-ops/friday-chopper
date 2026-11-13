# FRIDAY v2.0 — GitHub Edition (the chopper that never sleeps)

Friday is the on-demand chop worker for BestBooks. When a student opens a
paper in the Vault that has not been chopped yet, the site drops the request
into `chop_queue.json`. Friday wakes up, downloads the PDF, chops pages 2-10,
OCR's them, sends the text to Groq, and ships the extracted questions — now
with **brief explanations for every MCQ** and **short model answers for
theory** — back to `friday_bridge.php`. The Vault serves them instantly.

**The old problem:** she lived on Hugging Face and went to sleep when idle,
so chops sat in the queue until the Space woke up (or never).
**The fix:** GitHub Actions wakes her every 5 minutes, forever. No PC needed.
No sleep button. Free on a public repository.

---

## Setup — 6 steps, about 5 minutes

1. **Create the repo** — on GitHub, click **New repository**, name it
   something like `friday-chop`, set it **Public** (public repos get free,
   unlimited Actions minutes; there are no secrets inside the code).

2. **Upload these files** — via *Add file → Upload files*, or with git:

   ```
   friday_worker.py
   requirements.txt
   .github/workflows/friday.yml
   .gitignore
   ```

   The `.github` folder must be at the TOP level of the repo — GitHub only
   reads workflows from there. If you upload a zip, unzip first and make
   sure `.github/workflows/friday.yml` is at the root, not nested.

3. **Add the 3 secrets** — in the repo: *Settings → Secrets and variables →
   Actions → New repository secret*:

   | Secret         | Value                                        |
   |----------------|----------------------------------------------|
   | `GROQ_API_KEY` | the same Groq key she used on Hugging Face    |
   | `SITE_URL`     | `https://cambook.lovestoblog.com`             |
   | `SITE_SECRET`  | the site secret from `config.json`            |

   Optional: `GROQ_MODEL` (defaults to `openai/gpt-oss-120b`).

4. **Enable Actions** — *Settings → Actions → General* → allow all actions.
   The schedule starts on its own once the workflow file is on the default
   branch (usually `main`).

5. **First test** — open the *Actions* tab → pick **friday-chop** →
   **Run workflow** → run it. Then open the run's log: you should see
   `⚔️ Friday GitHub Edition v2.0 — --once (CI wake-up)` followed by
   `[ONCE] pass complete`.

6. **Full test** — go to the site, open a paper in the Vault that has no
   questions yet (the processing screen appears), and watch the Actions log:
   `📥 1 request(s) waiting` → `downloaded from public Drive` →
   `chopping pages 2-10` → `[SHIP] …` → `[QUEUE] ✅`. About 2-3 minutes
   later the student's screen flips to Ready.

---

## How she behaves on GitHub

- **Every 5 minutes** a run starts: poll the queue → chop up to 2 waiting
  papers → exit. Anything left over waits for the next wake-up 5 minutes
  later — she is never asleep, just pacing.
- A single chop takes roughly 2-4 minutes (8 pages × OCR + Groq). The run
  timeout is 14 minutes and `concurrency` stops runs from overlapping, so a
  chop is never abandoned mid-air.
- If a paper's Drive file is not shared publicly, the log now screams
  `ACCESS DENIED` with the fix (Share → Anyone with the link → Viewer).
- Nothing is stored on GitHub: every run downloads fresh, chops fresh, ships
  to the site, deletes everything. There is no state to corrupt.

## Keeping the heartbeat alive (the one GitHub quirk)

GitHub **disables scheduled workflows after 60 days with no commits** to the
repo (it emails you first, twice). Two easy counters:

- Just push any tiny commit (edit the README) — the 60-day clock resets; or
- If you see "This scheduled workflow is disabled because there hasn't been
  activity", click **Enable** once — or use the **Run workflow** button.

That is the only maintenance she will ever ask for.

## The explanation upgrade (v2.0)

- Multiple choice: Groq now returns one short, simple sentence per question
  saying **why the correct option is correct**. The Vault shows it in a
  "Why the correct answer is B" box whenever a student answers wrong, and in
  the end-of-paper review.
- Theory: Groq writes a **2-4 sentence model answer** in clear English,
  shown under the student's attempt next to the keyword checklist.
- Old papers already in the bank keep working — boxes simply do not appear
  until a paper is re-chopped or the explanation is typed in the Chopper.

## Still Hugging Face? Keep her as a spare

The same file still runs the old loop+Flask mode with no arguments
(`python friday_worker.py`), so the HF Space (if you ever un-pause it) keeps
working untouched. Two workers are safe — the site only hands each queued
paper to whoever polls first.
