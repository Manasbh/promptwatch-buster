#!/usr/bin/env python3
"""promptwatch-buster: AI answer visibility monitor on the Renderdata API. Stdlib only.

  pwb.py init DIR                                  config template
  pwb.py seed CONFIG --gsc F --bing F --bing-ai F  prompt candidates from real search data (CSV or GSC zip)
  pwb.py plan CONFIG [--icc 0.3]                   credits, hours, precision
  pwb.py run CONFIG --wave W [--yes]               run the panel (resumable)
  pwb.py report CONFIG --wave W                    full report, summary.json, runs.csv (free, re-runnable)
  pwb.py pages CONFIG --wave W --top N [--yes]     fetch top cited pages, see which brands each names
  pwb.py diff CONFIG --a W1 --b W2                 paired change + treated-minus-holdout
  pwb.py trend CONFIG                              brand rates across every wave
  pwb.py dashboard CONFIG                          self-contained HTML dashboard + action checklist
  pwb.py selftest

`run` and `pages` need RENDERDATA_API_KEY.
"""
import argparse, csv, glob, gzip, io, json, math, os, re, sys, tempfile, threading, time
import urllib.error, urllib.request, zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from statistics import mean, stdev, variance
from urllib.parse import urlparse

API = "https://api.renderdata.cloud/v1/scrapers/{}/run"
UA = "promptwatch-buster/2.0"  # Cloudflare 1010-blocks urllib's default UA
ENDPOINT = {"chatgpt": {"fast": "chatgpt", "slow": "chatgpt-slow"},
            "gemini": {"fast": "gemini", "slow": "gemini-slow"},
            "google": {"fast": "google-serp", "slow": "google-serp"}}
SECS = {"chatgpt": 40, "gemini": 15, "google": 90}  # observed fast-mode seconds per call, Nov 2026
CONCURRENCY, MIN_GAP = 2, 2.0  # Renderdata limits: 2 at once, 30 per minute
Z = 1.96
STOP = set("""a an the of for to in on and or with my your our is are be am was were best top vs versus how what which
why who when where can could do does should would will i we you it its that this these from by at as near me than more most
about into get use using vs. 2023 2024 2025 2026 2027 www com html php""".split())

TEMPLATE = {
    "brand": {"name": "YourBrand", "aliases": ["YourBrand.com"], "domain": "yourbrand.com", "case_sensitive": False},
    "competitors": [
        {"name": "CompetitorA", "aliases": [], "domain": "competitora.com", "case_sensitive": False},
        {"name": "CompetitorB", "aliases": [], "domain": "competitorb.com", "case_sensitive": False}],
    "engines": {"chatgpt": 7, "gemini": 7, "google": 3},
    "mode": "fast", "country": "us", "language": "en",
    "sitemap": "https://yourbrand.com/sitemap.xml",
    "prompts": [
        {"id": "best-1", "text": "What is the best <category> for <use case>?", "intent": "best-of", "group": "treated"},
        {"id": "alt-1", "text": "What are good alternatives to CompetitorA?", "intent": "alternatives", "group": "holdout"},
        {"id": "rep-1", "text": "Is YourBrand any good?", "intent": "reputation", "group": "treated"}],
}


# ---------- matching ----------
def brand_rx(b):
    names = [b["name"]] + list(b.get("aliases") or [])
    pat = r"(?<![\w])(?:%s)(?![\w])" % "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(pat, 0 if b.get("case_sensitive") else re.I)


def host(url):
    h = (urlparse(url or "").hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def on_domain(url, domain):
    h, d = host(url), (domain or "").lower().removeprefix("www.")
    return bool(d) and (h == d or h.endswith("." + d))


def toks(s):
    """Content words, plural-folded (crms -> crm) for dedupe and slug matching."""
    return {t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t
            for t in re.findall(r"[a-z0-9]+", s.lower()) if t not in STOP and not t.isdigit()}


# ---------- stats ----------
def wilson(h, n):
    if n == 0:
        return 0.0, 1.0
    p, d = h / n, 1 + Z * Z / n
    c = (p + Z * Z / (2 * n)) / d
    w = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / d
    return max(0.0, c - w), min(1.0, c + w)


def rate_ci(cells):
    """cells: [(hits, runs)] per prompt. The prompt is the unit, so repeat runs are not treated as
    independent. Interval = wider of cluster-robust and pooled Wilson."""
    cells = [(h, n) for h, n in cells if n]
    if not cells:
        return None
    rates = [h / n for h, n in cells]
    m = mean(rates)
    lo, hi = wilson(sum(h for h, _ in cells), sum(n for _, n in cells))
    if len(rates) > 1:
        se = stdev(rates) / math.sqrt(len(rates))
        lo, hi = min(lo, m - Z * se), max(hi, m + Z * se)
    return {"rate": m, "lo": max(0.0, lo), "hi": min(1.0, hi), "prompts": len(rates)}


def paired(diffs):
    if not diffs:
        return None
    m = mean(diffs)
    se = stdev(diffs) / math.sqrt(len(diffs)) if len(diffs) > 1 else float("nan")
    return {"diff": m, "lo": m - Z * se, "hi": m + Z * se, "n": len(diffs)}


def did(dt, dh):
    if len(dt) < 2 or len(dh) < 2:
        return None
    m = mean(dt) - mean(dh)
    se = math.sqrt(variance(dt) / len(dt) + variance(dh) / len(dh))
    return {"diff": m, "lo": m - Z * se, "hi": m + Z * se, "n_treated": len(dt), "n_holdout": len(dh)}


def pct(x):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.0f}%"


def fmt(ci):
    return "n/a" if not ci else f"{pct(ci['rate'])} ({pct(ci['lo'])}–{pct(ci['hi'])})"


# ---------- io ----------
def load_cfg(path):
    cfg = json.load(open(path))
    cfg["_dir"] = os.path.dirname(os.path.abspath(path))
    ids = [p["id"] for p in cfg.get("prompts", [])]
    if len(ids) != len(set(ids)):
        sys.exit("prompt ids must be unique")
    bad = set(cfg["engines"]) - set(ENDPOINT)
    if bad:
        sys.exit(f"unknown engines {bad}; use chatgpt, gemini, google")
    return cfg


def wave_dir(cfg, wave):
    d = os.path.join(cfg["_dir"], "waves", wave)
    os.makedirs(d, exist_ok=True)
    return d


def read_raw(d):
    """Last successful record per (prompt, engine, run)."""
    best, path = {}, os.path.join(d, "raw.jsonl")
    if os.path.exists(path):
        for line in open(path):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("ok"):
                best[(r["prompt_id"], r["engine"], r["run"])] = r
    return best


def post(key, scraper, inp):
    req = urllib.request.Request(API.format(scraper), data=json.dumps({"input": inp}).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=330) as r:
        return json.loads(r.read())


class Pacer:
    """Spaces request starts so Renderdata's 30/minute limit holds across threads."""
    def __init__(self):
        self.lock, self.t = threading.Lock(), 0.0

    def wait(self):
        with self.lock:
            delay = max(0.0, self.t + MIN_GAP - time.time())
            self.t = time.time() + delay
        time.sleep(delay)


def need_key():
    key = os.environ.get("RENDERDATA_API_KEY")
    if not key:
        sys.exit("set RENDERDATA_API_KEY (free key with 10 credits: https://platform.renderdata.cloud/login)")
    return key


def fetch_with_retry(key, scraper, inp, pacer, abort):
    """Returns (response_dict_or_{}, error). Stops all workers on auth/credit/endpoint errors."""
    err = None
    for attempt in range(3):
        if abort.is_set():
            return {}, "aborted"
        pacer.wait()
        try:
            rec = post(key, scraper, inp)
            if rec.get("ok"):
                return rec, None
            err = rec.get("error") or "not ok"
        except urllib.error.HTTPError as x:
            err = f"HTTP {x.code}: {x.read()[:200].decode(errors='ignore')}"
            if x.code in (401, 402, 403, 404):
                abort.set()
                print("fatal:", err, file=sys.stderr)
                return {}, err
        except Exception as x:
            err = repr(x)[:200]
        time.sleep(5 * (attempt + 1))
    return {}, err


