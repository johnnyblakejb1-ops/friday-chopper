# ═══════════════════════════════════════════════════════════════
#  FRIDAY — GITHUB EDITION ⚙️  v2.0  (never sleeps · CI worker)
#  Runs on GitHub Actions every 5 minutes — a PC is never needed.
#  Student requests paper → pages 2-10 chopped → Groq extracts
#  MCQs + brief explanations → shipped to friday_bridge.php.
#
#  v2.0 changes (GitHub Edition):
#   • --once mode: one chop pass for CI, then exit (Actions cron
#     wakes her every 5 minutes — no sleeping, no billing)
#   • MAX_JOBS_PER_RUN guard so one pass always finishes in time
#   • Brief, clear EXPLANATIONS for every MCQ answer ("exp") and
#     short model answers for theory questions ("model") — the
#     Vault now teaches the why, not just the letter
#   • Everything from v1.3 kept: OAuth-free Drive routes, cookie
#     shield solver, ACCESS DENIED detector, PDF sanity check,
#     route memory, model self-heal
#   • Still runs classic loop+Flask mode with no args (HF/VM OK)
# ═══════════════════════════════════════════════════════════════
import os, re, time, shutil, requests, json, base64, subprocess, threading
import argparse
from pdf2image import convert_from_path
from flask import Flask, jsonify

# ─── InfinityFree cookie shield solver ───
try:
    from Crypto.Cipher import AES
    HAS_AES = True
except ImportError:
    HAS_AES = False

# ==========================================
# CONFIGURATION — everything comes from Space secrets
# Only 3 secrets needed: GROQ_API_KEY, SITE_URL, SITE_SECRET
# (GROQ_MODEL is optional, defaults below)
# ==========================================
BASE_DIR = "/tmp/friday_system"
os.makedirs(BASE_DIR, exist_ok=True)

TEMP_PREFIX = "temp_page_"
CHOP_PAGES = (2, 10)  # the CEO's law: always chop pages 2-10
MIN_PDF_BYTES = 8192  # anything smaller is an error page, not a paper

BROWSER_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

MAX_JOBS_PER_RUN = int(os.environ.get("MAX_JOBS_PER_RUN", "2"))  # CI passes stay inside the job timeout

CONFIG = {
    "groq_api_key": os.environ.get("GROQ_API_KEY", ""),
    "model":        os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
    "site_url":     os.environ.get("SITE_URL", "").rstrip("/"),
    "site_secret":  os.environ.get("SITE_SECRET", ""),
}

STATS = {"started": time.time(), "polls": 0, "chops_done": 0, "errors": 0,
         "last_chop": "—", "last_route": "—"}

def load_config():
    return CONFIG

def save_config(update):
    """No persistent disk on Spaces — model self-heal updates live in memory only."""
    CONFIG.update(update or {})

def log(message):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)

def cleanup_temp_images():
    try:
        for f in os.listdir(BASE_DIR):
            if f.startswith(TEMP_PREFIX) and f.endswith(".png"):
                os.remove(os.path.join(BASE_DIR, f))
    except: pass

# ─── Tesseract is installed via apt in the Docker image ───
def find_tesseract_path():
    return shutil.which("tesseract")

# ==========================================
# DRIVE ID HELPERS — extract an ID from anything
# ==========================================
def drive_id_from_anything(s):
    """Pulls a Drive file ID out of a raw ID, a full URL, or any link format."""
    if not s: return None
    s = str(s).strip()
    if re.fullmatch(r'[a-zA-Z0-9_-]{20,}', s): return s
    m = re.search(r'[?&]id=([a-zA-Z0-9_-]{20,})', s)
    if m: return m.group(1)
    m = re.search(r'/file/d/([a-zA-Z0-9_-]{20,})', s)
    if m: return m.group(1)
    return None

def looks_like_pdf(content):
    """A REAL pdf starts with %PDF- and isn't a 2-line error page."""
    return (isinstance(content, (bytes, bytearray))
            and len(content) > MIN_PDF_BYTES
            and content[:4] == b"%PDF")

