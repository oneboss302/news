#!/usr/bin/env python3
"""Build news.castafero.com: pull publisher RSS feeds, group shared stories, write index.html.

Standard library only, so it runs on a bare GitHub Actions runner.
Usage: python3 build.py [output.html]
"""
import html, json, re, sys, concurrent.futures as cf
import urllib.request, xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

# (source, section, url). Order sets which outlet leads a shared story.
FEEDS = [
    ("BBC", "world", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("NPR", "us", "https://feeds.npr.org/1001/rss.xml"),
    ("New York Times", "world", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"),
    ("New York Times", "us", "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"),
    ("Guardian", "world", "https://www.theguardian.com/world/rss"),
    ("Guardian", "us", "https://www.theguardian.com/us-news/rss"),
    ("BBC", "us", "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml"),
    ("Washington Post", "world", "https://feeds.washingtonpost.com/rss/world"),
    ("Al Jazeera", "world", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("DW", "world", "https://rss.dw.com/rdf/rss-en-all"),
    ("France 24", "world", "https://www.france24.com/en/rss"),
    ("CBS News", "us", "https://www.cbsnews.com/latest/rss/main"),
    ("ABC News", "us", "https://feeds.abcnews.com/abcnews/topstories"),
    ("BBC", "business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("CNBC", "business", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("NPR", "business", "https://feeds.npr.org/1006/rss.xml"),
    ("BBC", "tech", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
    ("Ars Technica", "tech", "https://feeds.arstechnica.com/arstechnica/index"),
    ("The Verge", "tech", "https://www.theverge.com/rss/index.xml"),
    ("BBC", "science", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"),
    ("NPR", "science", "https://feeds.npr.org/1007/rss.xml"),
]
SECTIONS = [("world", "World"), ("us", "U.S."), ("business", "Business"), ("tech", "Technology"), ("science", "Science")]
MAX_AGE = timedelta(hours=36)
UA = "Mozilla/5.0 (compatible; castafero-news/1.0; RSS reader)"

STOP = set("""a an the and or but of to in on at for from by with as is are was were be been being has have had
it its this that these those after before over under into about amid says say said new news live update updates
will would could can may might not no than then up out off what who why how when where his her their they he she
we you our your more most first last two three one year years day week us u.s via vs against over amid""".split())


def localname(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def child_text(el, *names):
    for c in el:
        if localname(c.tag) in names:
            t = (c.text or "").strip()
            if t:
                return t
            if localname(c.tag) == "link" and c.get("href"):
                return c.get("href").strip()
    return ""


def atom_link(el):
    for c in el:
        if localname(c.tag) == "link" and c.get("href") and c.get("rel", "alternate") == "alternate":
            return c.get("href").strip()
    return child_text(el, "link")


def parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def clean(s, limit=None):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(html.unescape(s))
    s = re.sub(r"\s+", " ", s).strip()
    if limit and len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(",;:.") + "…"
    return s


def fetch(feed):
    src, sec, url = feed
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        root = ET.fromstring(r.read())
    out = []
    for el in root.iter():
        if localname(el.tag) not in ("item", "entry"):
            continue
        title = clean(child_text(el, "title"))
        link = atom_link(el)
        if not title or not link.startswith(("http://", "https://")):
            continue
        out.append({
            "src": src, "sec": sec, "title": title, "link": link,
            "desc": clean(child_text(el, "description", "summary", "encoded", "content"), 220),
            "date": parse_date(child_text(el, "pubDate", "date", "published", "updated")),
        })
    return out[:40]


def tokens(title):
    words = re.findall(r"[a-z0-9]+", title.lower().replace("’", "'").replace("'s ", " "))
    return {w[:-1] if len(w) > 4 and w.endswith("s") else w for w in words if len(w) > 2 and w not in STOP}


def make_sim(titles):
    """Similarity of two headlines: shared words weighted by rarity (common words like a
    president's name count for little), relative to the shorter headline."""
    import math
    df = {}
    for t in titles:
        for w in tokens(t):
            df[w] = df.get(w, 0) + 1
    n = len(titles)
    idf = lambda w: math.log((n + 1) / (df.get(w, 0) + 1))

    def sim(a, b):
        shared = a & b
        if len(shared) < 2:
            return 0.0
        wa, wb = sum(map(idf, a)), sum(map(idf, b))
        return sum(map(idf, shared)) / max(1e-9, min(wa, wb))
    return sim


def cluster(items):
    """Leader clustering: each headline joins the first group whose lead headline it matches.
    Comparing only against leads stops A~B~C chains from merging unrelated stories."""
    sim = make_sim([it["title"] for it in items])
    groups = []  # (lead_tokens, members, member_tokens)
    for it in items:
        tk = tokens(it["title"])
        best, best_s = None, 0.0
        for g in groups:
            lead_s = sim(tk, g[0])
            if lead_s < 0.2:
                continue
            s = max(lead_s, max(sim(tk, t) for t in g[2]))
            if s > best_s:
                best, best_s = g, s
        if best is not None and best_s >= 0.45:
            best[1].append(it)
            best[2].append(tk)
        else:
            groups.append((tk, [it], [tk]))
    return [g[1] for g in groups]


def ago_attr(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ") if d else ""


def e(s):
    return html.escape(s or "", quote=True)


def story_html(lead, others=(), big=False):
    t = ago_attr(lead["date"])
    also = ""
    if others:
        links = " · ".join(f'<a href="{e(o["link"])}" rel="noopener">{e(o["src"])}</a>' for o in others[:6])
        also = f'<div class="also">Also covered by {links}</div>'
    return (f'<article class="story{" big" if big else ""}">'
            f'<h3><a href="{e(lead["link"])}" rel="noopener">{e(lead["title"])}</a></h3>'
            f'{f"<p>{e(lead["desc"])}</p>" if lead["desc"] and lead["desc"] != lead["title"] else ""}'
            f'<div class="meta"><span class="src">{e(lead["src"])}</span>'
            f'{f"<time datetime=\"{t}\">{t}</time>" if t else ""}</div>{also}</article>')


def main(out_path):
    now = datetime.now(timezone.utc)
    items, failed = [], []
    with cf.ThreadPoolExecutor(8) as ex:
        futs = {ex.submit(fetch, f): f for f in FEEDS}
        for fu in cf.as_completed(futs):
            f = futs[fu]
            try:
                items += fu.result()
            except Exception as err:  # one bad feed shouldn't sink the page
                failed.append(f"{f[0]} {f[1]}: {err}")
    for msg in failed:
        print("feed failed:", msg, file=sys.stderr)

    order = {(s, sec): i for i, (s, sec, _) in enumerate(FEEDS)}
    seen, uniq = set(), []
    for it in sorted(items, key=lambda x: order[(x["src"], x["sec"])]):
        key = it["link"].split("?")[0]
        if key in seen or (it["date"] and now - it["date"] > MAX_AGE):
            continue
        seen.add(key)
        uniq.append(it)
    if len(uniq) < 40:
        sys.exit(f"only {len(uniq)} stories fetched; keeping the previous page")

    groups = cluster(uniq)
    rank_of = lambda it: order[(it["src"], it["sec"])]
    newest = lambda g: max((x["date"] for x in g if x["date"]), default=now - MAX_AGE)
    top = []
    for g in groups:
        srcs = {x["src"] for x in g}
        if len(srcs) >= 2:
            g.sort(key=rank_of)
            top.append((len(srcs), newest(g), g))
    top.sort(key=lambda t: (t[0], t[1]), reverse=True)
    top = top[:12]
    in_top = {id(x) for _, _, g in top for x in g}

    top_html = []
    for i, (_, _, g) in enumerate(top):
        lead, others, used = g[0], [], {g[0]["src"]}
        for x in g[1:]:
            if x["src"] not in used:
                used.add(x["src"])
                others.append(x)
        top_html.append(story_html(lead, others, big=i < 2))

    sec_html = []
    for key, label in SECTIONS:
        pool = [x for x in uniq if x["sec"] == key and id(x) not in in_top]
        pool.sort(key=lambda x: x["date"] or now - MAX_AGE, reverse=True)
        picked, toks_seen = [], []
        for x in pool:
            tk = tokens(x["title"])
            if any(len(tk & s) >= 2 and len(tk & s) / max(1, min(len(tk), len(s))) >= 0.5 for s in toks_seen):
                continue
            toks_seen.append(tk)
            picked.append(x)
            if len(picked) == 10:
                break
        sec_html.append(f'<section class="sec" id="{key}"><h2>{label}</h2>'
                        + "".join(story_html(x) for x in picked) + "</section>")

    sources = sorted({s for s, _, _ in FEEDS})
    page = TEMPLATE.replace("{{UPDATED}}", ago_attr(now)) \
        .replace("{{TOP}}", "".join(top_html)) \
        .replace("{{SECTIONS}}", "".join(sec_html)) \
        .replace("{{NAV}}", "".join(f'<a href="#{k}">{l}</a>' for k, l in SECTIONS)) \
        .replace("{{SOURCES}}", e(", ".join(sources))) \
        .replace("{{COUNT}}", str(len(uniq)))
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(page)
    print(f"wrote {out_path}: {len(uniq)} stories, {len(top)} top, {len(failed)} feeds failed")


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Castafero News</title>
<meta name="description" content="Top world, U.S., business, tech and science headlines, updated every two hours.">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600;9..144,700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#f7f5f0;--surface:#fffdf8;--ink:#1c1b19;--muted:#6b665d;--line:#e3ded3;--accent:#b4232a;--soft:#f1ece2;
  --f-display:"Fraunces",Georgia,serif;--f-body:"Inter",system-ui,sans-serif}
@media (prefers-color-scheme:dark){:root{--bg:#141412;--surface:#1c1b19;--ink:#ece8df;--muted:#a19b90;--line:#33312c;--accent:#ff6b6b;--soft:#24221f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 var(--f-body)}
a{color:inherit;text-decoration:none}
.wrap{max-width:1180px;margin:0 auto;padding:0 16px}
header{border-bottom:3px double var(--line);padding:20px 0 0}
.mast{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap}
.mast h1{font:700 clamp(2rem,5vw,3rem)/1 var(--f-display);margin:0;letter-spacing:-.01em}
.mast h1 span{color:var(--accent)}
.upd{color:var(--muted);font-size:.88rem}
nav{display:flex;gap:4px;overflow-x:auto;padding:12px 0;margin-top:12px;border-top:1px solid var(--line)}
nav a{padding:6px 12px;border-radius:999px;font-size:.9rem;font-weight:500;white-space:nowrap}
nav a:hover{background:var(--soft)}
main{padding:24px 0 48px}
h2{font:700 1.35rem var(--f-display);margin:0 0 14px;padding-bottom:6px;border-bottom:2px solid var(--ink)}
.top{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,340px),1fr));gap:18px;margin-bottom:36px}
.story{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:16px}
.story h3{font:600 1.05rem/1.3 var(--f-display);margin:0 0 6px}
.story h3 a:hover{text-decoration:underline;text-decoration-color:var(--accent)}
.story.big h3{font-size:1.35rem}
.story p{margin:0 0 10px;color:var(--muted);font-size:.92rem}
.meta{display:flex;gap:10px;font-size:.8rem;color:var(--muted)}
.src{font-weight:600;color:var(--accent);text-transform:uppercase;letter-spacing:.04em}
.also{margin-top:8px;padding-top:8px;border-top:1px dashed var(--line);font-size:.82rem;color:var(--muted)}
.also a{color:var(--ink);font-weight:500}.also a:hover{color:var(--accent)}
.secs{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,340px),1fr));gap:28px}
.sec .story{background:none;border:0;border-bottom:1px solid var(--line);border-radius:0;padding:12px 0}
.sec .story p{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
footer{border-top:1px solid var(--line);padding:20px 0 40px;color:var(--muted);font-size:.82rem}
</style>
</head>
<body>
<header><div class="wrap">
  <div class="mast"><h1>Castafero <span>News</span></h1>
  <div class="upd">Updated <time id="upd" datetime="{{UPDATED}}">{{UPDATED}}</time> · refreshes every 2 hours</div></div>
  <nav><a href="#top">Top stories</a>{{NAV}}</nav>
</div></header>
<main class="wrap">
  <section id="top"><h2>Top stories</h2><div class="top">{{TOP}}</div></section>
  <div class="secs">{{SECTIONS}}</div>
</main>
<footer><div class="wrap">Headlines and summaries come from the publishers' RSS feeds ({{SOURCES}}). Every link goes to the original article. Top stories are the ones the most outlets are covering. {{COUNT}} stories from the last 36 hours.</div></footer>
<script>
(function(){
  function ago(d){var m=Math.round((Date.now()-d)/60000);if(m<1)return"just now";if(m<60)return m+" min ago";var h=Math.round(m/60);return h<24?h+"h ago":Math.round(h/24)+"d ago"}
  document.querySelectorAll(".meta time").forEach(function(t){var d=new Date(t.getAttribute("datetime"));if(!isNaN(d)){t.textContent=ago(d);t.title=d.toLocaleString()}});
  var u=document.getElementById("upd"),d=new Date(u.getAttribute("datetime"));
  if(!isNaN(d))u.textContent=d.toLocaleString([], {weekday:"short",month:"short",day:"numeric",hour:"numeric",minute:"2-digit",timeZoneName:"short"});
  setTimeout(function(){location.reload()},30*60*1000);
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "index.html")