# ---------- seed from GSC / Bing ----------
Q_COLS = ("top queries", "query", "queries", "keyword", "keywords", "search query", "grounding query",
          "grounding queries", "prompt", "prompts")
IMP_COLS = ("impressions", "impr", "citations", "cited", "count", "volume", "searches")
POS_COLS = ("position", "avg. position", "average position", "avg position")
QUESTION_RX = re.compile(r"^(who|what|which|why|how|when|where|can|could|should|is|are|does|do|will|would)\b|\?$", re.I)
INTENT_RX = [("comparison", r"\b(vs\.?|versus|compared? (to|with)|difference between)\b"),
             ("alternatives", r"\b(alternatives?|competitors?|instead of|(sites|apps|tools|software) like|similar to)\b"),
             ("pricing", r"\b(price|prices|pricing|cost|costs|cheap|cheapest|affordable|free|plans?)\b"),
             ("best-of", r"\b(best|top|recommend(ed)?|leading|good)\b"),
             ("how-to", r"^(how|what|why|when|where|which|can|should|is|are|does|do)\b|\bhow to\b")]


def classify(q, own_rx):
    if own_rx.search(q):
        return "reputation"
    for name, rx in INTENT_RX:
        if re.search(rx, q, re.I):
            return name
    return "topic"


def num(s):
    try:
        return float(re.sub(r"[^\d.]", "", str(s)) or 0)
    except ValueError:
        return 0.0


def find_col(headers, names):
    h = {k: (k or "").strip().lower() for k in headers}
    for n in names:
        for k, v in h.items():
            if v == n:
                return k
    for n in names:
        for k, v in h.items():
            if n in v:
                return k
    return None


def read_rows(path):
    if path.lower().endswith(".zip"):
        z = zipfile.ZipFile(path)
        names = [n for n in z.namelist() if n.lower().endswith((".csv", ".tsv"))]
        pick = [n for n in names if re.search(r"quer|keyword|prompt|grounding", n, re.I)] or names
        texts = [z.read(n).decode("utf-8-sig", "ignore") for n in pick]
    else:
        texts = [open(path, encoding="utf-8-sig", errors="ignore").read()]
    rows = []
    for t in texts:
        first = t.split("\n", 1)[0]
        delim = "\t" if first.count("\t") > first.count(",") else ","
        rows += list(csv.DictReader(io.StringIO(t), delimiter=delim))
    return rows


def cmd_seed(cfg, files, max_n):
    own = brand_rx(cfg["brand"])
    cands = {}
    for path, source in files:
        rows = read_rows(path)
        if not rows:
            print(f"warning: no rows in {path}", file=sys.stderr)
            continue
        qc, ic, pc = (find_col(rows[0].keys(), c) for c in (Q_COLS, IMP_COLS, POS_COLS))
        if not qc:
            sys.exit(f"{path}: no query column among {list(rows[0].keys())}")
        for r in rows:
            q = " ".join((r.get(qc) or "").split())
            if not q or len(q) > 300:
                continue
            c = cands.setdefault(q.lower(), {"text": q, "impressions": 0.0, "sources": set(), "position": None})
            c["impressions"] += num(r.get(ic)) if ic else 0
            c["sources"].add(source)
            if pc and r.get(pc) and num(r[pc]) > 0:
                c["position"] = min(filter(None, [c["position"], num(r[pc])]))
    by_intent, rep = defaultdict(list), []
    for c in cands.values():
        words = len(c["text"].split())
        is_q = bool(QUESTION_RX.search(c["text"]))
        if words < 2 and "bing-ai" not in c["sources"]:
            continue  # one-word queries are navigational or too vague to be prompts
        c["score"] = math.log1p(c["impressions"]) + 1.5 * is_q + 0.3 * min(words, 10) + 2 * ("bing-ai" in c["sources"])
        c["needs_rewrite"] = not is_q and words < 6
        intent = classify(c["text"], own)
        (rep if intent == "reputation" else by_intent[intent]).append(c)
    # dedupe near-duplicates within an intent, then round-robin across intents for balance
    queues = {}
    for intent, lst in by_intent.items():
        kept = []
        for c in sorted(lst, key=lambda c: -c["score"]):
            t = toks(c["text"])
            if t and all(len(t & toks(k["text"])) / len(t | toks(k["text"])) < 0.7 for k in kept):
                kept.append(c)
        queues[intent] = kept
    picked, i = [], 0
    while len(picked) < max_n and any(i < len(q) for q in queues.values()):
        for intent in sorted(queues):
            if i < len(queues[intent]) and len(picked) < max_n:
                picked.append((intent, queues[intent][i]))
        i += 1
    seen, out = Counter(), []
    for intent, c in picked:
        seen[intent] += 1
        out.append({"id": f"s-{intent}-{seen[intent]}", "text": c["text"], "seed": c["text"], "intent": intent,
                    "group": "holdout" if seen[intent] % 4 == 0 else "treated",
                    "source": "+".join(sorted(c["sources"])), "impressions": round(c["impressions"]),
                    "position": c["position"], "needs_rewrite": c["needs_rewrite"]})
    reputation = [{"id": f"s-reputation-{n + 1}", "text": c["text"], "seed": c["text"], "intent": "reputation",
                   "group": "treated", "source": "+".join(sorted(c["sources"])), "impressions": round(c["impressions"])}
                  for n, c in enumerate(sorted(rep, key=lambda c: -c["score"])[:10])]
    path = os.path.join(cfg["_dir"], "prompt_candidates.json")
    json.dump({"prompts": out, "reputation": reputation}, open(path, "w"), indent=2)
    print(f"{len(cands)} unique queries read · {len(out)} picked · {len(reputation)} branded kept for reputation")
    print("by intent:", dict(Counter(p["intent"] for p in out)))
    print("by source:", dict(Counter(p["source"] for p in out)))
    print(f"needs rewrite into a natural question: {sum(p['needs_rewrite'] for p in out)}")
    print(f"wrote {path}")


# ---------- plan ----------
def cmd_plan(cfg, icc=0.3):
    P = len(cfg["prompts"])
    credits = secs = 0
    for e, R in cfg["engines"].items():
        n_eff = P * R / (1 + (R - 1) * icc)
        hw = Z * math.sqrt(0.25 / n_eff) if n_eff else 1
        credits += P * R
        secs += P * R * SECS[e]
        print(f"  {e:8} {P} prompts x {R} runs = {P * R} calls · effective n {n_eff:.0f} · ±{100 * hw:.0f} pts worst case")
    print(f"  total {credits} credits (${credits / 1000:.2f}) · about {secs / CONCURRENCY / 3600:.1f} h at {CONCURRENCY} concurrent")
    print(f"  precision assumes prompt ICC {icc}; the report measures the real spread")