def is_access_denied(html):
    low = (html or "")[:6000].lower()
    return ("you need access" in low
            or "request access" in low
            or ("accounts.google.com" in low and "sign in" in low))

# ==========================================
# GROQ MODEL DISCOVERY — self-healing
# ==========================================
def discover_model(api_key):
    try:
        r = requests.get("https://api.groq.com/openai/v1/models",
                         headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
        if r.status_code != 200: return None
        ids = [m.get("id", "") for m in r.json().get("data", [])]
        ids = [i for i in ids if i and not any(b in i for b in ("vision","guard","whisper","tts","embed","orpheus","safeguard"))]
        for pref in ("gpt-oss-120b","gpt-oss","instant","8b","llama","qwen"):
            for i in ids:
                if pref in i: return i
        return ids[0] if ids else None
    except Exception:
        return None

# ==========================================
# THE DOCK SESSION — cookie shield
# ==========================================
DOCK = None

def dock_session(force=False):
    global DOCK
    if DOCK is not None and not force: return DOCK
    config = load_config()
    site = config.get("site_url")
    if not site: raise RuntimeError("SITE_URL secret not set.")
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    r = s.get(f"{site}/friday_bridge.php?command=ping", timeout=30)
    if "__test" in r.text:
        if not HAS_AES: raise RuntimeError("pycryptodome missing in image.")
        vals = re.findall(r'toNumbers\("([0-9a-f]{32})"\)', r.text)
        if len(vals) < 3: raise RuntimeError("Could not read the site's shield values.")
        key = bytes.fromhex(vals[0]); iv = bytes.fromhex(vals[1]); ct = bytes.fromhex(vals[2])
        cookie = AES.new(key, AES.MODE_CBC, iv).decrypt(ct).hex()
        host = site.split("//")[-1].split("/")[0]
        s.cookies.set("__test", cookie, domain=host, path="/")
        log("[OK] Passed the site's cookie shield")
    DOCK = s
    return DOCK

# ==========================================
# 🌐 PUBLIC DRIVE DOWNLOAD — no OAuth, ever
# ==========================================
def gdrive_public_download(file_id, dest_path):
    """Download a PUBLIC Google Drive file with zero authentication.
       2a: direct usercontent URL (skips confirm dance)
       2b: classic uc?export=download flow with form scraping
       Loudly reports ACCESS DENIED when the file is not shared."""
    s = requests.Session()
    s.headers.update({"User-Agent": BROWSER_HEADERS["User-Agent"]})

    # ── Route 2a — direct usercontent (fastest, handles big files too) ──
    try:
        r = s.get("https://drive.usercontent.google.com/download",
                  params={"id": file_id, "export": "download", "confirm": "t"},
                  timeout=180)
        if r.status_code == 200 and looks_like_pdf(r.content):
            with open(dest_path, "wb") as f: f.write(r.content)
            return True
        if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
            if is_access_denied(r.text):
                log("   [QUEUE] 🚫 Drive says ACCESS DENIED — file is NOT shared publicly.")
                log("   [QUEUE] → Fix: Drive → right-click file → Share → 'Anyone with the link' → Viewer")
                return False
            # real virus-scan interstitial → scrape the form and follow it
            params = dict(re.findall(r'name="([^"]+)"\s+value="([^"]*)"', r.text))
            if params.get("confirm") or params.get("uuid"):
                r2 = s.get("https://drive.usercontent.google.com/download",
                           params=params, timeout=180)
                if r2.status_code == 200 and looks_like_pdf(r2.content):
                    with open(dest_path, "wb") as f: f.write(r2.content)
                    return True
    except Exception as e:
        log(f"   [QUEUE] Route 2a error: {e}")

    # ── Route 2b — classic uc flow ──
    try:
        r = s.get("https://drive.google.com/uc",
                  params={"export": "download", "id": file_id},
                  timeout=120)
        ctype = r.headers.get("content-type", "")
        if "text/html" in ctype:
            if is_access_denied(r.text):
                log("   [QUEUE] 🚫 Drive says ACCESS DENIED — file is NOT shared publicly.")
                return False
            params = dict(re.findall(r'name="([^"]+)"\s+value="([^"]*)"', r.text))
            if not params.get("confirm"):
                log("   [QUEUE] Drive confirm page had no token — file may not be public")
                return False
            r = s.get("https://drive.usercontent.google.com/download",
                      params=params, timeout=180)
            ctype = r.headers.get("content-type", "")
        if r.status_code == 200 and "text/html" not in ctype and looks_like_pdf(r.content):
            with open(dest_path, "wb") as f: f.write(r.content)
            return True
    except Exception as e:
        log(f"   [QUEUE] Route 2b error: {e}")
    return False

# ==========================================
# PDF FETCHER — smart multi-route, no OAuth
# ==========================================
LAST_GOOD_ROUTE = {"method": None}  # remembered between jobs for speed

def fetch_pdf(site, file_id, url):
    """Get the paper PDF by any means necessary. Returns local path or None."""
    dest = os.path.join(BASE_DIR, "temp_queue.pdf")

    # normalize: sometimes only a URL is supplied — pull the Drive ID out of it
    if not file_id and url:
        extracted = drive_id_from_anything(url)
        if extracted:
            log(f"   [QUEUE] extracted Drive ID from URL → {extracted[:12]}…")
            file_id = extracted

    # reorder routes if the last job found a winner
    if LAST_GOOD_ROUTE["method"] == "drive":
        order = ["drive", "dlproxy", "url"]
    else:
        order = ["dlproxy", "drive", "url"]

    for method in order:
        # ── Route 1 — via the site's own dl.php proxy (passes the shield) ──
        if method == "dlproxy" and file_id:
            try:
                s = dock_session()
                r = s.get(f"{site}/dl.php?f={file_id}", timeout=180, stream=True)
                if r.status_code == 200:
                    first = next(r.iter_content(8192), b"")
                    if first[:4] == b"%PDF":
                        with open(dest, "wb") as f:
                            f.write(first)
                            for chunk in r.iter_content(65536):
                                f.write(chunk)
                        if os.path.getsize(dest) > MIN_PDF_BYTES:
                            log(f"   [QUEUE] downloaded via site dl.php ({os.path.getsize(dest)} bytes)")
                            LAST_GOOD_ROUTE["method"] = "dlproxy"
                            STATS["last_route"] = "dl.php proxy"
                            return dest
                        log("   [QUEUE] dl.php gave a suspiciously small file — rejected")
                    else:
                        log("   [QUEUE] dl.php did not return a PDF (not a vault file? — that's OK, trying Drive)")
                else:
                    log(f"   [QUEUE] dl.php HTTP {r.status_code}")
            except Exception as e:
                log(f"   [QUEUE] dl.php download failed: {e}")

        # ── Route 2 — public Google Drive (2a direct + 2b classic inside) ──
        if method == "drive" and file_id:
            try:
                if gdrive_public_download(file_id, dest):
                    log(f"   [QUEUE] downloaded from public Drive ({os.path.getsize(dest)} bytes)")
                    LAST_GOOD_ROUTE["method"] = "drive"
                    STATS["last_route"] = "public Drive"
                    return dest
            except Exception as e:
                log(f"   [QUEUE] public Drive download failed: {e}")

        # ── Route 3 — direct URL supplied by the queue request ──
        if method == "url" and url:
            try:
                u = url if url.startswith("http") else f"{site}/{url}"
                # if it's actually a Drive link, reuse the smart downloader
                did = drive_id_from_anything(u)
                if did and "drive" in u:
                    if gdrive_public_download(did, dest):
                        log(f"   [QUEUE] downloaded from Drive URL ({os.path.getsize(dest)} bytes)")
                        LAST_GOOD_ROUTE["method"] = "drive"
                        STATS["last_route"] = "Drive via URL"
                        return dest
                r = requests.get(u, timeout=120, headers={"User-Agent": BROWSER_HEADERS["User-Agent"]})
                if r.status_code == 200 and looks_like_pdf(r.content):
                    with open(dest, "wb") as f: f.write(r.content)
                    log(f"   [QUEUE] downloaded from server URL ({len(r.content)} bytes)")
                    LAST_GOOD_ROUTE["method"] = "url"
                    STATS["last_route"] = "direct URL"
                    return dest
                log(f"   [QUEUE] URL gave HTTP {r.status_code}, not a valid PDF")
            except Exception as e:
                log(f"   [QUEUE] server URL download failed: {e}")

    log("   [QUEUE] ❌ all routes exhausted — if the log shows ACCESS DENIED above, share the Drive file publicly")
    return None

# ==========================================
# FRIDAY (AI EDITOR)
# ==========================================
def friday_format(page_texts):
    config = load_config()
    api_key = config.get("groq_api_key")
    model = config.get("model", "openai/gpt-oss-120b")
    if not api_key:
        log("   [ERROR] No API key!"); return []
    system_prompt = (
        "You are a strict data extractor AND a patient exam coach. Extract exam questions from the provided text of ONE exam page. "
        "Correct OCR spelling errors. "
        "For multiple choice questions output question, option_a to option_d, answer (the correct letter, or empty string if unknown), "
        "and explanation: ONE short sentence in simple English that says why the correct option is correct, so a student who "
        "picked a wrong option understands their mistake (empty string if you are not sure). "
        "For theory/structured questions output only question, 3-8 keywords that a good answer must contain, and model_answer: "
        "a brief 2-4 sentence model answer in simple, clear English a student can learn from, with empty options. "
        "Set has_figure to true ONLY if the question explicitly refers to a diagram, figure, graph or the text below. "
        "Output STRICTLY valid JSON: "
        '{"questions":[{"question":"...","option_a":"...","option_b":"...","option_c":"...","option_d":"...",'
        '"answer":"a","explanation":"...","keywords":["..."],"model_answer":"...","has_figure":false}]}'
    )
    all_questions = []
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    for page_num, text in enumerate(page_texts, 1):
        if len(text.strip()) < 50: continue
        if page_num > 1: time.sleep(12)
        for attempt in range(4):
            try:
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"Extract the questions from this page:\n\n{text[:3500]}"}
                    ],
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"}
                }
                res = requests.post("https://api.groq.com/openai/v1/chat/completions",
                                    headers=headers, json=payload, timeout=90)
                if res.status_code == 200:
                    data = json.loads(res.json()['choices'][0]['message']['content'])
                    if isinstance(data, dict):
                        for key in ["questions", "data", "results", "items"]:
                            if key in data and isinstance(data[key], list):
                                data = data[key]; break
                    if isinstance(data, dict): data = [data]
                    n_added = 0
                    if isinstance(data, list):
                        for q in data:
                            if not isinstance(q, dict) or not q.get("question"): continue
                            opts = [str(q.get(k, "") or "") for k in ("option_a","option_b","option_c","option_d")]
                            answer = str(q.get("answer", "") or "").strip().upper()[:1]
                            if answer not in "ABCD": answer = ""
                            kws = [str(k).strip() for k in (q.get("keywords") or []) if str(k).strip()][:10]
                            exp = str(q.get("explanation", "") or "").strip()
                            mdl = str(q.get("model_answer", "") or "").strip()
                            all_questions.append({
                                "q": str(q["question"]).strip()[:1500],
                                "a": opts[0][:180], "b": opts[1][:180],
                                "c": opts[2][:180], "d": opts[3][:180],
                                "ans": answer,
                                "kw": kws if not any(opts) else [],
                                "exp": exp[:500] if any(opts) else "",
                                "model": mdl[:900] if not any(opts) else "",
                                "has_figure": bool(q.get("has_figure")),
                                "page": page_num
                            })
                            n_added += 1
                    log(f"   [OK] Page {page_num}: {n_added} questions")
                    break
                elif res.status_code == 429:
                    wait_s = 70
                    m = re.search(r'"retry-after"\s*:\s*"?(\d+)', res.text)
                    if not m: m = re.search(r'retry again in ([\d.]+)s', res.text)
                    if m:
                        try: wait_s = max(20, int(float(m.group(1))) + 5)
                        except ValueError: pass
                    log(f"   [WAIT] Rate limit page {page_num} — breathing {wait_s}s... (try {attempt+1}/4)")
                    time.sleep(wait_s)
                    if attempt < 3: continue
                    log(f"   [ERROR] Page {page_num} throttled too many times — page skipped.")
                elif res.status_code == 404 and ("does not exist" in res.text or "model_not_found" in res.text):
                    new_model = discover_model(api_key)
                    if new_model and new_model != model:
                        log(f"   [FIX] Model '{model}' was retired — switching to '{new_model}'")
                        model = new_model; save_config({"model": new_model})
                        continue
                    else:
                        log("   [ERROR] No working model found."); break
                elif res.status_code == 400 and attempt < 3:
                    log(f"   [WARN] Page {page_num} retry... {res.text[:150]}"); time.sleep(2)
                else:
                    log(f"   [ERROR] API {res.status_code} page {page_num}: {res.text[:150]}"); break
            except json.JSONDecodeError:
                if attempt < 3:
                    log(f"   [WARN] Decode error page {page_num}, retrying..."); time.sleep(3)
                else: log(f"   [ERROR] Decode failed page {page_num}")
            except Exception as e:
                log(f"   [ERROR] Network page {page_num}: {e}")
                if attempt < 3: time.sleep(5)
    return all_questions

