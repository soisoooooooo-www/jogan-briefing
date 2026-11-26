#!/usr/bin/env python3
"""
조간 브리핑 — 미국·한국 주요 일간지 헤드라인 & 요약 앱

실행:  python3 news_brief.py            → 맥에서 http://localhost:8765
      python3 news_brief.py --build site → site/ 폴더에 정적 웹페이지 생성(GitHub Pages용)

외부 라이브러리 없이 파이썬 3.8+ 표준 라이브러리만 사용합니다.
(선택) GEMINI_API_KEY 환경변수를 설정하면 제미나이가
  - 미국 기사 제목을 한국어로 옮기고
  - 기사별 한 줄 요약
  - 오늘의 종합 브리핑(한·미 주요 이슈)
을 만들어 줍니다. 키가 없으면 각 신문사 RSS의 요약문을 그대로 보여줍니다.
"""

import html
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("NEWS_PORT", "8765"))
PER_PAPER = int(os.environ.get("NEWS_PER_PAPER", "8"))
CACHE_SEC = 600
MAX_AGE_HOURS = 72          # 피드의 최신 기사가 이보다 오래되면 '죽은 피드'로 보고 다음 후보로
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
# 키는 코드에 넣지 않습니다. GitHub에서는 Secrets(GEMINI_API_KEY), 맥에서는 환경변수
# 또는 이 파일 옆의 .gemini_key 파일(한 줄)에서 읽습니다.
def _read_key():
    k = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not k:
        f = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".gemini_key")
        if os.path.exists(f):
            k = open(f, encoding="utf-8").read()
    return (k or "").strip()


API_KEY = _read_key()


def make_ssl_context():
    """macOS의 python.org 파이썬은 인증서 묶음이 없어 모든 HTTPS가 실패하곤 함 → 쓸 수 있는 인증서를 찾아 연결."""
    ctx = ssl.create_default_context()
    try:
        import certifi  # 설치돼 있으면 사용
        ctx.load_verify_locations(certifi.where())
        return ctx
    except Exception:
        pass
    for path in ("/etc/ssl/cert.pem", "/opt/homebrew/etc/openssl@3/cert.pem", "/usr/local/etc/openssl@3/cert.pem",
                 "/etc/ssl/certs/ca-certificates.crt"):
        if os.path.exists(path):
            try:
                ctx.load_verify_locations(path)
            except Exception:
                pass
    return ctx


SSL_CTX = make_ssl_context()


def describe(e):
    """'URLError' 대신 실제 원인을 사람이 읽을 수 있게."""
    reason = getattr(e, "reason", None)
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    text = str(reason or e)
    if "CERTIFICATE_VERIFY_FAILED" in text:
        return "SSL 인증서 확인 실패"
    if "nodename nor servname" in text or "Name or service not known" in text or "getaddrinfo" in text:
        return "주소를 찾을 수 없음(인터넷/DNS)"
    if "timed out" in text:
        return "시간 초과"
    if "refused" in text:
        return "연결 거부"
    return text[:80]
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def gnews(site, lang):
    """구글 뉴스 RSS에서 특정 신문사 사이트의 최근 1일 기사 — 공식 피드가 없을 때의 대안."""
    if lang == "ko":
        q, tail = f"site:{site} when:1d", "hl=ko&gl=KR&ceid=KR:ko"
    else:
        q, tail = f"site:{site} when:1d", "hl=en-US&gl=US&ceid=US:en"
    return f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&{tail}"