# ---------- run ----------
def cmd_run(cfg, wave, yes):
    d = wave_dir(cfg, wave)
    done = read_raw(d)
    # run index outermost spreads each prompt's repeats across the whole window
    jobs = [(p, e, r) for r in range(max(cfg["engines"].values()))
            for p in cfg["prompts"] for e, n in cfg["engines"].items()
            if r < n and (p["id"], e, r) not in done]
    est = sum(SECS[e] for _, e, _ in jobs)
    print(f"{len(done)} done, {len(jobs)} to run · up to {len(jobs)} credits (${len(jobs) / 1000:.2f}) · ~{est / CONCURRENCY / 3600:.1f} h")
    if not jobs:
        return
    if not yes:
        sys.exit("dry run. add --yes to spend credits")
    key, pacer, abort, lock, stats = need_key(), Pacer(), threading.Event(), threading.Lock(), Counter()
    out = open(os.path.join(d, "raw.jsonl"), "a")

    def work(job):
        p, e, r = job
        inp = f"{p['text']} :: {cfg.get('country', 'us')} :: {cfg.get('language', 'en')}" if e == "google" else p["text"]
        t0 = time.time()
        rec, err = fetch_with_retry(key, ENDPOINT[e][cfg.get("mode", "fast")], inp, pacer, abort)
        line = {"prompt_id": p["id"], "prompt": p["text"], "group": p.get("group", "treated"), "intent": p.get("intent"),
                "engine": e, "run": r, "ok": bool(rec.get("ok")), "credits": rec.get("credits", 0),
                "seconds": round(time.time() - t0, 1), "error": err,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "data": rec.get("data")}
        with lock:
            out.write(json.dumps(line) + "\n")
            out.flush()
            stats["ok" if line["ok"] else "fail"] += 1
            if sum(stats.values()) % 10 == 0:
                print(f"  {sum(stats.values())}/{len(jobs)} · {stats['ok']} ok · {stats['fail']} failed", flush=True)

    with ThreadPoolExecutor(CONCURRENCY) as ex:
        list(ex.map(work, jobs))
    out.close()
    print(f"finished: {stats['ok']} ok, {stats['fail']} failed (failed calls are not billed). Re-run to fill gaps.")


# ---------- analysis ----------
def extract(rec):
    d = rec.get("data") or {}
    if rec["engine"] == "google":
        ai = d.get("aiOverview") or {}
        text = ai.get("text") or ""
        return {"text": text, "md": text, "urls": [c["url"] for c in ai.get("citations") or [] if c.get("url")],
                "organic": [(o.get("position"), o.get("url")) for o in d.get("organic") or [] if o.get("url")],
                "queries": [], "aio": bool(text)}
    junk = r"https?://googleusercontent\.com/\S+"  # Gemini widget placeholders
    text = re.sub(junk, " ", d.get("answerText") or d.get("answer") or "")
    md = re.sub(junk, " ", d.get("answer") or text)
    urls = []
    for c in (d.get("citations") or []) + (d.get("sources") or []):
        if c.get("url") and c["url"] not in urls:
            urls.append(c["url"])
    return {"text": text, "md": md, "urls": urls, "organic": [], "queries": d.get("searchQueries") or [], "aio": None}


def build_rows(cfg, recs):
    tracked = [cfg["brand"]] + cfg.get("competitors", [])
    rx = {b["name"]: brand_rx(b) for b in tracked}
    prompts = {p["id"]: p for p in cfg["prompts"]}
    rows = []
    for (pid, e, run), r in sorted(recs.items()):
        if pid not in prompts:
            continue  # prompt removed from config
        x = extract(r)
        pos = {n: m.start() for n, R in rx.items() if (m := R.search(x["text"]))}
        rows.append({"pid": pid, "p": prompts[pid], "e": e, "run": run, "x": x, "pos": pos,
                     "first": min(pos, key=pos.get) if pos else None, "doms": {host(u) for u in x["urls"]} - {""}})
    return rows, tracked, prompts