# ==========================================
# JARVIS (SHIPPING QUESTIONS)
# ==========================================
def jarvis_ship(questions, page_images, subject, category, year, paper):
    config = load_config()
    site, secret = config.get("site_url"), config.get("site_secret")
    if not site or not secret:
        log("   [ERROR] site_url / site_secret missing"); return False, "no site config"
    needed_pages = sorted({q["page"] for q in questions if q.get("has_figure")})
    images = {}
    for pn in needed_pages:
        idx = pn - 1
        if 0 <= idx < len(page_images) and os.path.exists(page_images[idx]):
            try:
                with open(page_images[idx], "rb") as f:
                    images[f"page{pn}"] = "data:image/png;base64," + base64.b64encode(f.read()).decode()
            except Exception as e:
                log(f"   [WARN] Could not read page {pn} image: {e}")
    clean = []
    for q in questions:
        clean.append({
            "q": q["q"], "a": q["a"], "b": q["b"], "c": q["c"], "d": q["d"],
            "ans": q["ans"], "kw": q["kw"],
            "exp": q.get("exp", ""), "model": q.get("model", ""),
            "img": f"@page{q['page']}" if q.get("has_figure") and f"page{q['page']}" in images else ""
        })
    body = {"secret": secret, "subject": subject, "category": category,
            "year": str(year), "paper": paper,
            "questions": clean, "images": images}
    log(f"   [SHIP] {len(clean)} questions, {len(images)} figure(s) → {site}")
    for attempt in range(3):
        try:
            s = dock_session(force=(attempt > 0))
            res = s.post(f"{site}/friday_bridge.php?command=add_paper",
                         json=body, timeout=300)
            if "__test" in res.text:
                log("   [WARN] Shield asked again — refreshing..."); dock_session(force=True); continue
            reply = res.json()
            if res.status_code == 200 and reply.get("ok"):
                return True, (f"{reply.get('count')} questions + {reply.get('figures')} figures "
                              f"written to {reply.get('key')}")
            log(f"   [WARN] Site said: {reply}")
            if attempt < 2: time.sleep(5)
        except Exception as e:
            log(f"   [WARN] Ship error ({attempt+1}/3): {e}")
            if attempt < 2: time.sleep((attempt + 1) * 5)
    return False, "site refused after 3 tries"

