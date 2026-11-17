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

# Never show satire or fabricated-news outlets
SATIRE = ["the onion", "theonion", "babylon bee", "babylonbee", "clickhole", "beaverton", "waterford whispers", "daily mash",
          "thedailymash", "newsthump", "duffel blog", "duffelblog", "reductress", "the hard times", "thehardtimes",
          "points in case", "national report", "world news daily report", "worldnewsdailyreport", "empire news",
          "the shovel", "betoota", "der postillon", "borowitz", "huzlers", "react365", "newsbiscuit", "the spoof", "thespoof",
          "weekly world news", "satire", "parody"]


def is_satire(publisher, url):
    blob = f"{publisher} {url}".lower()
    return any(x in blob for x in SATIRE)



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


def image_of(el):
    """Largest image in media:content / media:thumbnail / enclosure, or ''."""
    best, best_w = "", -1
    for c in el.iter():
        name, url = localname(c.tag), c.get("url") or ""
        kind = (c.get("medium") or c.get("type") or "").lower()
        if name not in ("content", "thumbnail", "enclosure") or not url.startswith("https://"):
            continue
        if name != "thumbnail" and "image" not in kind and not re.search(r"\.(jpe?g|png|webp)(\?|$)", url, re.I):
            continue
        w = int(c.get("width") or 0)
        if w > best_w:
            best, best_w = url, w
    return best


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
        if not title or not link.startswith(("http://", "https://")) or is_satire(src, link):
            continue
        out.append({
            "src": src, "sec": sec, "title": title, "link": link,
            "desc": clean(child_text(el, "description", "summary", "encoded", "content"), 220),
            "date": parse_date(child_text(el, "pubDate", "date", "published", "updated")),
            "img": image_of(el),
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


def story_html(lead, others=(), kind="", img=""):
    """kind: "lead" (front-page lead), "rail", "brief", "" (section column story)."""
    t = ago_attr(lead["date"])
    also = ""
    if others:
        links = " · ".join(f'<a href="{e(o["link"])}" rel="noopener">{e(o["src"])}</a>' for o in others[:6])
        also = f'<div class="also">Also reported by {links}</div>'
    pic = (f'<figure><img src="{e(img)}" alt="" loading="lazy" referrerpolicy="no-referrer"></figure>' if img else "")
    deck = f'<p>{e(lead["desc"])}</p>' if lead["desc"] and lead["desc"] != lead["title"] and kind != "brief" else ""
    return (f'<article class="story {kind}">{pic}'
            f'<h3><a href="{e(lead["link"])}" rel="noopener">{e(lead["title"])}</a></h3>{deck}'
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

    top_html = {"lead": [], "rail": [], "brief": []}
    for i, (_, _, g) in enumerate(top):
        lead, others, used = g[0], [], {g[0]["src"]}
        for x in g[1:]:
            if x["src"] not in used:
                used.add(x["src"])
                others.append(x)
        kind = "lead" if i == 0 else "rail" if i <= 3 else "brief"
        img = next((x["img"] for x in g if x["img"]), "") if kind == "lead" else ""
        top_html[kind].append(story_html(lead, others, kind, img))

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
        if not picked:
            continue
        first = next((x for x in picked if x["img"]), picked[0])
        rest = [x for x in picked if x is not first]
        sec_html.append(f'<section class="sec" id="{key}"><h2><span>{label}</span></h2><div class="cols">'
                        + story_html(first, kind="sec-lead", img=first["img"])
                        + "".join(story_html(x) for x in rest) + "</div></section>")

    sources = sorted({s for s, _, _ in FEEDS})
    page = TEMPLATE.replace("{{UPDATED}}", ago_attr(now)) \
        .replace("{{LEAD}}", "".join(top_html["lead"])) \
        .replace("{{RAIL}}", "".join(top_html["rail"])) \
        .replace("{{BRIEFS}}", "".join(top_html["brief"])) \
        .replace("{{NSRC}}", str(len({s for s, _, _ in FEEDS}))) \
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
<link href="https://fonts.googleapis.com/css2?family=UnifrakturMaguntia&family=Playfair+Display:ital,wght@0,700;0,900;1,700&family=Libre+Caslon+Text:ital,wght@0,400;0,700;1,400&family=Libre+Franklin:wght@500;700&display=swap" rel="stylesheet">
<style>
:root{--paper:#f4efe4;--ink:#1a1814;--muted:#5e584d;--rule:#1a1814;--hair:#cfc6b4;--accent:#8e1b1b;
  --f-mast:"UnifrakturMaguntia","Old English Text MT",serif;--f-head:"Playfair Display",Georgia,serif;
  --f-body:"Libre Caslon Text",Georgia,serif;--f-label:"Libre Franklin",Arial,sans-serif}
@media (prefers-color-scheme:dark){:root{--paper:#16140f;--ink:#e9e3d6;--muted:#a59d8e;--rule:#e9e3d6;--hair:#3b372f;--accent:#e07a6b}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 var(--f-body)}
a{color:inherit;text-decoration:none}
a:hover{text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:3px}
.wrap{max-width:1240px;margin:0 auto;padding:0 16px}
/* masthead */
.ears{display:flex;justify-content:space-between;gap:12px;padding:14px 0 6px;font:500 .72rem/1.3 var(--f-label);text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.ears div:last-child{text-align:right}
.mast{text-align:center;font:400 clamp(2.6rem,9vw,5.6rem)/1 var(--f-mast);margin:4px 0 8px;letter-spacing:.01em}
.dateline{display:flex;justify-content:space-between;gap:8px 16px;flex-wrap:wrap;border-top:3px double var(--rule);border-bottom:1px solid var(--rule);padding:6px 0;font:700 .74rem var(--f-label);text-transform:uppercase;letter-spacing:.1em}
nav{display:flex;justify-content:center;gap:4px 22px;flex-wrap:wrap;padding:8px 0;border-bottom:3px double var(--rule);font:500 .8rem var(--f-label);text-transform:uppercase;letter-spacing:.12em}
main{padding:20px 0 48px}
/* stories */
.story h3{font:700 1.1rem/1.2 var(--f-head);margin:0 0 6px}
.story p{margin:0 0 6px;font-size:.93rem;color:var(--ink);hyphens:auto}
.meta{display:flex;gap:10px;font:500 .7rem var(--f-label);text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.src{color:var(--accent);font-weight:700}
.also{margin-top:6px;font:italic .8rem var(--f-body);color:var(--muted)}
.also a{color:var(--ink);font-style:normal;font-weight:700}
figure{margin:0 0 10px}
figure img{width:100%;display:block;filter:grayscale(.15) contrast(1.02);border:1px solid var(--hair)}
/* front page */
.front{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr);gap:0;border-bottom:1px solid var(--rule);padding-bottom:18px}
.front>.leadcol{padding-right:22px;border-right:1px solid var(--hair)}
.front>.rail{padding-left:22px}
.story.lead h3{font:900 clamp(1.8rem,3.6vw,2.9rem)/1.06 var(--f-head);letter-spacing:-.01em;margin:4px 0 10px}
.story.lead p{font-size:1.08rem;line-height:1.6}
.story.lead p::first-letter{float:left;font:700 3.3rem/.82 var(--f-head);padding:4px 6px 0 0}
.story.lead figure img{aspect-ratio:16/9;object-fit:cover}
.rail .story{padding:0 0 14px;margin-bottom:14px;border-bottom:1px solid var(--hair)}
.rail .story:last-child{border-bottom:0;margin-bottom:0}
.rail .story h3{font-size:1.32rem;line-height:1.15}
.briefs{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border-bottom:3px double var(--rule);margin-bottom:28px}
.briefs .story{padding:14px 16px 16px;border-left:1px solid var(--hair)}
.briefs .story:nth-child(4n+1){border-left:0;padding-left:0}
.briefs .story h3{font-size:1rem}
/* sections */
.sec{margin-bottom:30px}
.sec h2{display:flex;align-items:center;gap:14px;margin:0 0 14px;font:900 1.5rem var(--f-head);text-transform:uppercase;letter-spacing:.06em}
.sec h2::before,.sec h2::after{content:"";flex:1;border-top:1px solid var(--rule);border-bottom:1px solid var(--rule);height:4px}
.cols{column-count:3;column-gap:32px;column-rule:1px solid var(--hair)}
.cols .story{break-inside:avoid;padding:0 0 12px;margin-bottom:12px;border-bottom:1px solid var(--hair)}
.story.sec-lead h3{font-size:1.45rem;line-height:1.12}
.cols .story p{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.cols .story.sec-lead p{-webkit-line-clamp:5}
footer{border-top:3px double var(--rule);padding:14px 0 40px;color:var(--muted);font:italic .85rem var(--f-body);text-align:center}
@media (max-width:900px){
  .front{grid-template-columns:1fr}.front>.leadcol{padding-right:0;border-right:0;padding-bottom:16px;border-bottom:1px solid var(--hair);margin-bottom:16px}.front>.rail{padding-left:0}
  .briefs{grid-template-columns:repeat(2,minmax(0,1fr))}.briefs .story:nth-child(4n+1){padding-left:16px;border-left:1px solid var(--hair)}.briefs .story:nth-child(2n+1){border-left:0;padding-left:0}
  .cols{column-count:2}}
@media (max-width:600px){.briefs{grid-template-columns:1fr}.briefs .story{border-left:0!important;padding:12px 0!important;border-bottom:1px solid var(--hair)}.cols{column-count:1}.ears div:nth-child(2){display:none}}
</style>
</head>
<body>
<header class="wrap">
  <div class="ears"><div>Top headlines from<br>{{NSRC}} newsrooms</div><div>“All the news that<br>fits in two hours”</div></div>
  <h1 class="mast">Castafero News</h1>
  <div class="dateline"><span id="today"></span><span>{{COUNT}} stories · last 36 hours</span><span>Updated <time id="upd" datetime="{{UPDATED}}">{{UPDATED}}</time></span></div>
  <nav><a href="#top">Front Page</a>{{NAV}}</nav>
</header>
<main class="wrap">
  <section id="top">
    <div class="front"><div class="leadcol">{{LEAD}}</div><div class="rail">{{RAIL}}</div></div>
    <div class="briefs">{{BRIEFS}}</div>
  </section>
  {{SECTIONS}}
</main>
<footer><div class="wrap">Headlines, summaries and photos come from the publishers' RSS feeds ({{SOURCES}}). Every link goes to the original article. The front page leads with the stories the most outlets are covering. Refreshed every two hours.</div></footer>
<script>
(function(){
  function ago(d){var m=Math.round((Date.now()-d)/60000);if(m<1)return"just now";if(m<60)return m+" min ago";var h=Math.round(m/60);return h<24?h+"h ago":Math.round(h/24)+"d ago"}
  document.querySelectorAll(".meta time").forEach(function(t){var d=new Date(t.getAttribute("datetime"));if(!isNaN(d)){t.textContent=ago(d);t.title=d.toLocaleString()}});
  var u=document.getElementById("upd"),d=new Date(u.getAttribute("datetime"));
  if(!isNaN(d)){u.textContent=d.toLocaleTimeString([],{hour:"numeric",minute:"2-digit",timeZoneName:"short"});
    document.getElementById("today").textContent=d.toLocaleDateString([],{weekday:"long",month:"long",day:"numeric",year:"numeric"})}
  document.querySelectorAll("figure img").forEach(function(i){i.onerror=function(){i.closest("figure").remove()}});
  setTimeout(function(){location.reload()},30*60*1000);
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "index.html")