# 신문사별 후보 피드: 앞에서부터 시도하고, 실패하거나 오래된 피드면 다음 후보로 넘어갑니다.
PAPERS = {
    "us": [
        ("The New York Times", ["https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml", gnews("nytimes.com", "en")]),
        ("The Wall Street Journal", ["https://feeds.content.dowjones.io/public/rss/RSSWorldNews",
                                     "https://feeds.a.dj.com/rss/RSSWorldNews.xml", gnews("wsj.com", "en")]),
        ("The Washington Post", ["https://feeds.washingtonpost.com/rss/national", gnews("washingtonpost.com", "en")]),
        ("USA Today", [gnews("usatoday.com", "en")]),
        ("Los Angeles Times", ["https://www.latimes.com/world-nation/rss2.0.xml", gnews("latimes.com", "en")]),
    ],
    "kr": [
        ("조선일보", ["https://www.chosun.com/arc/outboundfeeds/rss/?outputType=xml", gnews("chosun.com", "ko")]),
        ("중앙일보", ["https://rss.joins.com/joins_news_list.xml", gnews("joongang.co.kr", "ko")]),
        ("동아일보", ["https://rss.donga.com/total.xml", gnews("donga.com", "ko")]),
        ("한겨레", ["https://www.hani.co.kr/rss/", gnews("hani.co.kr", "ko")]),
        ("경향신문", ["https://www.khan.co.kr/rss/rssdata/total_news.xml", gnews("khan.co.kr", "ko")]),
        ("한국일보", [gnews("hankookilbo.com", "ko")]),
        ("매일경제", ["https://www.mk.co.kr/rss/30000001/", gnews("mk.co.kr", "ko")]),
    ],
}

# ───────────────────────── 피드 수집 ─────────────────────────

TAG_RE = re.compile(r"<[^>]+>")


def clean(text, limit=260):
    if not text:
        return ""
    t = html.unescape(TAG_RE.sub(" ", html.unescape(text)))
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= limit else t[:limit].rsplit(" ", 1)[0] + "…"


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        d = None
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
            try:
                d = datetime.strptime(s.replace("+09:00", "+0900"), fmt)
                break
            except Exception:
                pass
    if d and d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_feed(data, is_gnews):
    root = ET.fromstring(data)
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        f = {}
        for c in el:
            name = local(c.tag)
            if name == "link" and c.get("href"):
                f.setdefault("link", c.get("href"))
            elif name not in f:
                f[name] = (c.text or "").strip()
        title = clean(f.get("title"), 300)
        if not title:
            continue
        desc = f.get("description") or f.get("summary") or f.get("encoded") or f.get("content") or ""
        if is_gnews:
            title = re.sub(r"\s+-\s+[^-]+$", "", title)  # "제목 - 언론사" 꼬리 제거
            desc = ""  # 구글 뉴스 설명은 링크 목록이라 쓸모없음
        d = parse_date(f.get("pubDate") or f.get("published") or f.get("updated") or f.get("date"))
        items.append({"title": title, "summary": clean(desc), "link": f.get("link", ""),
                      "ts": d.timestamp() if d else None})
    return items


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=12, context=SSL_CTX) as r:
        return r.read()


def fetch_paper(name, urls):
    errors = []
    for url in urls:
        is_g = "news.google.com" in url
        try:
            items = parse_feed(fetch(url), is_g)
            if not items:
                errors.append("기사 없음")
                continue
            newest = max((i["ts"] for i in items if i["ts"]), default=None)
            if newest and time.time() - newest > MAX_AGE_HOURS * 3600:
                errors.append("오래된 피드")
                continue
            seen, out = set(), []
            for it in items:
                key = it["title"][:40]
                if key not in seen:
                    seen.add(key)
                    out.append(it)
            if is_g:  # 구글 뉴스 검색 결과는 최신순으로
                out.sort(key=lambda i: i["ts"] or 0, reverse=True)
            return {"paper": name, "via": "Google News" if is_g else "공식 RSS",
                    "items": out[:PER_PAPER], "error": None}
        except Exception as e:
            errors.append(describe(e))
    return {"paper": name, "via": None, "items": [], "error": " / ".join(errors) or "실패"}


# ───────────────────────── 제미나이 요약(선택) ─────────────────────────

MODEL_CHAIN = [MODEL] + [m for m in ("gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.8-flash") if m != MODEL]
GEMINI_ENDPOINTS = [
    # Google AI Studio 키
    "https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
    # Vertex AI 익스프레스 모드 키 — 위 주소가 '키가 안 맞는다'고 할 때만 시도
    "https://aiplatform.googleapis.com/v1/publishers/google/models/{m}:generateContent",
]
BUSY = (429, 500, 502, 503, 504)   # 서버 혼잡·일시 오류 → 잠시 후 재시도, 그래도 안 되면 다른 모델
_good_endpoint = None