ENT_PATTERNS = [re.compile(r"\*\*([^*\n]{2,48}?)\*\*"),
                re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+\**([A-Z0-9][^\n:—–(*]{1,40}?)\**\s*(?:[—–:(]|\s-\s)", re.M),
                re.compile(r"^#{1,4}\s+(?:\d+[.)]\s*)?([A-Z0-9][^\n:—–(]{1,40})", re.M),
                re.compile(r":\s+([A-Z][\w.&'+]*(?:\s[A-Z][\w.&'+]*){0,3})\s+[—–]")]
GENERIC_START = ("best", "top", "great", "good", "ideal", "most", "why", "how", "what", "when", "which", "if", "for", "the ",
                 "key", "main", "free", "paid", "pros", "cons", "price", "pricing", "cost", "step", "tip", "option", "note",
                 "bottom", "verdict", "summary", "overview", "conclusion", "recommend", "quick", "final", "use ", "choose",
                 "pick", "budget", "small", "large", "enterprise", "startup", "feature", "integration", "ease", "support",
                 "customer", "scalab", "learning", "setup", "security", "who ", "my ", "our ", "your ", "a ", "an ",
                 "option", "alternative", "comparison", "table", "example", "sources", "related", "here", "also", "in ",
                 "on ", "with ", "without", "important", "caveat", "consider", "look", "start", "avoid", "if ", "you ")


COMMON = set("""professional high low medium basic starter standard premium plus pro team teams business growth essential
essentials enterprise advanced ultimate free trial paid yes no none best good great excellent average limited unlimited
monthly annual annually yearly overview summary verdict pricing features integrations support onboarding fees fee cost
costs price prices plan plans tier tiers users user seats seat sales marketing service automation reporting analytics
mobile api crm email calling sms dialing pipeline deals contacts leads ai note tip important bottom line recommended
recommendation option options alternative alternatives comparison winner runner small large mid startups startup""".split())


def ent_candidates(md):
    """Brand-like names from list leads, bold spans and headings. Brands are Title Case or single tokens,
    so phrases with lowercase words, prices or plan/feature vocabulary are dropped."""
    found = set()
    for rx in ENT_PATTERNS:
        for m in rx.finditer(md):
            s = re.sub(r"^\d+[.)]\s*", "", m.group(1)).strip(" .,:;-–—()*_`#")
            words, low = s.split(), s.lower()
            if not (1 <= len(words) <= 4) or len(s) < 2 or not re.search(r"[A-Za-z]", s) or re.search(r"[%$€£]", s):
                continue
            if low.startswith(GENERIC_START) or " vs" in low:
                continue
            if any(not (w[:1].isupper() or w[:1].isdigit() or w in ("&", "+", ".")) for w in words):
                continue  # "Power dialing", "Built-in calling and SMS"
            if all(w.lower().strip(".:") in COMMON for w in words):
                continue  # "Professional", "Onboarding Fees"
            found.add(s)
    return found


def discover(rows, tracked, min_answers=2):
    known = {n.lower() for b in tracked for n in [b["name"]] + list(b.get("aliases") or [])}
    seed = Counter()
    for r in rows:
        seed.update(ent_candidates(r["x"]["md"]))
    all_hosts = Counter(h for r in rows for h in r["doms"])
    out = []
    for name, c in seed.most_common(80):
        if c < min_answers or name.lower() in known or any(k in name.lower().split() for k in known):
            continue
        rx = re.compile(r"(?<![\w])%s(?![\w])" % re.escape(name))
        hit = [r for r in rows if rx.search(r["x"]["text"])]
        if len(hit) < min_answers:
            continue
        slug = re.sub(r"[^a-z0-9]", "", name.lower())
        first = re.sub(r"[^a-z0-9]", "", name.lower().split()[0])
        dom = next((h for h, _ in all_hosts.most_common() if len(first) > 2 and
                    (h.split(".")[-2] if h.count(".") else h).startswith((slug, first))), None)
        out.append({"name": name, "answers": len(hit), "share": len(hit) / len(rows),
                    "engines": sorted({r["e"] for r in hit}), "prompts": len({r["pid"] for r in hit}),
                    "domain": dom, "rows": hit})
    return sorted(out, key=lambda x: -x["answers"])[:20]


def cosources(sub, rows, k=6):
    """Domains over-represented in answers that name a brand: where that brand gets named."""
    if not sub:
        return []
    base = Counter(h for r in rows for h in r["doms"])
    c = Counter(h for r in sub for h in r["doms"])
    lift = {h: (n / len(sub)) / (base[h] / len(rows)) for h, n in c.items()}
    return [(h, n, lift[h]) for h, n in sorted(c.items(), key=lambda t: (-t[1], -lift[t[0]])) if n >= 2][:k]


def sentence(text, i):
    a = max(text.rfind(s, 0, i) for s in (". ", "\n", "! ", "? "))
    b = min([j for j in (text.find(s, i) for s in (". ", "\n", "! ", "? ")) if j != -1] or [len(text)])
    s = " ".join(text[a + 1:b + 1].split())
    return (s if len(s) >= 40 else " ".join(text[i:i + 300].split()))[:320]  # bare heading: take what follows


def robots_sitemaps(domain):
    try:
        x = urllib.request.urlopen(urllib.request.Request(f"https://{domain}/robots.txt", headers={"User-Agent": UA}),
                                   timeout=20).read().decode(errors="ignore")
        return re.findall(r"(?im)^\s*sitemap:\s*(\S+)", x)
    except Exception as e:
        print(f"robots.txt fetch failed for {domain}: {e} (sitemap matching skipped; set `sitemap` in config)", file=sys.stderr)
        return []


def locale_of(url):
    """'pt' for /pt/page or /sitemaps/pt-blog.xml, 'zh-hant' for /zh-hant/x; None if unlocalized."""
    m = re.search(r"(?:^|/)([a-z]{2}(?:-[a-z]{2,4})?)(?=/|[-_.][a-z])", urlparse(url).path.lower())
    return m.group(1).split("-")[0] if m and m.group(1)[:2] not in ("ww",) else None


def sitemap_urls(cfg, cache):
    if os.path.exists(cache):
        return open(cache).read().split()
    dom = (cfg["brand"].get("domain") or "").removeprefix("www.")
    todo = [u for u in [cfg.get("sitemap")] if u] + (robots_sitemaps(dom) if dom else [])
    out, seen = [], set()
    lang = (cfg.get("language") or "en").lower()
    # pop() takes from the end: sort so sitemaps in the panel's language (or unlocalized) are fetched first
    pref = lambda u: 1 if (locale_of(u) or lang).startswith(lang) else 0
    while todo and len(seen) < 60:
        todo.sort(key=pref)
        u = todo.pop()
        if u in seen:
            continue
        seen.add(u)
        time.sleep(0.3)  # be polite to the site
        try:
            x = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=30).read()
            x = gzip.decompress(x) if x[:2] == b"\x1f\x8b" else x
        except urllib.error.HTTPError as e:
            print(f"sitemap fetch failed {u}: {e}", file=sys.stderr)
            if e.code == 429:
                print("site is rate-limiting; using the sitemaps fetched so far", file=sys.stderr)
                break
            continue
        except Exception as e:
            print(f"sitemap fetch failed {u}: {e}", file=sys.stderr)
            continue
        is_index = b"<sitemapindex" in x
        for loc in re.findall(rb"<loc>\s*([^<\s]+)\s*</loc>", x):
            loc = loc.decode(errors="ignore")
            (todo if is_index else out).append(loc)
    out = out[:20000]
    if out:  # don't cache a failure
        open(cache, "w").write("\n".join(out))
    return out


def analyze(cfg, wave):
    """All numbers for one wave, as plain JSON-able data. Report and dashboard both render from this."""
    d = os.path.join(cfg["_dir"], "waves", wave)
    recs = read_raw(d)
    if not recs:
        return None, []
    rows, tracked, prompts = build_rows(cfg, recs)
    B = cfg["brand"]["name"]
    comps = [c["name"] for c in cfg.get("competitors", [])]
    engines = list(cfg["engines"])
    rep = [r for r in rows if r["p"].get("intent") == "reputation"]
    rows = [r for r in rows if r["p"].get("intent") != "reputation"]
    vis = {pid: p for pid, p in prompts.items() if p.get("intent") != "reputation"}
    cell = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: [0, 0]))))
    answers, nocite, aio, org_pos, own_urls, fan = Counter(), Counter(), Counter(), [], Counter(), Counter()
    pdoms = defaultdict(Counter)
    for r in rows:
        e, pid, x = r["e"], r["pid"], r["x"]
        answers[e] += 1
        nocite[e] += not x["urls"]
        aio[e] += bool(x["aio"])
        pdoms[pid].update(r["doms"])
        for b in tracked:
            n = b["name"]
            for metric, hit in (("mention", n in r["pos"]), ("first", r["first"] == n),
                                ("cited", any(on_domain(u, b.get("domain")) for u in x["urls"]))):
                c = cell[e][n][metric][pid]
                c[0] += hit
                c[1] += 1
        own_urls.update(u.split("#")[0] for u in x["urls"] if on_domain(u, cfg["brand"].get("domain")))
        fan.update(q.lower().strip() for q in x["queries"])
        if e == "google":
            best = next((p for p, u in x["organic"] if on_domain(u, cfg["brand"].get("domain"))), None)
            org_pos.append(best)
            c = cell[e][B]["organic10"][pid]
            c[0] += best is not None and best <= 10
            c[1] += 1

    def ci(e, n, metric, pids=None):
        return rate_ci([v for p, v in cell[e][n][metric].items() if pids is None or p in pids])

    def pooled(pid, n):
        h = sum(cell[e][n]["mention"][pid][0] for e in engines)
        t = sum(cell[e][n]["mention"][pid][1] for e in engines)
        return h / t if t else None

    live = [e for e in engines if answers[e]]
    an = {"wave": wave, "brand": B, "competitors": comps, "engines": live, "n_prompts": len(vis),
          "date": max((r.get("ts") or "")[:10] for r in recs.values()),
          "answers": {e: answers[e] for e in live},
          "expected": {e: len(vis) * cfg["engines"][e] for e in live},
          "nocite": {e: nocite[e] / answers[e] for e in live},
          "headline": {e: {m: ci(e, B, m) for m in ("mention", "first", "cited")} for e in live}}
    if answers["google"]:
        found = sorted(p for p in org_pos if p)
        an["aio"] = aio["google"] / answers["google"]
        an["organic10"] = ci("google", B, "organic10")
        an["organic_median"] = found[len(found) // 2] if found else None
    an["brands"] = {}
    for e in live:
        sov = Counter(n for r in rows if r["e"] == e for n in r["pos"])
        tot = sum(sov.values()) or 1
        an["brands"][e] = [{"name": b["name"], "me": b["name"] == B, "has_domain": bool(b.get("domain")),
                            "mention": ci(e, b["name"], "mention"), "first": ci(e, b["name"], "first"),
                            "cited": ci(e, b["name"], "cited") if b.get("domain") else None,
                            "sov": sov[b["name"]] / tot} for b in tracked]
    an["intents"] = []
    for it in sorted({p.get("intent", "topic") for p in vis.values()}):
        pids = {pid for pid, p in vis.items() if p.get("intent", "topic") == it}
        cells = {}
        for e in live:
            me = ci(e, B, "mention", pids)
            riv = max((((ci(e, c, "mention", pids) or {"rate": 0})["rate"]), c) for c in comps) if comps else (0, None)
            cells[e] = {"me": me["rate"] if me else None, "rival": riv[1] if riv[0] else None, "rival_rate": riv[0]}
        an["intents"].append({"intent": it, "prompts": len(pids), "cells": cells})
    an["prompt_domains"] = {pid: [h for h, _ in c.most_common(6)] for pid, c in pdoms.items()}
    an["gaps"], an["movable"] = [], []
    for pid, p in vis.items():
        me = pooled(pid, B)
        if me is None:
            continue
        riv = sorted(((c, pooled(pid, c)) for c in comps), key=lambda t: -(t[1] or 0))
        strong = [{"name": c, "rate": v} for c, v in riv if v is not None and v >= 0.5]
        base = {"pid": pid, "text": p["text"], "intent": p.get("intent"), "me": me,
                "impressions": p.get("impressions"), "position": p.get("position")}
        if strong and me < 0.2:
            score = (strong[0]["rate"] - me) * (1 + math.log1p(p.get("impressions") or 0))
            an["gaps"].append({**base, "rivals": strong, "score": score})
        if 0.2 <= me <= 0.8:
            an["movable"].append({**base, "rivals": [c for c, v in riv if (v or 0) >= 0.5]})
    an["gaps"].sort(key=lambda g: -g["score"])
    an["movable"].sort(key=lambda m: -m["me"])  # closest to winning first
    an["discovered"] = [{**{k: v for k, v in x.items() if k != "rows"},
                         "sources": [h for h, _, _ in cosources(x["rows"], rows, 4)]} for x in discover(rows, tracked)]
    an["cosources"] = {}
    for n in [B] + comps:
        sub = [r for r in rows if n in r["pos"]]
        an["cosources"][n] = {"answers": len(sub), "domains": [[h, c, round(l, 2)] for h, c, l in cosources(sub, rows)]}
    tracked_doms = {(b.get("domain") or "").lower() for b in tracked}
    rival_only = Counter(h for r in rows if B not in r["pos"] and any(c in r["pos"] for c in comps) for h in r["doms"])
    mine = Counter(h for r in rows if B in r["pos"] for h in r["doms"])
    n_mine = max(1, sum(1 for r in rows if B in r["pos"]))
    an["targets"] = [[h, c] for h, c in rival_only.most_common(40)
                     if c >= 2 and h not in tracked_doms and mine[h] / n_mine < 0.1][:15]
    an["top_domains"] = {e: [[h, c / answers[e]] for h, c in Counter(h for r in rows if r["e"] == e
                                                                     for h in r["doms"]).most_common(15)] for e in live}
    seen, an["excerpts"] = set(), []
    for r in rows:
        if B in r["pos"] and (r["pid"], r["e"]) not in seen and len(seen) < 12:
            seen.add((r["pid"], r["e"]))
            an["excerpts"].append({"engine": r["e"], "pid": r["pid"], "text": sentence(r["x"]["text"], r["pos"][B])})
    an["reputation"] = [{"engine": r["e"], "prompt": r["p"]["text"], "text": " ".join(r["x"]["text"].split())[:400],
                         "sources": sorted(r["doms"])[:6]} for r in rep[:12]]
    an["fanout"] = []
    if fan:
        site = sitemap_urls(cfg, os.path.join(cfg["_dir"], "sitemap_urls.txt"))
        stop = {"en", "us", "uk", "de", "fr", "es", "blog", "resource", "learn", "guide", "page", "index", "p", "post", "article"}
        # one entry per page: strip locale prefixes (/tr/, /pt-br/) and keep the English or unprefixed copy
        canon = {}
        for u in site:
            path = urlparse(u).path
            m = re.match(r"^/([a-z]{2}(?:-[a-z]{2,4})?)(/.*)?$", path, re.I)
            loc, rest = (m.group(1).lower(), m.group(2) or "/") if m else (None, path)
            rank = 0 if loc is None else 1 if loc.startswith((cfg.get("language") or "en").lower()) else 2
            if rest not in canon or rank < canon[rest][0]:
                canon[rest] = (rank, u)
        keep = [(r, u) for r, u in canon.values()]
        if any(r <= 1 for r, _ in keep):  # site has pages in the panel's language: ignore other languages
            keep = [(r, u) for r, u in keep if r <= 1]
        slugs = [(u, t) for _, u in keep if (t := toks(urlparse(u).path) - stop)]
        an["site_pages"] = len(site)
        for q, c in fan.most_common(20):
            item = {"q": q, "n": c, "status": None, "url": None}
            qt = toks(q)
            if slugs and qt:
                # fan-out queries are long; a page matches when most of ITS slug words are in the query
                hit, share, u = max(((len(qt & t), len(qt & t) / len(t), u) for u, t in slugs))
                item.update(status="page" if hit >= 2 and share >= 0.6 else "partial" if hit >= 2 else "none",
                            url=u if hit >= 2 else None)
            an["fanout"].append(item)
    an["own_pages"] = [[u, c] for u, c in own_urls.most_common(15)]
    an["grid"] = [{"pid": pid, "text": p["text"], "intent": p.get("intent", ""), "group": p.get("group", "treated"),
                   "impressions": p.get("impressions"),
                   "cells": {e: cell[e][B]["mention"][pid] for e in live}} for pid, p in vis.items()]
    an["groups"] = {pid: p.get("group", "treated") for pid, p in vis.items()}
    an["cells"] = {e: {b["name"]: {m: dict(cell[e][b["name"]][m]) for m in ("mention", "first", "cited")}
                       for b in tracked} for e in live}
    return an, rows + rep


def render_md(an):
    B, eng = an["brand"], an["engines"]
    L = [f"# AI visibility report: {B} · wave `{an['wave']}`", "",
         f"{an['n_prompts']} visibility prompts · "
         + ", ".join("%s %d/%d answers" % (e, an["answers"][e], an["expected"][e]) for e in eng)
         + " · logged-out sessions via Renderdata", "",
         "Rates are per answer, averaged over prompts, with 95% ranges. No AI Overview counts as not visible. "
         "Overlapping ranges mean no detectable difference.", "",
         "## 1. Headline", "", "| Engine | Mentioned | Named first | Domain cited | No-citation answers |", "|---|---|---|---|---:|"]
    for e in eng:
        h = an["headline"][e]
        L.append(f"| {e} | {fmt(h['mention'])} | {fmt(h['first'])} | {fmt(h['cited'])} | {pct(an['nocite'][e])} |")
    if "aio" in an:
        L += ["", f"Google: AI Overview shown on {pct(an['aio'])} of runs. {B} in organic top 10: {fmt(an['organic10'])}"
                  + (f", median best position {an['organic_median']}." if an.get("organic_median") else ".")]
    L += ["", "## 2. Competitive picture", ""]
    for e in eng:
        L += [f"**{e}**", "", "| Brand | Mentioned | Named first | Domain cited | Share of voice |", "|---|---|---|---|---:|"]
        for b in sorted(an["brands"][e], key=lambda b: -(b["mention"] or {"rate": 0})["rate"]):
            n = f"**{b['name']}**" if b["me"] else b["name"]
            L.append(f"| {n} | {fmt(b['mention'])} | {fmt(b['first'])} | {fmt(b['cited']) if b['has_domain'] else 'n/a'} | {pct(b['sov'])} |")
        L.append("")
    L += ["## 3. By intent", "", "| Intent | Prompts | " + " | ".join(f"{e}: you / best rival" for e in eng) + " |",
          "|---|---:|" + "---|" * len(eng)]
    for it in an["intents"]:
        vals = [f"{pct(c['me'])} / " + (f"{c['rival']} {pct(c['rival_rate'])}" if c["rival"] else "–")
                for c in (it["cells"][e] for e in eng)]
        L.append(f"| {it['intent']} | {it['prompts']} | " + " | ".join(vals) + " |")
    L += ["", "## 4. Gaps: rivals named, you're not", "", "Rival ≥50%, you <20%, ranked by gap × search demand.", ""]
    for g in an["gaps"]:
        extra = (f" · {g['impressions']} impr" if g.get("impressions") else "") + \
                (f" · you rank ~#{g['position']:.0f} in search" if g.get("position") else "")
        L.append(f"- `{g['pid']}` ({g['intent']}) \"{g['text']}\": you {pct(g['me'])} · "
                 + ", ".join(f"{r['name']} {pct(r['rate'])}" for r in g["rivals"]) + extra)
    if not an["gaps"]:
        L.append("- none at this threshold")
    L += ["", "## 5. Movable prompts (named in 20–80% of runs)", ""]
    L += [f"- `{m['pid']}` \"{m['text']}\": you {pct(m['me'])}" + (f" · alongside {', '.join(m['rivals'])}" if m["rivals"] else "")
          for m in an["movable"]] or ["- none"]
    L += ["", "## 6. Brands the engines name that you don't track", "",
          "Auto-detected; check them. Add real rivals to `competitors` and re-run `report` (free).", "",
          "| Name | Answers | Engines | Prompts | Likely domain | Named alongside |", "|---|---:|---|---:|---|---|"]
    L += [f"| {x['name']} | {pct(x['share'])} | {', '.join(x['engines'])} | {x['prompts']} | {x['domain'] or '?'} | "
          f"{', '.join(x['sources']) or '–'} |" for x in an["discovered"]] or ["| none found | | | | | |"]
    L += ["", "## 7. Where each brand gets named", "", "Domains in answers naming the brand, with lift vs all answers.", ""]
    for n, v in an["cosources"].items():
        L.append(f"- **{n}** ({v['answers']} answers): " + (", ".join(f"{h} {c} (×{l})" for h, c, l in v["domains"]) or "–"))
    L += ["", "**Outreach targets** (cited when rivals are named and you aren't):", ""]
    L += [f"- {h}: {c} answers" for h, c in an["targets"]] or ["- none"]
    L += ["", "## 8. Top cited domains per engine", ""]
    L += [f"- **{e}:** " + ", ".join(f"{h} {pct(s)}" for h, s in an["top_domains"][e]) for e in eng]
    L += ["", "## 9. How the engines describe you", ""]
    L += [f"- *{x['engine']}, `{x['pid']}`:* {x['text']}" for x in an["excerpts"]] or ["- not mentioned in any answer"]
    if an["reputation"]:
        L += ["", "**Reputation prompts** (excluded from rates):", ""]
        L += [f"- *{x['engine']}, \"{x['prompt']}\":* {x['text']}" + (f" — sources: {', '.join(x['sources'])}" if x["sources"] else "")
              for x in an["reputation"]]
    if an["fanout"]:
        L += ["", "## 10. What ChatGPT actually searched", ""]
        if an.get("site_pages"):
            L.append(f"Matched against {an['site_pages']} sitemap URLs.\n")
        for f in an["fanout"]:
            tail = {"page": f" → {f['url']}", "partial": f" → **no page** (closest: {f['url']})", "none": " → **no page**"}.get(f["status"], "")
            L.append(f"- {f['q']} ({f['n']}){tail}")
    L += ["", "## 11. Your pages that get cited", ""]
    L += [f"- {u} ({c} answers)" for u, c in an["own_pages"]] or ["- none"]
    L += ["", "## 12. Per-prompt detail", "", "| Prompt | Intent | Group | " + " | ".join(eng) + " |", "|---|---|---|" + "---|" * len(eng)]
    L += [f"| `{g['pid']}` | {g['intent']} | {g['group']} | " + " | ".join(f"{g['cells'][e][0]}/{g['cells'][e][1]}"
                                                                         if g["cells"][e][1] else "–" for e in eng) + " |"
          for g in an["grid"]]
    return "\n".join(L) + "\n"


def cmd_report(cfg, wave):
    an, rows = analyze(cfg, wave)
    if not an:
        sys.exit("no successful runs in this wave")
    d = wave_dir(cfg, wave)
    md = render_md(an)
    open(os.path.join(d, "report.md"), "w").write(md)
    json.dump(an, open(os.path.join(d, "analysis.json"), "w"))
    with open(os.path.join(d, "runs.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["wave", "prompt_id", "intent", "group", "engine", "run", "brands_named", "first_named",
                    "cited_domains", "aio_shown", "answer_chars"])
        for r in rows:
            w.writerow([wave, r["pid"], r["p"].get("intent"), r["p"].get("group"), r["e"], r["run"],
                        ";".join(sorted(r["pos"], key=r["pos"].get)), r["first"] or "", ";".join(sorted(r["doms"])),
                        r["x"]["aio"] if r["e"] == "google" else "", len(r["x"]["text"])])
    print(md)
    print(f"saved {d}/report.md, analysis.json, runs.csv")


# ---------- pages ----------
def page_table(cfg, wave, top=25):
    """Top cited third-party URLs with which tracked brands each names, from the local cache."""
    d = wave_dir(cfg, wave)
    rows, tracked, _ = build_rows(cfg, read_raw(d))
    tdoms = [b.get("domain") for b in tracked if b.get("domain")]
    cites = Counter()
    for r in rows:
        cites.update({u.split("#")[0] for u in r["x"]["urls"] if not any(on_domain(u, t) for t in tdoms)})
    cache, path = {}, os.path.join(d, "pages.jsonl")
    if os.path.exists(path):
        for line in open(path):
            x = json.loads(line)
            cache[x["url"]] = x
    B = cfg["brand"]["name"]
    rx = {b["name"]: brand_rx(b) for b in tracked}
    out = []
    for u, c in cites.most_common(top):
        x = cache.get(u)
        named = [n for n, R in rx.items() if R.search(x["markdown"])] if x and x.get("ok") else None
        out.append({"url": u, "cites": c, "title": (x or {}).get("title"), "fetched": x is not None,
                    "ok": bool(x and x.get("ok")), "named": [n for n in (named or []) if n != B],
                    "names_you": None if named is None else B in named})
    return out


def cmd_pages(cfg, wave, top, yes):
    d = wave_dir(cfg, wave)
    table = page_table(cfg, wave, top)
    todo = [p["url"] for p in table if not p["fetched"]]
    print(f"{len(table)} top cited third-party pages · {len(todo)} to fetch · {len(todo)} credits (${len(todo) / 1000:.3f})")
    if todo:
        if not yes:
            sys.exit("dry run. add --yes to spend credits")
        key, pacer, abort, lock = need_key(), Pacer(), threading.Event(), threading.Lock()
        out = open(os.path.join(d, "pages.jsonl"), "a")

        def work(u):
            rec, err = fetch_with_retry(key, "page-markdown", u, pacer, abort)
            dt = rec.get("data") or {}
            x = {"url": u, "ok": bool(rec.get("ok")), "title": dt.get("title"), "words": dt.get("wordCount"),
                 "markdown": (dt.get("markdown") or "")[:80000], "error": err}
            with lock:
                out.write(json.dumps(x) + "\n")
                out.flush()

        with ThreadPoolExecutor(CONCURRENCY) as ex:
            list(ex.map(work, todo))
        out.close()
        table = page_table(cfg, wave, top)
    L = [f"# Cited pages and who they name · wave `{wave}`", "", "| Page | Cited in | Names | Names you? |", "|---|---:|---|---|"]
    for p in table:
        if not p["ok"]:
            L.append(f"| {p['url']} | {p['cites']} | fetch failed | ? |")
        else:
            L.append(f"| [{(p['title'] or p['url'])[:70]}]({p['url']}) | {p['cites']} | {', '.join(p['named']) or '–'} | "
                     f"{'yes' if p['names_you'] else '**no**'} |")
    miss = [p for p in table if p["ok"] and not p["names_you"] and p["named"]]
    L += ["", "## Pages naming rivals but not you (pitch, review or partner targets)", ""]
    L += [f"- {p['url']} ({p['cites']} answers) names {', '.join(p['named'])}" for p in miss] or ["- none"]
    rep = "\n".join(L) + "\n"
    open(os.path.join(d, "pages.md"), "w").write(rep)
    print(rep)
    print(f"saved {d}/pages.md")


# ---------- actions + dashboard ----------
def auto_actions(an, pages):
    """Rule-based starter actions from the data. Claude's curated actions.json goes on top of these."""
    acts = []

    def add(kind, key, title, detail, tier, metric, prompts=()):
        acts.append({"id": f"auto-{kind}-" + re.sub(r"[^a-z0-9]+", "-", str(key).lower()).strip("-")[:60],
                     "title": title, "detail": detail, "tier": tier, "metric": metric, "prompts": list(prompts),
                     "source": "auto", "status": "todo", "kind": kind})

    B, pd = an["brand"], an["prompt_domains"]
    for x in [x for x in an["discovered"] if x["share"] >= 0.15 and x["domain"]][:3]:  # a cited domain = likely a real brand
        add("track", x["name"], f"Start tracking {x['name']}",
            f"Named in {pct(x['share'])} of answers ({', '.join(x['engines'])}) but not in your competitor list. "
            "Add it to config.json and re-run report.", "Setup", "Competitive picture")
    for m in an["movable"][:3]:
        add("movable", m["pid"], f"Tip “{m['text']}” over the line",
            f"You're named in {pct(m['me'])} of runs, so engines already consider you. On your best page for this "
            f"question, answer it in the title and first paragraph and name {B} there. Then get onto pages cited for it: "
            f"{', '.join(pd.get(m['pid'], [])[:4]) or 'see Sources'}.", "Strong (relevance)", "Mention rate", [m["pid"]])
    for g in an["gaps"][:3]:
        top = g["rivals"][0]
        add("gap", g["pid"], f"Win “{g['text']}”",
            f"{top['name']} is named in {pct(top['rate'])} of answers, you in {pct(g['me'])}. Earn coverage on the pages "
            f"engines cite for it: {', '.join(pd.get(g['pid'], [])[:4]) or 'see Sources'}."
            + (f" You already rank ~#{g['position']:.0f} in search, so fix that page's title and lead passage first."
               if g.get("position") and g["position"] <= 10 else ""), "Moderate", "Mention rate", [g["pid"]])
    for p in [p for p in pages if p["ok"] and not p["names_you"] and p["named"]][:4]:
        add("page", p["url"], f"Get listed on “{(p['title'] or host(p['url']))[:80]}”",
            f"Cited in {p['cites']} answers. It names {', '.join(p['named'])} but not {B}. Pitch with real proof, "
            "data or a review account; never pay for undisclosed placement.", "Moderate", "Mention rate on its prompts")
    for h, c in an["targets"][:4]:
        add("outreach", h, f"Get covered on {h}",
            f"Cited in {c} answers where rivals are named and you aren't, and almost never when you are.",
            "Moderate", "Outreach targets")
    for f in [f for f in an["fanout"] if f["status"] in ("none", "partial")][:4]:
        add("content", f["q"], f"Cover the search “{f['q'][:80]}”",
            ("Closest page: " + f["url"] + ". Retitle or extend it to match." if f["status"] == "partial"
             else "ChatGPT searches this and no page on your site matches. Build one."), "Strong (relevance)",
            "Citation and mention on related prompts")
    for e, h in an["headline"].items():
        if h["mention"] and h["first"] and h["mention"]["rate"] >= 0.5 and h["first"]["rate"] < 0.2:
            add("first", e, f"Become the first pick on {e}",
                f"Named in {pct(h['mention']['rate'])} of answers but first in only {pct(h['first']['rate'])}. "
                "First place follows consensus: reviews, independent comparisons, case studies with numbers.",
                "Moderate", "Named-first rate")
    if an.get("organic10") and an["organic10"]["rate"] < 0.3:
        add("seo", "google-organic", "Check classic SEO for these questions",
            f"Renderdata's Google results show you in the organic top 10 on {pct(an['organic10']['rate'])} of runs. "
            "Confirm positions in Search Console first; if they hold, ranking comes first, because AI Overviews draw "
            "from Google's index.", "Strong", "Organic top-10 presence")
    return acts


def cmd_dashboard(cfg):
    waves = sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(cfg["_dir"], "waves", "*", "raw.jsonl")))
    ans = [a for a in (analyze(cfg, w)[0] for w in waves) if a]
    if not ans:
        sys.exit("no waves with data yet")
    latest = ans[-1]
    pages = page_table(cfg, latest["wave"])
    ap = os.path.join(cfg["_dir"], "actions.json")
    curated = json.load(open(ap)) if os.path.exists(ap) else []
    ids = {a.get("id") for a in curated}
    for a in curated:
        a.setdefault("source", "claude")
        a.setdefault("status", "todo")
    actions = curated + [a for a in auto_actions(latest, pages) if a["id"] not in ids]
    slim = lambda a: {k: a[k] for k in ("wave", "date", "engines", "headline", "answers", "expected", "n_prompts")} | \
        {"aio": a.get("aio"), "organic10": a.get("organic10"),
         # per-brand rates per wave, for sparklines and the leaderboard's movement column
         "brands": {e: {b["name"]: [b["mention"]["rate"] if b.get("mention") else None, b.get("sov")] for b in bs}
                    for e, bs in a.get("brands", {}).items()},
         # per-prompt counts per brand, so the monitor can re-filter by model and tag in the browser
         "cells": {e: {n: {m: c[m] for m in ("mention", "first")} for n, c in bs.items()} for e, bs in a.get("cells", {}).items()}}
    data = {"brand": cfg["brand"]["name"], "domain": cfg["brand"].get("domain"), "country": cfg.get("country", "us"),
            "language": cfg.get("language", "en"), "generated": time.strftime("%Y-%m-%d %H:%M"),
            "runs": cfg["engines"], "history": [slim(a) for a in ans],
            "latest": {k: v for k, v in latest.items() if k not in ("cells", "groups")},
            "pages": [p for p in pages if p["fetched"]], "actions": actions,
            "domains": {b["name"]: b.get("domain") for b in [cfg["brand"]] + cfg.get("competitors", [])},
            "prompts": [{"id": p["id"], "text": p["text"], "intent": p.get("intent", ""), "impressions": p.get("impressions")}
                        for p in cfg["prompts"]],
            "feed": mention_feed(cfg, latest["wave"]),
            "pwb": os.path.abspath(__file__), "config": os.path.join(cfg["_dir"], "config.json")}
    blob = json.dumps(data).replace("</", "<\\/")  # keep scraped text from closing the script tag
    assets = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets")
    for tpl_name, out_name in (("dashboard.html", "dashboard.html"), ("monitor.html", "monitor.html")):
        out = os.path.join(cfg["_dir"], out_name)
        open(out, "w").write(open(os.path.join(assets, tpl_name)).read().replace("/*__DATA__*/null", blob))
        print(f"{out_name[:-5]}: {out}")
    print(f"({len(ans)} waves · {len(actions)} actions, {len(curated)} curated · {len(data['feed'])} answers in the feed)")