# ==========================================
# ═ ON-DEMAND CHOP QUEUE (loud failures) ═
# ==========================================
def process_chop_queue(max_jobs=99):
    global DOCK
    config = load_config()
    site, secret = config.get("site_url"), config.get("site_secret")
    if not site or not secret:
        log("[QUEUE] site_url / site_secret missing")
        return
    try:
        s = dock_session()
        res = s.post(f"{site}/friday_bridge.php?command=check_queue",
                     json={"secret": secret}, timeout=30)
    except Exception as e:
        log(f"[QUEUE] ❌ could not reach site: {e}")
        DOCK = None
        return
    if "__test" in res.text:
        log("[QUEUE] shield challenged — rebuilding session")
        dock_session(force=True)
        return
    if res.status_code != 200:
        log(f"[QUEUE] ❌ HTTP {res.status_code}: {res.text[:180]}")
        return
    try:
        reply = res.json()
    except Exception:
        log(f"[QUEUE] ❌ non-JSON reply: {res.text[:180]}")
        DOCK = None
        return
    if not reply.get("ok"):
        log(f"[QUEUE] ❌ site refused: {reply}")
        return
    pending = reply.get("pending") or []
    if not pending:
        return  # normal idle — nobody waiting
    log(f"[QUEUE] 📥 {len(pending)} request(s) waiting")

    handled = 0
    for req in pending:
        if handled >= max_jobs:
            log(f"[QUEUE] job cap {max_jobs} reached — the rest wait for the next wake-up (never asleep, just pacing)")
            break
        handled += 1
        key = req.get("key", "")
        subject = req.get("subject", "Other")
        category = req.get("category", "O-Level")
        year = req.get("year", "2000")
        try: paper = int(req.get("paper", 1))
        except (ValueError, TypeError): paper = 1
        file_id = req.get("file_id", "") or ""
        url = req.get("url", "") or ""
        log(f"[QUEUE] Student requested: {subject} {category} {year} P{paper}")

        def report(progress, status):
            try:
                dock_session().post(f"{site}/friday_bridge.php?command=update_chop",
                    json={"secret": secret, "key": key, "progress": progress, "status": status},
                    timeout=30)
            except Exception: pass

        report(10, "working")

        # ── get the PDF (smart multi-route, no OAuth) ──
        pdf_path = fetch_pdf(site, file_id, url)

        if not pdf_path:
            log("   [QUEUE] could not obtain PDF — marking error")
            STATS["errors"] += 1
            report(0, "error"); continue

        report(20, "working")

        # ── chop pages 2-10 ──
        tesseract_path = find_tesseract_path()
        if not tesseract_path:
            log("   [QUEUE] Tesseract not found in image!")
            os.remove(pdf_path); report(0, "error"); continue

        log(f"   [QUEUE] chopping pages {CHOP_PAGES[0]}-{CHOP_PAGES[1]}…")
        start_pg, end_pg = CHOP_PAGES
        page_images, page_texts = [], []
        ok_ocr = False
        try:
            pages = convert_from_path(pdf_path, dpi=150, thread_count=2,
                                      first_page=start_pg, last_page=end_pg)
            for page_num, page in enumerate(pages, 1):
                temp_img = os.path.join(BASE_DIR, f"{TEMP_PREFIX}{page_num}.png")
                page.save(temp_img, "PNG")
                result = subprocess.run(
                    [tesseract_path, temp_img, "stdout", "-l", "eng+fra"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    encoding="utf-8", errors="replace")
                page_texts.append(result.stdout or "")
                page_images.append(temp_img)
                log(f"   [QUEUE] page {page_num}/{len(pages)} done")
            ok_ocr = True
        except Exception as e:
            log(f"   [QUEUE] OCR error: {e}")
        if not ok_ocr or not page_texts:
            for t in page_images:
                if os.path.exists(t): os.remove(t)
            if os.path.exists(pdf_path): os.remove(pdf_path)
            report(0, "error"); continue

        report(50, "working")

        # ── AI extraction ──
        questions = friday_format(page_texts)
        report(80, "working")

        success, msg = False, "no questions found"
        if questions:
            success, msg = jarvis_ship(questions, page_images, subject, category, year, paper)

        # cleanup — only AFTER shipping (images needed for figures)
        for t in page_images:
            if os.path.exists(t): os.remove(t)
        if os.path.exists(pdf_path): os.remove(pdf_path)

        if success:
            log(f"   [QUEUE] ✅ {msg}")
            STATS["chops_done"] += 1
            STATS["last_chop"] = f"{subject} {category} {year} P{paper} — {msg}"
            report(100, "done")
        else:
            log(f"   [QUEUE] failed: {msg}")
            STATS["errors"] += 1
            report(80, "error")

# ==========================================
# WORKER LOOP (never crashes) + CI ONE-SHOT
# ==========================================
def worker_loop():
    log("⚔️ Friday GitHub Edition v2.0 online — loop mode (serve)")
    missing = [k for k in ("groq_api_key","site_url","site_secret") if not CONFIG.get(k)]
    if missing:
        log(f"[FATAL] Missing secrets: {', '.join(missing)} — set them in Space settings.")
        return
    try: dock_session()
    except Exception as e: log(f"[WARN] Dock not open yet ({e}) — will retry when shipping.")
    while True:
        try:
            STATS["polls"] += 1
            process_chop_queue()
        except Exception as e:
            STATS["errors"] += 1
            log(f"[ERROR] worker pass failed: {e}")
        cleanup_temp_images()
        time.sleep(45)

def run_once(max_jobs=None):
    """GitHub Actions mode: wake up, chop what is waiting, exit."""
    log("⚔️ Friday GitHub Edition v2.0 — --once (CI wake-up)")
    if max_jobs is None: max_jobs = MAX_JOBS_PER_RUN
    missing = [k for k in ("groq_api_key","site_url","site_secret") if not CONFIG.get(k)]
    if missing:
        log(f"[FATAL] Missing secrets: {', '.join(missing)} — add them in Repo → Settings → Secrets and variables → Actions.")
        return 1
    try: dock_session()
    except Exception as e: log(f"[WARN] Dock not open yet ({e}) — will retry inside the pass.")
    try:
        process_chop_queue(max_jobs=max_jobs)
    except Exception as e:
        STATS["errors"] += 1
        log(f"[ERROR] pass failed: {e}")
    cleanup_temp_images()
    log(f"[ONCE] pass complete — chops: {STATS['chops_done']} · errors: {STATS['errors']} · route: {STATS['last_route']}")
    return 0

# ==========================================
# STATUS SERVER (also the keep-alive target)
# ==========================================
app = Flask(__name__)

@app.route("/")
def status():
    return jsonify({
        "worker": "friday-chop", "version": "1.3-smart-fetch", "state": "alive",
        "uptime_min": round((time.time() - STATS["started"]) / 60),
        "polls": STATS["polls"], "chops_done": STATS["chops_done"],
        "errors": STATS["errors"], "last_chop": STATS["last_chop"],
        "last_route": STATS["last_route"],
        "dock": CONFIG.get("site_url") or "not configured",
    })

@app.route("/ping")
def ping():
    try:
        s = dock_session()
        res = s.post(f"{CONFIG['site_url']}/friday_bridge.php?command=ping",
                     json={"secret": CONFIG["site_secret"]}, timeout=30)
        return jsonify({"dock_reply": res.text[:300]})
    except Exception as e:
        return jsonify({"dock_error": str(e)[:300]}), 500

@app.route("/queue")
def queue_check():
    """Shows the raw check_queue reply — the smoking gun for debugging."""
    try:
        s = dock_session()
        res = s.post(f"{CONFIG['site_url']}/friday_bridge.php?command=check_queue",
                     json={"secret": CONFIG["site_secret"]}, timeout=30)
        return jsonify({
            "http": res.status_code,
            "shield_challenge": "__test" in res.text,
            "body": res.text[:600]
        })
    except Exception as e:
        return jsonify({"error": str(e)[:400]}), 500

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Friday — the on-demand chop worker")
    ap.add_argument("--once", action="store_true",
                    help="one chop pass then exit (GitHub Actions mode)")
    ap.add_argument("--jobs", type=int, default=None,
                    help="max papers to chop in one pass (default from MAX_JOBS_PER_RUN)")
    args = ap.parse_args()
    if args.once:
        raise SystemExit(run_once(max_jobs=args.jobs))
    threading.Thread(target=worker_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=7860, threaded=True)