def _call(url, body):
    req = urllib.request.Request(url, data=body, headers={"x-goog-api-key": API_KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180, context=SSL_CTX) as r:
            return json.loads(r.read()), None, None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail)["error"]["message"]
        except Exception:
            pass
        return None, e.code, detail[:120]


def gemini(prompt):
    global _good_endpoint
    body = json.dumps({"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                       "generationConfig": {"responseMimeType": "application/json"}}).encode()
    last = ""
    res = None
    for model in MODEL_CHAIN:
        endpoints = [_good_endpoint] if _good_endpoint else GEMINI_ENDPOINTS
        for ep in endpoints:
            for wait in (0, 8, 20):          # 혼잡하면 최대 두 번 더 기다렸다 재시도
                if wait:
                    time.sleep(wait)
                res, code, detail = _call(ep.format(m=model), body)
                if res is not None:
                    _good_endpoint = ep
                    break
                last = f"{model}: HTTP {code} — {detail}"
                print("  제미나이 재시도 사유:", last)
                if code not in BUSY:
                    break
            if res is not None or code in BUSY or code == 404:
                break                        # 혼잡/모델 없음이면 다른 주소 말고 다음 모델로
        if res is not None:
            break
        if code not in BUSY and code != 404:
            break                            # 키 문제 등은 모델을 바꿔도 소용없음
    if res is None:
        raise RuntimeError(last)
    parts = (res.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def ai_enrich(snapshot):
    lines = []
    for region in ("us", "kr"):
        for p in snapshot[region]:
            for it in p["items"]:
                lines.append(f'[{it["id"]}] ({region.upper()} · {p["paper"]}) {it["title"]} || {it["summary"]}')
    prompt = (
        "아래는 오늘 미국(US)과 한국(KR) 주요 일간지의 헤드라인 목록입니다. 형식: [id] (지역 · 신문) 제목 || RSS 요약\n\n"
        + "\n".join(lines)
        + "\n\n다음 JSON만 출력하세요(코드블록·설명 없이):\n"
        '{"briefing":{"us":[{"headline":"...","detail":"..."}],"kr":[{"headline":"...","detail":"..."}]},'
        '"items":{"<id>":{"ko_title":"...","summary":"..."}}}\n'
        "규칙: briefing은 지역별로 여러 신문이 함께 다룬 핵심 이슈 3~5개, headline은 한 줄, detail은 1~2문장이며 "
        "어느 신문들이 다뤘는지 괄호로 표기. items에는 모든 id를 포함하고 ko_title은 한국어 제목(한국 기사는 원제 유지), "
        "summary는 제공된 정보만으로 쓴 한국어 한 문장 요약(정보가 부족하면 제목을 풀어 쓰되 추측 금지). "
        "언론사 간 논조 차이가 뚜렷하면 detail에 짧게 언급."
    )
    text = gemini(prompt)
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0))


# ───────────────────────── 상태 & 캐시 ─────────────────────────

STATE = {"data": None, "at": 0, "ai": None, "ai_error": None, "busy": False}
LOCK = threading.Lock()


def collect():
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {r: [ex.submit(fetch_paper, n, u) for n, u in PAPERS[r]] for r in PAPERS}
        snap = {r: [f.result() for f in futs[r]] for r in futs}
    n = 0
    for r in snap:
        for p in snap[r]:
            for it in p["items"]:
                n += 1
                it["id"] = f"a{n}"
    return snap


def refresh(force=False):
    with LOCK:
        if STATE["busy"] or (not force and STATE["data"] and time.time() - STATE["at"] < CACHE_SEC):
            return
        STATE["busy"] = True
    try:
        snap = collect()
        with LOCK:
            STATE.update(data=snap, at=time.time(), ai=None, ai_error=None)
        if API_KEY:
            try:
                ai = ai_enrich(snap)
                with LOCK:
                    STATE["ai"] = ai
            except Exception as e:
                with LOCK:
                    STATE["ai_error"] = f"AI 요약 실패: {describe(e) if isinstance(e, urllib.error.URLError) else e}"
    finally:
        with LOCK:
            STATE["busy"] = False


# ───────────────────────── 웹 화면 ─────────────────────────

PAGE = r"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>조간 브리핑</title>
<link rel="manifest" href="manifest.webmanifest"><link rel="apple-touch-icon" href="icon-180.png">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="조간 브리핑">
<meta name="theme-color" content="#111110">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Serif+KR:wght@700;900&family=Noto+Sans+KR:wght@400;500;700&display=swap">
<style>
:root{color-scheme:light dark;--bg:light-dark(#f7f5f0,#111110);--paper:light-dark(#fff,#1b1a18);--ink:light-dark(#151412,#ece8e1);
--mute:light-dark(#6b665e,#a29c92);--rule:light-dark(#151412,#ece8e1);--hair:light-dark(#dcd7cd,#34312c);--acc:#c8102e}
*{box-sizing:border-box;min-width:0}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 "Noto Sans KR",system-ui,sans-serif}
a{color:inherit;text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1240px;margin:0 auto;padding:0 max(4vw,16px)}
header{border-bottom:4px double var(--rule);padding:28px 0 14px;text-align:center}
h1{font:900 clamp(34px,6vw,58px)/1.05 "Noto Serif KR",serif;margin:0;letter-spacing:-.02em}
.date{color:var(--mute);font-size:14px;margin-top:8px}
.bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:14px 0;border-bottom:1px solid var(--hair)}
.tabs{display:flex;gap:4px}.tab{font:700 15px "Noto Sans KR";padding:10px 16px;border:1px solid var(--rule);background:transparent;color:var(--ink);cursor:pointer;min-height:44px}
.tab[aria-pressed=true]{background:var(--ink);color:var(--bg)}
input{flex:1;min-width:160px;font:16px "Noto Sans KR";padding:10px 12px;border:1px solid var(--hair);background:var(--paper);color:var(--ink);min-height:44px}
button.go{font:700 15px "Noto Sans KR";padding:10px 16px;border:0;background:var(--acc);color:#fff;cursor:pointer;min-height:44px}
.status{color:var(--mute);font-size:13px;width:100%}
.brief{margin:22px 0;padding:20px 22px;background:var(--paper);border-top:3px solid var(--acc)}
.brief h2{font:900 22px "Noto Serif KR",serif;margin:0 0 10px}
.cols2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:22px}
.brief h3{font-size:14px;color:var(--acc);margin:0 0 6px}
.brief ol{margin:0;padding-left:20px}.brief li{margin:6px 0}.brief b{display:block}.brief span{color:var(--mute);font-size:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,360px),1fr));gap:0 28px;padding-bottom:40px}
.paper{padding:18px 0;border-top:2px solid var(--rule)}
.ph{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin-bottom:6px}
.ph h2{font:900 21px "Noto Serif KR",serif;margin:0}.via{font-size:12px;color:var(--mute);white-space:nowrap}
.it{padding:10px 0;border-bottom:1px solid var(--hair)}.it:last-child{border:0}
.it a.t{font-weight:700;font-size:16px;line-height:1.4;display:block}
.it .o{font-size:13px;color:var(--mute);margin-top:2px}
.it p{margin:4px 0 0;font-size:14px;color:var(--mute)}
.it time{font-size:12px;color:var(--mute)}
.it:first-of-type a.t{font:900 19px/1.35 "Noto Serif KR",serif}
.err{color:var(--acc);font-size:14px}
.hide{display:none}
</style></head><body>
<div class="wrap">
<header><h1>조간 브리핑</h1><div class="date" id="date"></div></header>
<div class="bar">
 <div class="tabs"><button class="tab" data-r="all" aria-pressed="true">전체</button><button class="tab" data-r="us" aria-pressed="false">미국</button><button class="tab" data-r="kr" aria-pressed="false">한국</button></div>
 <input id="q" placeholder="키워드로 거르기 (예: 관세, AI, Trump)" autocomplete="off">
 <button class="go" id="re">새로 고침</button>
 <div class="status" id="st">불러오는 중…</div>