def tidy(s):
    """Snippet text without markdown table pipes, bold markers and heading hashes."""
    s = re.sub(r"\s*\|[\s|:-]*", " · ", re.sub(r"[*#`]+", "", s))
    return re.sub(r"(\s·\s)+", " · ", s).strip(" ·")


def mention_feed(cfg, wave, cap=600):
    """One record per answer in the wave, newest first: who was named in what order, plus a snippet per brand."""
    recs = read_raw(wave_dir(cfg, wave))
    rows, _, _ = build_rows(cfg, recs)
    feed = []
    for r in rows:
        text = r["x"]["text"]
        if not text.strip():
            continue  # Google search with no AI Overview: no answer to show
        order = sorted(r["pos"], key=r["pos"].get)
        feed.append({"e": r["e"], "pid": r["pid"], "run": r["run"], "ts": recs[(r["pid"], r["e"], r["run"])].get("ts"),
                     "order": order, "snip": {n: tidy(sentence(text, r["pos"][n])) for n in order},
                     "lead": tidy(" ".join(text[:260].split())), "doms": sorted(r["doms"])[:8]})
    feed.sort(key=lambda f: f["ts"] or "", reverse=True)
    return feed[:cap]


# ---------- diff / trend ----------
def fmt_d(r):
    return "n/a" if not r else f"{100 * r['diff']:+.0f} pts ({100 * r['lo']:+.0f} to {100 * r['hi']:+.0f})"


def load_summary(cfg, wave):
    d = wave_dir(cfg, wave)
    for f in ("analysis.json", "summary.json"):
        if os.path.exists(os.path.join(d, f)):
            return json.load(open(os.path.join(d, f)))
    sys.exit(f"run `report` for wave {wave} first")


def cmd_diff(cfg, a, b):
    A, Bw = load_summary(cfg, a), load_summary(cfg, b)
    name, groups = cfg["brand"]["name"], Bw["groups"]
    L = [f"# Change for {name}: `{a}` → `{b}`", "",
         "Paired over prompts in both waves. Treated minus holdout separates your work from engine drift.", "",
         "| Engine | Metric | Change (95% CI) | Treated minus holdout (95% CI) |", "|---|---|---|---|"]
    for e in Bw["cells"]:
        if e not in A["cells"]:
            continue
        for m in ("mention", "first", "cited"):
            ca, cb = A["cells"][e][name][m], Bw["cells"][e][name][m]
            ds = {p: cb[p][0] / cb[p][1] - ca[p][0] / ca[p][1] for p in cb if p in ca and ca[p][1] and cb[p][1]}
            dd = did([v for p, v in ds.items() if groups.get(p) != "holdout"],
                     [v for p, v in ds.items() if groups.get(p) == "holdout"])
            L.append(f"| {e} | {m} | {fmt_d(paired(list(ds.values())))} | {fmt_d(dd)} |")
    L += ["", "A range that crosses 0 is not a detectable change. Claim a win only on treated minus holdout."]
    print("\n".join(L))