</div>
<section class="brief hide" id="brief"></section>
<main class="grid" id="grid"></main>
</div>
<script>
{
const $=s=>document.querySelector(s), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let D=null, region='all';
$('#date').textContent=new Date().toLocaleDateString('ko-KR',{year:'numeric',month:'long',day:'numeric',weekday:'long'});
const ago=ts=>{if(!ts)return'';const m=Math.round((Date.now()/1000-ts)/60);return m<60?m+'분 전':m<1440?Math.round(m/60)+'시간 전':Math.round(m/1440)+'일 전'};
function render(){
  if(!D||!D.data)return;
  const q=$('#q').value.trim().toLowerCase(), ai=D.ai||{}, AI=ai.items||{};
  const b=ai.briefing;
  if(b && !q){
    const part=(k,t)=>(region==='all'||region===k)&&b[k]?.length?`<div><h3>${t}</h3><ol>${b[k].map(x=>`<li><b>${esc(x.headline)}</b><span>${esc(x.detail)}</span></li>`).join('')}</ol></div>`:'';
    $('#brief').innerHTML=`<h2>오늘의 핵심</h2><div class="cols2">${part('us','미국 신문들이 주목한 것')}${part('kr','한국 신문들이 주목한 것')}</div>`;
    $('#brief').classList.remove('hide');
  } else $('#brief').classList.add('hide');
  const papers=[...(region!=='kr'?D.data.us:[]),...(region!=='us'?D.data.kr:[])];
  $('#grid').innerHTML=papers.map(p=>{
    const items=p.items.filter(it=>{const a=AI[it.id]||{};return !q||[it.title,it.summary,a.ko_title,a.summary].join(' ').toLowerCase().includes(q)});
    if(q&&!items.length)return'';
    const body=p.error?`<div class="err">불러오지 못했어요 (${esc(p.error)})</div>`:items.map(it=>{
      const a=AI[it.id]||{}, ko=a.ko_title&&a.ko_title!==it.title;
      const href=/^https?:\/\//.test(it.link)?esc(it.link):'#';
      return `<div class="it"><a class="t" href="${href}" target="_blank" rel="noopener">${esc(ko?a.ko_title:it.title)}</a>${ko?`<div class="o">${esc(it.title)}</div>`:''}<p>${esc(a.summary||it.summary)}</p><time>${ago(it.ts)}</time></div>`}).join('');
    return `<section class="paper"><div class="ph"><h2>${esc(p.paper)}</h2><span class="via">${esc(p.via||'')}</span></div>${body}</section>`}).join('')||'<p>일치하는 기사가 없어요.</p>';
}
async function load(force){
  if(window.__DATA__){
    D=window.__DATA__;
    const t=new Date(D.at*1000).toLocaleString('ko-KR',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
    let s=`${t} 기준 · 2시간마다 자동 갱신`;
    if(!D.has_key)s+=' · AI 요약 꺼짐(GEMINI_API_KEY 시크릿 확인)'; else if(D.ai_error)s+=' · '+D.ai_error;
    $('#st').textContent=s; render();
    if(force)location.reload();
    return;
  }
  $('#st').textContent=force?'새로 모으는 중… (10~20초)':'불러오는 중…';
  try{
    const r=await fetch('/api/news'+(force?'?refresh=1':''));D=await r.json();
    const t=D.at?new Date(D.at*1000).toLocaleTimeString('ko-KR',{hour:'2-digit',minute:'2-digit'}):'';
    let s=D.busy?'수집/요약 진행 중… 잠시 후 자동으로 갱신돼요.':`${t} 기준`;
    if(!D.has_key)s+=' · GEMINI_API_KEY를 설정하면 한국어 번역·요약·종합 브리핑이 추가돼요.';
    else if(D.ai_error)s+=' · '+D.ai_error; else if(!D.ai&&!D.busy)s+=' · AI 요약 없음';
    $('#st').textContent=s; render();
    if(D.busy)setTimeout(()=>load(false),4000);
  }catch(e){$('#st').textContent='서버에 연결할 수 없어요. 터미널에서 앱이 실행 중인지 확인해 주세요.'}
}
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{region=t.dataset.r;document.querySelectorAll('.tab').forEach(x=>x.setAttribute('aria-pressed',x===t));render()});
$('#q').oninput=render; $('#re').onclick=()=>load(true);
load(false);
}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":
            return self.send(200, PAGE, "text/html; charset=utf-8")
        if u.path == "/api/news":
            force = "refresh=1" in u.query
            if force or not STATE["data"] or time.time() - STATE["at"] > CACHE_SEC:
                if STATE["data"] is None:
                    refresh(force=True)  # 첫 요청은 수집이 끝날 때까지 기다림
                else:
                    threading.Thread(target=refresh, args=(True,), daemon=True).start()
                    time.sleep(0.2)
            with LOCK:
                out = {"data": STATE["data"], "at": STATE["at"], "ai": STATE["ai"], "ai_error": STATE["ai_error"],
                       "busy": STATE["busy"], "has_key": bool(API_KEY)}
            return self.send(200, json.dumps(out, ensure_ascii=False), "application/json; charset=utf-8")
        self.send(404, "not found", "text/plain")