def cmd_trend(cfg):
    name = cfg["brand"]["name"]
    waves = sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(cfg["_dir"], "waves", "*", "raw.jsonl")))
    if not waves:
        sys.exit("no waves yet")
    print(f"# Trend for {name}\n\n| Wave | Engine | Mentioned | Named first | Domain cited |\n|---|---|---|---|---|")
    for w in waves:
        s = load_summary(cfg, w)
        for e, brands in s["cells"].items():
            c = brands.get(name, {})
            print(f"| {w} | {e} | " + " | ".join(fmt(rate_ci(list(c.get(m, {}).values()))) for m in ("mention", "first", "cited")) + " |")


# ---------- selftest ----------
def selftest():
    lo, hi = wilson(5, 10)
    assert abs(lo - 0.2366) < 1e-3 and abs(hi - 0.7634) < 1e-3, (lo, hi)
    r = rate_ci([(7, 7), (0, 7), (7, 7), (0, 7)])
    assert r["rate"] == 0.5 and r["hi"] - r["lo"] > 0.5, r  # clustered: wide, not the naive ±12
    assert rate_ci([(0, 7), (0, 7)])["hi"] > 0
    rx = brand_rx({"name": "Close", "aliases": ["Close CRM"], "case_sensitive": True})
    assert rx.search("Try Close CRM.") and not rx.search("close the deal") and not rx.search("Closed")
    assert brand_rx({"name": "HubSpot"}).search("hubspot's free tier")
    assert on_domain("https://www.acme.com/x", "acme.com") and on_domain("https://blog.acme.com", "acme.com")
    assert not on_domain("https://acme.com.evil.io", "acme.com") and not on_domain("https://notacme.com", "acme.com")
    dd = did([0.2, 0.3, 0.25], [0.0, 0.05, -0.05])
    assert abs(dd["diff"] - 0.25) < 1e-9 and dd["lo"] > 0
    assert extract({"engine": "gemini", "data": {"answerText": "A http://googleusercontent.com/lmdx_content/X B"}})["text"] == "A   B"
    own = brand_rx({"name": "Acme"})
    assert classify("hubspot vs pipedrive", own) == "comparison"
    assert classify("alternatives to hubspot", own) == "alternatives"
    assert classify("crm pricing for startups", own) == "pricing"
    assert classify("is acme legit", own) == "reputation"
    assert classify("how to track sales leads", own) == "how-to"
    ents = ent_candidates("1. **Attio** — modern\n2. **Best for:** teams\n- Folk: simple\n### 3. Zoho CRM\n**Pricing**")
    assert {"Attio", "Folk", "Zoho CRM"} <= ents and "Pricing" not in ents and "Best for" not in ents, ents
    noise = ent_candidates("- **Professional**: $49\n- **Power dialing** — yes\n**Onboarding Fees**\n**15% to 25%**\n1. **Monday CRM** — ok")
    assert noise == {"Monday CRM"}, noise
    assert tidy("**Pipedrive** | $14 | | HubSpot |") == "Pipedrive · $14 · HubSpot", tidy("**Pipedrive** | $14 | | HubSpot |")
    assert locale_of("https://x.com/sitemaps/pt-blog-pages.xml") == "pt" and locale_of("https://x.com/zh-hant/a") == "zh"
    assert locale_of("https://x.com/blog/post") is None and locale_of("https://x.com/en/crm") == "en"
    with tempfile.TemporaryDirectory() as t:
        open(os.path.join(t, "c.json"), "w").write(json.dumps({**TEMPLATE, "prompts": []}))
        with zipfile.ZipFile(os.path.join(t, "gsc.zip"), "w") as z:
            z.writestr("Queries.csv", "Top queries,Clicks,Impressions,CTR,Position\n"
                       "best crm for startups,10,\"1,200\",1%,8.2\nbest crms for startups,1,50,2%,9\n"
                       "yourbrand login,90,900,10%,1\nhow do i track sales leads,3,300,1%,14\ncrm,5,9000,0%,40\n")
            z.writestr("Pages.csv", "Top pages,Clicks\nhttps://x,1\n")
        cfg = load_cfg(os.path.join(t, "c.json"))
        cmd_seed(cfg, [(os.path.join(t, "gsc.zip"), "gsc")], 50)
        out = json.load(open(os.path.join(t, "prompt_candidates.json")))
        texts = [p["text"] for p in out["prompts"]]
        assert "best crm for startups" in texts and "best crms for startups" not in texts, texts  # deduped
        assert "crm" not in texts and out["reputation"][0]["text"] == "yourbrand login"
        assert next(p for p in out["prompts"] if p["text"] == "best crm for startups")["impressions"] == 1200
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("init").add_argument("dir")
    p = sp.add_parser("seed"); p.add_argument("config"); p.add_argument("--max", type=int, default=100)
    for flag in ("gsc", "bing", "bing-ai", "csv"):
        p.add_argument(f"--{flag}", action="append", default=[], help=f"{flag} export (.csv/.tsv, or GSC .zip)")
    p = sp.add_parser("plan"); p.add_argument("config"); p.add_argument("--icc", type=float, default=0.3)
    p = sp.add_parser("run"); p.add_argument("config"); p.add_argument("--wave", required=True); p.add_argument("--yes", action="store_true")
    p = sp.add_parser("report"); p.add_argument("config"); p.add_argument("--wave", required=True)
    p = sp.add_parser("pages"); p.add_argument("config"); p.add_argument("--wave", required=True)
    p.add_argument("--top", type=int, default=25); p.add_argument("--yes", action="store_true")
    p = sp.add_parser("diff"); p.add_argument("config"); p.add_argument("--a", required=True); p.add_argument("--b", required=True)
    p = sp.add_parser("trend"); p.add_argument("config")
    p = sp.add_parser("dashboard"); p.add_argument("config")
    sp.add_parser("selftest")
    a = ap.parse_args()
    if a.cmd == "init":
        os.makedirs(a.dir, exist_ok=True)
        path = os.path.join(a.dir, "config.json")
        if os.path.exists(path):
            sys.exit(f"{path} exists")
        json.dump(TEMPLATE, open(path, "w"), indent=2)
        print(f"wrote {path}")
        return
    if a.cmd == "selftest":
        return selftest()
    cfg = load_cfg(a.config)
    if a.cmd == "seed":
        files = [(f, s) for s in ("gsc", "bing", "bing-ai", "csv") for f in getattr(a, s.replace("-", "_"))]
        if not files:
            sys.exit("give at least one --gsc / --bing / --bing-ai / --csv file")
        cmd_seed(cfg, files, a.max)
    elif a.cmd == "plan":
        cmd_plan(cfg, a.icc)
    elif a.cmd == "run":
        cmd_run(cfg, a.wave, a.yes)
    elif a.cmd == "report":
        cmd_report(cfg, a.wave)
    elif a.cmd == "pages":
        cmd_pages(cfg, a.wave, a.top, a.yes)
    elif a.cmd == "diff":
        cmd_diff(cfg, a.a, a.b)
    elif a.cmd == "trend":
        cmd_trend(cfg)
    elif a.cmd == "dashboard":
        cmd_dashboard(cfg)


if __name__ == "__main__":
    main()