def build(out):
    refresh(force=True)
    with LOCK:
        data = {"data": STATE["data"], "at": STATE["at"], "ai": STATE["ai"], "ai_error": STATE["ai_error"],
                "busy": False, "has_key": bool(API_KEY)}
    os.makedirs(out, exist_ok=True)
    blob = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    page = PAGE.replace("<script>\n{", f"<script>window.__DATA__={blob};</script>\n<script>\n{{", 1)
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    manifest = {"name": "조간 브리핑", "short_name": "조간 브리핑", "start_url": ".", "display": "standalone",
                "background_color": "#111110", "theme_color": "#111110",
                "icons": [{"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
                          {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"}]}
    with open(os.path.join(out, "manifest.webmanifest"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False)
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("icon-180.png", "icon-192.png", "icon-512.png"):
        src = os.path.join(here, name)
        if os.path.exists(src):
            with open(src, "rb") as a, open(os.path.join(out, name), "wb") as b:
                b.write(a.read())
    ok = sum(1 for r in data["data"] for p in data["data"][r] if p["items"])
    print(f"빌드 완료: {out}/index.html  (신문 {ok}곳 수집, AI 요약 {'성공' if data['ai'] else '없음: ' + str(data['ai_error'] or '키 없음')})")


def cli():
    """터미널 출력 모드:  python3 news_brief.py --print"""
    refresh(force=True)
    ai = (STATE["ai"] or {}).get("items", {})
    for region, label in (("us", "미국"), ("kr", "한국")):
        print(f"\n━━━━━━━━ {label} ━━━━━━━━")
        for p in STATE["data"][region]:
            print(f"\n■ {p['paper']}  ({p['via'] or p['error']})")
            for it in p["items"]:
                a = ai.get(it["id"], {})
                print(f"  • {a.get('ko_title') or it['title']}")
                s = a.get("summary") or it["summary"]
                if s:
                    print(f"    {s}")


if __name__ == "__main__":
    if "--build" in sys.argv:
        i = sys.argv.index("--build")
        build(sys.argv[i + 1] if len(sys.argv) > i + 1 else "site")
        sys.exit()
    if "--print" in sys.argv:
        cli()
        sys.exit()
    try:
        fetch("https://www.google.com/")
    except Exception as e:
        print(f"[경고] 인터넷 연결 확인 실패: {describe(e)}")
        if "SSL" in describe(e):
            print("  → 터미널에서  pip3 install certifi  를 실행한 뒤 다시 시작해 보세요.")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"조간 브리핑 실행 중 → {url}  (종료: Ctrl+C)")
    print("AI 요약: " + ("켜짐 (" + MODEL + ")" if API_KEY else "꺼짐 — GEMINI_API_KEY 설정 시 활성화"))
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
