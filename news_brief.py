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
CANDIDATES = PER_PAPER + 5   # 스포츠·연예를 뺀 뒤에도 PER_PAPER개가 남도록 넉넉히 받아 둠
# 뺄 분야: sports, entertainment (쉼표로 구분, 빈 값이면 아무것도 빼지 않음)
EXCLUDE = {x.strip() for x in os.environ.get("NEWS_EXCLUDE", "sports,entertainment").split(",") if x.strip()}

# 1단계: 주소·분류 태그·매체 표시로 거르기
_PATH_RE = {
    "sports": re.compile(r"/(sports?|baseball|football|soccer|golf|basketball|esports)(/|\.|$)", re.I),
    "entertainment": re.compile(r"/(entertainments?|enter|ent|star|stars|celeb|celebrity|arts|style|movies?|music|"
                                r"television|tv|broadcast|culture/entertainment|showbiz|people)(/|\.|$)", re.I),
}
_CAT_WORDS = {
    "sports": ("sport", "스포츠", "야구", "축구", "골프", "농구", "배구", "スポーツ", "野球", "サッカー"),
    "entertainment": ("entertain", "연예", "celebrit", "movies", "television", "music", "arts", "방송", "가요", "영화", "芸能", "エンタメ"),
}
_TEXT_MARKS = {
    "sports": ("KBO", "K리그", "프로야구", "MLB", "NBA", "NFL", "EPL", "프리미어리그", "챔피언스리그", "스포츠조선", "일간스포츠",
               "스포츠서울", "스포츠경향", "스포츠동아", "Super Bowl", "World Series",
               "プロ野球", "Jリーグ", "大相撲", "スポーツ報知", "日刊スポーツ", "スポニチ", "サンスポ", "高校野球"),
    "entertainment": ("OSEN", "엑스포츠뉴스", "스타뉴스", "텐아시아", "마이데일리", "TV리포트", "스포츠조선", "뉴스엔",
                      "아이돌", "걸그룹", "보이그룹", "컴백", "예능", "드라마", "시청률", "박스오피스", "Box Office", "Grammy", "Oscar",
                      "芸能", "アイドル", "俳優", "女優", "紅白", "ジャニーズ", "朝ドラ"),
}


def rule_section(it):
    link = it.get("link", "")
    cats = " ".join(it.get("cats", [])).lower()
    text = f'{it.get("title", "")} {it.get("summary", "")[:80]}'
    for sec in EXCLUDE:
        if sec in _PATH_RE and _PATH_RE[sec].search(urllib.parse.urlparse(link).path):
            return sec
        if any(w.lower() in cats for w in _CAT_WORDS.get(sec, ())):
            return sec
        if any(w in text for w in _TEXT_MARKS.get(sec, ())):
            return sec
    return None
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
    elif lang == "ja":
        q, tail = f"site:{site} when:1d", "hl=ja&gl=JP&ceid=JP:ja"
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
    "jp": [
        ("요미우리신문", [gnews("yomiuri.co.jp", "ja")]),
        ("아사히신문", ["https://www.asahi.com/rss/asahi/newsheadlines.rdf", gnews("asahi.com", "ja")]),
        ("마이니치신문", ["https://mainichi.jp/rss/etc/mainichi-flash.rss", gnews("mainichi.jp", "ja")]),
        ("니혼게이자이(닛케이)", [gnews("nikkei.com", "ja")]),
        ("산케이신문", [gnews("sankei.com", "ja")]),
        ("NHK", ["https://www3.nhk.or.jp/rss/news/cat0.xml", gnews("www3.nhk.or.jp", "ja")]),
    ],
}

OP_PER_PAPER = int(os.environ.get("NEWS_OP_PER_PAPER", "3"))


def gnews_q(q, lang):
    tail = "hl=ko&gl=KR&ceid=KR:ko" if lang == "ko" else "hl=en-US&gl=US&ceid=US:en"
    return f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&{tail}"


def kr_ed(site):
    return gnews_q(f"intitle:사설 site:{site} when:3d", "ko")


def us_op(path):
    return gnews_q(f"site:{path} when:3d", "en")


def en_q(q):
    return gnews_q(q + " when:3d", "en")


# 사설: 한국 신문은 제목이 [사설]인 글만, 영어 매체는 구독 없이 원문을 읽을 수 있는 곳만(편집위원회 사설 우선)
OPINION = [
    ("USA Today", "en", [en_q("site:usatoday.com/opinion")]),
    ("The Guardian", "en", ["https://www.theguardian.com/profile/editorial/rss",
                            en_q('intitle:"The Guardian view" site:theguardian.com')]),
    ("Korea Herald", "en", [en_q('intitle:Editorial site:koreaherald.com')]),
    ("Korea JoongAng Daily", "en", [en_q("editorial site:koreajoongangdaily.joins.com")]),
    ("조선일보", "kr", ["https://www.chosun.com/arc/outboundfeeds/rss/category/opinion/?outputType=xml", kr_ed("chosun.com")]),
    ("중앙일보", "kr", [kr_ed("joongang.co.kr")]),
    ("동아일보", "kr", ["https://rss.donga.com/total.xml", kr_ed("donga.com")]),
    ("한겨레", "kr", ["https://www.hani.co.kr/rss/opinion/", kr_ed("hani.co.kr")]),
    ("경향신문", "kr", ["https://www.khan.co.kr/rss/rssdata/opinion_news.xml", kr_ed("khan.co.kr")]),
    ("한국일보", "kr", [kr_ed("hankookilbo.com")]),
    ("매일경제", "kr", [kr_ed("mk.co.kr")]),
]
_BOARD = re.compile(r"editorial board|the editors|board of editors|editorial", re.I)
_ED_TITLE = re.compile(r"^\s*(\[editorial\]|editorial\s*[:|]|the guardian view)", re.I)


def is_board(it):
    return bool(_BOARD.search(it.get("author", "")) or "/editorial" in it.get("link", "") or _ED_TITLE.search(it["title"]))


def is_kr_editorial(it):
    return bool(re.match(r"\s*[\[［(（]?\s*사설\s*[\]］)）]", it["title"]))


def us_board_first(items):
    return sorted(items, key=lambda it: 0 if is_board(it) else 1)


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
            elif name in ("category", "subject"):
                f.setdefault("_cats", []).append((c.text or c.get("term") or "").strip())
            elif name in ("creator", "author") and "_author" not in f:
                nm = (c.text or "").strip() or " ".join((x.text or "").strip() for x in c if local(x.tag) == "name")
                f["_author"] = nm
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
                      "ts": d.timestamp() if d else None, "cats": f.get("_cats", []), "author": f.get("_author", "")})
    return items


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=12, context=SSL_CTX) as r:
        return r.read()


def fetch_paper(name, urls, kind="news"):
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
            if kind == "kr_ed":
                picked = [i for i in out if is_kr_editorial(i)][:OP_PER_PAPER]
            elif kind == "en_op":
                picked = us_board_first(out)[:OP_PER_PAPER]
            else:
                picked = [i for i in out if not rule_section(i)][:CANDIDATES]
            if not picked:
                errors.append("사설 없음" if kind != "news" else "기사 없음")
                continue
            for i in picked:
                i["board"] = kind == "en_op" and is_board(i)
            return {"paper": name, "via": "Google News" if is_g else "공식 RSS", "items": picked, "error": None}
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


def gemini(prompt, schema=None):
    global _good_endpoint
    cfg = {"responseMimeType": "application/json", "maxOutputTokens": 32768}
    if schema:
        cfg["responseSchema"] = schema
    body = json.dumps({"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": cfg}).encode()
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


SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "briefing": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "headline": {"type": "STRING"}, "detail": {"type": "STRING"}, "outlook": {"type": "STRING"}},
            "required": ["headline", "detail", "outlook"]}},
        "items": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "id": {"type": "STRING"}, "ko_title": {"type": "STRING"}, "summary": {"type": "STRING"},
            "section": {"type": "STRING", "enum": ["news", "sports", "entertainment"]}},
            "required": ["id", "summary", "section"]}},
    },
    "required": ["briefing", "items"],
}


def _parse_json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    # 마지막 수단: 깨진 답에서 읽을 수 있는 항목만 건지기
    items = []
    for m in re.finditer(r'\{[^{}]*?"id"\s*:\s*"(a\d+)"[^{}]*?\}', text):
        try:
            items.append(json.loads(m.group(0)))
        except Exception:
            sm = re.search(r'"summary"\s*:\s*"(.*?)"\s*[,}]', m.group(0))
            if sm:
                items.append({"id": m.group(1), "summary": sm.group(1)})
    briefing = []
    for m in re.finditer(r'\{\s*"headline"\s*:\s*"(.*?)"\s*,\s*"detail"\s*:\s*"(.*?)"\s*\}', text):
        briefing.append({"headline": m.group(1), "detail": m.group(2)})
    if not items and not briefing:
        raise ValueError("응답을 JSON으로 읽을 수 없음")
    return {"briefing": briefing, "items": items, "_salvaged": True}


def _q(t):
    return (t or "").replace('"', "'").replace("\\", " ")


def _enrich_region(region, papers):
    lines = []
    for p in papers:
        for it in p["items"]:
            lines.append(f'[{it["id"]}] ({p["paper"]}) {_q(it["title"])} || {_q(it["summary"][:160])}')
    if not lines:
        return {"briefing": [], "items": {}}
    where = {"us": "미국", "kr": "한국", "jp": "일본"}[region]
    prompt = (
        f"아래는 오늘 {where} 주요 일간지의 헤드라인 목록입니다. 형식: [id] (신문) 제목 || RSS 요약\n\n"
        + "\n".join(lines)
        + "\n\nJSON으로 답하세요.\n"
        "- briefing: section이 news인 기사들 중 여러 신문이 함께 다룬 핵심 이슈 3~5개. headline 한 줄, detail은 1~2문장이며 끝에 다룬 신문을 괄호로. "
        "논조 차이가 뚜렷하면 짧게 언급.\n"
        "- briefing의 outlook: 이 이슈로 앞으로 일어날 수 있는 일과 기대효과·파급효과를 2~3문장으로. "
        "가능한 전개를 '~할 가능성', '~가 관건'처럼 조건과 불확실성을 드러내 쓰고, 단정하지 말 것. "
        "정치적 쟁점은 특정 입장을 지지하거나 평가하지 말고 각 측이 예상하는 결과를 균형 있게. "
        + ("이 이슈가 한국(경제·산업·외교)에 미칠 수 있는 영향이 있으면 한 문장 포함. " if region != "kr" else "")
        + "기사에 없는 구체적 수치·날짜를 지어내지 말 것.\n"
        "- items: 모든 기사를 빠짐없이, 각각 {id, ko_title, summary, section}.\n"
        "  section은 스포츠(경기·선수·구단·리그) 기사면 sports, 연예·방송·영화·음악·셀럽 기사면 entertainment, 그 밖은 news. "
        "스포츠·연예 산업의 경제·정책 기사(예: 중계권 규제, 구단 매각 세금)는 news.\n"
        + ("  ko_title은 자연스러운 한국어 제목.\n" if region != "kr" else "  ko_title은 빈 문자열로 두세요(원제를 그대로 씀).\n")
        + "  summary는 제공된 정보만으로 쓴 한국어 한 문장(60자 이내), 추측 금지.\n"
        "- 어떤 문자열에도 큰따옴표(\")를 쓰지 말고 필요하면 작은따옴표(')를 쓰세요."
    )
    last = None
    for attempt in range(3):
        schema = SCHEMA if attempt < 2 else None   # 스키마가 거절되는 모델이면 마지막엔 없이
        try:
            data = _parse_json(gemini(prompt, schema))
        except RuntimeError as e:
            if "HTTP 400" in str(e) and schema and "key" not in str(e).lower():
                last = e
                continue
            raise
        except ValueError as e:
            last = e
            print(f"  [{where}] 응답 형식 오류, 재시도: {e}")
            continue
        raw = data.get("items") or []
        if isinstance(raw, dict):
            raw = [dict(v, id=k) for k, v in raw.items() if isinstance(v, dict)]
        items = {}
        for it in raw:
            if isinstance(it, dict) and it.get("id"):
                d = {"summary": it.get("summary", ""), "section": it.get("section") or "news"}
                if region != "kr" and it.get("ko_title"):
                    d["ko_title"] = it["ko_title"]
                items[it["id"]] = d
        return {"briefing": data.get("briefing") or [], "items": items}
    raise RuntimeError(f"{where} 요약 형식 오류: {last}")


OP_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "briefing": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "headline": {"type": "STRING"}, "detail": {"type": "STRING"}, "outlook": {"type": "STRING"}},
            "required": ["headline", "detail", "outlook"]}},
        "items": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "id": {"type": "STRING"}, "ko_title": {"type": "STRING"}, "claim": {"type": "STRING"},
            "summary": {"type": "STRING"},
            "vocab": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "en": {"type": "STRING"}, "ko": {"type": "STRING"}},
                "required": ["en", "ko"]}}},
            "required": ["id", "claim", "summary"]}},
    },
    "required": ["briefing", "items"],
}


def _enrich_opinion(papers):
    lines = []
    for p in papers:
        for it in p["items"]:
            tag = "영어" if p.get("region") == "en" else "한국"
            board = "·편집위원회" if it.get("board") else ""
            lines.append(f'[{it["id"]}] ({tag}·{p["paper"]}{board}) {_q(it["title"])} || {_q(it["summary"][:240])}')
    if not lines:
        return {"briefing": [], "items": {}}
    prompt = (
        "아래는 오늘 한국 주요 일간지와 영어 매체의 사설·오피니언 목록입니다. 형식: [id] (지역·신문) 제목 || 요약\n\n"
        + "\n".join(lines)
        + "\n\nJSON으로 답하세요.\n"
        "- briefing: 둘 이상의 신문이 사설로 함께 다룬 쟁점 1~3개(없으면 빈 배열). headline은 쟁점 이름, "
        "detail은 신문별 입장을 신문 이름과 함께 간결히 비교(어느 쪽이 옳은지 평가하지 말 것), "
        "outlook은 이 쟁점이 앞으로 어떻게 전개될 수 있는지 1~2문장, 가능성의 형태로.\n"
        "- items: 모든 글을 빠짐없이, 각각 {id, ko_title, claim, summary, vocab}.\n"
        "  vocab은 영어 글에만: 제목과 요약에 실제로 나온 영어 표현 중 영어 학습에 가치 있는 단어·숙어 최대 3개. "
        "수능·토익 기본 수준(예: export, access, benefit, investigate)은 빼고 중상급 이상 어휘나 관용 표현, 구동사 위주로. "
        "고유명사 제외, 마땅한 표현이 없으면 개수를 줄여도 됨. en은 원문에 나온 형태 그대로, ko는 이 글의 문맥에 맞는 짧은 뜻. "
        "한국 글은 빈 배열.\n"
        "  ko_title은 영어 글이면 자연스러운 한국어 제목, 한국 글이면 빈 문자열.\n"
        "  claim은 이 글의 핵심 주장 한 문장(글쓴이 관점을 그대로 요약, 예: '~해야 한다고 주장'), "
        "summary는 그 근거 한 문장(60자 이내). 제공된 정보만 사용하고 추측 금지.\n"
        "- 어떤 문자열에도 큰따옴표(\")를 쓰지 말고 필요하면 작은따옴표(')를 쓰세요."
    )
    last = None
    for attempt in range(3):
        schema = OP_SCHEMA if attempt < 2 else None
        try:
            data = _parse_json(gemini(prompt, schema))
        except RuntimeError as e:
            if "HTTP 400" in str(e) and schema and "key" not in str(e).lower():
                last = e
                continue
            raise
        except ValueError as e:
            last = e
            continue
        raw = data.get("items") or []
        if isinstance(raw, dict):
            raw = [dict(v, id=k) for k, v in raw.items() if isinstance(v, dict)]
        items = {}
        for it in raw:
            if isinstance(it, dict) and it.get("id"):
                d = {"summary": it.get("summary", ""), "claim": it.get("claim", "")}
                voc = [v for v in (it.get("vocab") or []) if isinstance(v, dict) and v.get("en") and v.get("ko")][:3]
                if voc:
                    d["vocab"] = [{"en": v["en"], "ko": v["ko"]} for v in voc]
                if it.get("ko_title"):
                    d["ko_title"] = it["ko_title"]
                items[it["id"]] = d
        return {"briefing": data.get("briefing") or [], "items": items}
    raise RuntimeError(f"사설 요약 형식 오류: {last}")


def ai_enrich(snapshot):
    out = {"briefing": {}, "items": {}}
    errors = []
    for region in ("us", "kr", "jp"):
        try:
            r = _enrich_region(region, snapshot[region])
            out["briefing"][region] = r["briefing"]
            for k, v in r["items"].items():
                out.setdefault("sections", {})[k] = v.pop("section", "news")
            out["items"].update(r["items"])
        except Exception as e:
            errors.append(str(e)[:160])
            out["briefing"][region] = []
    try:
        r = _enrich_opinion(snapshot.get("op", []))
        out["briefing"]["op"] = r["briefing"]
        out["items"].update(r["items"])
    except Exception as e:
        errors.append(str(e)[:160])
        out["briefing"]["op"] = []
    if not out["items"] and errors:
        raise RuntimeError(" / ".join(errors))
    if errors:
        out["partial_error"] = " / ".join(errors)
    return out


# ───────────────────────── 상태 & 캐시 ─────────────────────────

STATE = {"data": None, "at": 0, "ai": None, "ai_error": None, "busy": False}
LOCK = threading.Lock()


def collect():
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {r: [ex.submit(fetch_paper, n, u) for n, u in PAPERS[r]] for r in PAPERS}
        futs["op"] = [ex.submit(fetch_paper, n, u, "en_op" if reg == "en" else "kr_ed") for n, reg, u in OPINION]
        snap = {r: [f.result() for f in futs[r]] for r in futs}
        for p, (_, reg, _) in zip(snap["op"], OPINION):
            p["region"] = reg
    n = 0
    for r in snap:
        for p in snap[r]:
            for it in p["items"]:
                n += 1
                it["id"] = f"a{n}"
                it.pop("cats", None)
                it.pop("author", None)
            if not API_KEY and r != "op":
                p["items"] = p["items"][:PER_PAPER]
    return snap


def trim(snap, ai):
    """제미나이가 스포츠·연예로 분류한 기사를 빼고 신문사별 PER_PAPER개로 자름."""
    sec = (ai or {}).get("sections", {})
    for r in ("us", "kr", "jp"):
        for p in snap[r]:
            p["items"] = [i for i in p["items"] if sec.get(i["id"], "news") not in EXCLUDE][:PER_PAPER]


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
                trim(snap, ai)
                with LOCK:
                    STATE["data"] = snap
                    STATE["ai"] = ai
                    if ai.get("partial_error"):
                        STATE["ai_error"] = "일부 요약 실패: " + ai["partial_error"]
            except Exception as e:
                trim(snap, None)
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
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 "Noto Sans KR","Hiragino Sans","Noto Sans JP",system-ui,sans-serif}
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
.brief ol{margin:0;padding-left:20px}.brief li{margin:10px 0}.brief b{display:block}.brief span{color:var(--mute);font-size:14px;display:block}
.brief .ol{margin-top:6px;font-size:14px;line-height:1.55;color:var(--ink);opacity:.88}
.brief .ol em{font-style:normal;font-weight:700;color:var(--acc);font-size:12px;margin-right:6px;white-space:nowrap}
.brief .note{font-size:12px;color:var(--mute);margin:12px 0 0}
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
.it .claim{color:var(--ink);font-size:15px;margin-top:6px}
.it em{font-style:normal;font-weight:700;font-size:12px;color:var(--acc);margin-right:6px}
.voc{list-style:none;margin:6px 0 2px;padding:5px 9px;background:var(--paper);border:1px solid var(--hair);
 display:flex;flex-wrap:wrap;gap:2px 14px;align-items:baseline;font-size:13px;line-height:1.5}
.voc .vh{font-size:11px;font-weight:700;color:var(--acc)}
.voc b{font-family:Georgia,"Noto Serif KR",serif;font-size:14px;margin-right:4px}
.voc span{color:var(--ink)}
.it .tag{display:inline-block;font-size:12px;color:var(--mute);border:1px solid var(--hair);padding:1px 7px;margin-top:5px}
.chips{display:none;gap:6px;overflow-x:auto;position:sticky;top:0;z-index:5;background:var(--bg);padding:10px 0;margin-top:14px;scrollbar-width:none;border-bottom:1px solid var(--hair)}
.chips::-webkit-scrollbar,.grid::-webkit-scrollbar{display:none}
.chip{flex:none;font:500 14px "Noto Sans KR",sans-serif;padding:7px 13px;min-height:36px;border:1px solid var(--hair);border-radius:999px;background:transparent;color:var(--ink);cursor:pointer;white-space:nowrap}
.chip[aria-current=true]{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.count{flex:none;align-self:center;font-size:12px;color:var(--mute);padding:0 4px 0 2px}
@media(max-width:700px){
 .chips{display:flex}
 .grid{position:relative;display:flex;align-items:flex-start;gap:18px;overflow-x:auto;scroll-snap-type:x mandatory;overscroll-behavior-x:contain;
  margin:0 calc(-1*max(4vw,16px));padding:0 max(4vw,16px) 40px;scroll-padding-inline:max(4vw,16px);scrollbar-width:none}
 .paper{flex:0 0 100%;scroll-snap-align:start;scroll-snap-stop:always;border-top:0;padding-top:12px}
 .it a.t{font-size:17px}
 .brief{margin:16px 0 0}
}
</style></head><body>
<div class="wrap">
<header><h1>조간 브리핑</h1><div class="date" id="date"></div></header>
<div class="bar">
 <div class="tabs"><button class="tab" data-r="all" aria-pressed="true">전체</button><button class="tab" data-r="us" aria-pressed="false">미국</button><button class="tab" data-r="kr" aria-pressed="false">한국</button><button class="tab" data-r="jp" aria-pressed="false">일본</button><button class="tab" data-r="op" aria-pressed="false">사설</button></div>
 <input id="q" placeholder="키워드로 거르기 (예: 관세, AI, Trump)" autocomplete="off">
 <button class="go" id="re">새로 고침</button>
 <div class="status" id="st">불러오는 중…</div>
</div>
<section class="brief hide" id="brief"></section>
<nav class="chips" id="chips" aria-label="신문사"></nav>
<main class="grid" id="grid"></main>
</div>
<script>
{
const $=s=>document.querySelector(s), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let D=null, region='all';
$('#date').textContent=new Date().toLocaleDateString('ko-KR',{year:'numeric',month:'long',day:'numeric',weekday:'long'});
const ago=ts=>{if(!ts)return'';const m=Math.round((Date.now()/1000-ts)/60);return m<1?'방금':m<60?m+'분 전':m<1440?Math.round(m/60)+'시간 전':Math.round(m/1440)+'일 전'};
function render(){
  if(!D||!D.data)return;
  const q=$('#q').value.trim().toLowerCase(), ai=D.ai||{}, AI=ai.items||{};
  const b=ai.briefing;
  if(b && !q){
    const part=(k,t)=>((region==='all'&&k!=='op')||region===k)&&b[k]?.length?`<div><h3>${t}</h3><ol>${b[k].map(x=>`<li><b>${esc(x.headline)}</b><span>${esc(x.detail)}</span>${x.outlook?`<div class="ol"><em>전망</em>${esc(x.outlook)}</div>`:''}</li>`).join('')}</ol></div>`:'';
    const parts=region==='op'?part('op','여러 신문이 사설로 다룬 쟁점'):part('us','미국 신문들이 주목한 것')+part('kr','한국 신문들이 주목한 것')+part('jp','일본 신문들이 주목한 것');
    $('#brief').innerHTML=`<h2>${region==='op'?'사설 비교':'오늘의 핵심'}</h2><div class="cols2">${parts}</div><p class="note">전망은 기사 내용을 바탕으로 AI가 추정한 가능성이에요. 실제 전개와 다를 수 있어요.</p>`;
    $('#brief').classList.toggle('hide',!parts);
  } else $('#brief').classList.add('hide');
  const papers=region==='all'?[...D.data.us,...D.data.kr,...(D.data.jp||[])]:(D.data[region]||[]);
  $('#grid').innerHTML=papers.map(p=>{
    const items=p.items.filter(it=>{const a=AI[it.id]||{};return !q||[it.title,it.summary,a.ko_title,a.summary,a.claim,(a.vocab||[]).map(v=>v.en+' '+v.ko).join(' ')].join(' ').toLowerCase().includes(q)});
    if(q&&!items.length)return'';
    const body=p.error?`<div class="err">${region==='op'?'오늘 사설을 찾지 못했어요':'불러오지 못했어요'} (${esc(p.error)})</div>`:items.map(it=>{
      const a=AI[it.id]||{}, ko=a.ko_title&&a.ko_title!==it.title;
      const href=/^https?:\/\//.test(it.link)?esc(it.link):'#';
      return `<div class="it"><a class="t" href="${href}" target="_blank" rel="noopener">${esc(ko?a.ko_title:it.title)}</a>${ko?`<div class="o">${esc(it.title)}</div>`:''}${it.board?'<span class="tag">편집위원회 사설</span>':''}${a.claim?`<p class="claim"><em>주장</em>${esc(a.claim)}</p>`:''}<p>${a.claim&&a.summary?'<em class="why">근거</em>':''}${esc(a.summary||it.summary)}</p>${a.vocab?.length?`<ul class="voc"><li class="vh">핵심 표현</li>${a.vocab.map(v=>`<li><b lang="en">${esc(v.en)}</b><span>${esc(v.ko)}</span></li>`).join('')}</ul>`:''}<time>${ago(it.ts)}</time></div>`}).join('');
    return `<section class="paper" data-p="${esc(p.paper)}"><div class="ph"><h2>${esc(p.paper)}</h2><span class="via">${esc(p.via||'')}</span></div>${body}</section>`}).join('')||'<p>일치하는 기사가 없어요.</p>';
  const cards=[...document.querySelectorAll('.paper')];
  $('#chips').innerHTML=cards.map((c,i)=>`<button class="chip" data-i="${i}" aria-current="${i===0}">${esc(c.dataset.p)}</button>`).join('')+`<span class="count" id="cnt">1/${cards.length}</span>`;
  $('#grid').scrollLeft=0; cur=0;
}
let cur=0, tmr=null;
const cardsNow=()=>[...document.querySelectorAll('.paper')];
const padL=()=>parseFloat(getComputedStyle($('#grid')).paddingLeft)||0;
function mark(i){
  cur=i;
  document.querySelectorAll('.chip').forEach(c=>c.setAttribute('aria-current',String(+c.dataset.i===i)));
  const chip=document.querySelector(`.chip[data-i="${i}"]`); const bar=$('#chips');
  if(chip) bar.scrollTo({left:chip.offsetLeft-bar.clientWidth/2+chip.clientWidth/2,behavior:'smooth'});
  const n=$('#cnt'); if(n) n.textContent=`${i+1}/${cardsNow().length}`;
}
function topOfCards(){
  // 옆으로 넘겼는데 화면이 아래쪽에 있으면 새 신문 맨 위로 올려 준다
  const y=$('#chips').getBoundingClientRect().top+window.scrollY;
  if(window.scrollY>y+4) window.scrollTo({top:y,behavior:'smooth'});
}
$('#grid').addEventListener('scroll',()=>{
  clearTimeout(tmr);
  tmr=setTimeout(()=>{
    const cs=cardsNow(); if(!cs.length) return;
    const x=$('#grid').scrollLeft+padL();
    let best=0; cs.forEach((c,i)=>{ if(Math.abs(c.offsetLeft-x)<Math.abs(cs[best].offsetLeft-x)) best=i; });
    if(best!==cur){ mark(best); topOfCards(); }
  },110);
},{passive:true});
$('#chips').addEventListener('click',e=>{
  const b=e.target.closest('.chip'); if(!b) return;
  const c=cardsNow()[+b.dataset.i]; if(!c) return;
  $('#grid').scrollTo({left:c.offsetLeft-padL(),behavior:'smooth'});
  mark(+b.dataset.i); topOfCards();
});
async function load(force){
  if(window.__DATA__){
    D=window.__DATA__;
    const t=new Date(D.at*1000).toLocaleString('ko-KR',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
    let s=`${t} 기준 · 2시간마다 자동 갱신`;
    if(!D.has_key)s+=' · AI 요약 꺼짐(GEMINI_API_KEY 시크릿 확인)'; else if(D.ai_error)s+=' · '+D.ai_error;
    const ageMin=Math.round((Date.now()/1000-D.at)/60);
    s+=` · ${ageMin<60?ageMin+'분':Math.floor(ageMin/60)+'시간 '+(ageMin%60)+'분'} 전에 수집`;
    $('#st').textContent=s; render();
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
$('#q').oninput=render;
if(window.__DATA__){
  // 정적(GitHub Pages) 모드: 버튼은 GitHub에서 새 수집을 실행하는 화면을 연다
  const user=location.hostname.split('.')[0], repo=location.pathname.split('/').filter(Boolean)[0]||'jogan-briefing';
  const btn=$('#re'); btn.textContent='지금 갱신하기';
  // 아이폰·아이패드·맥: '조간 브리핑 갱신' 단축어를 실행(토큰은 단축어 안에만 있음). 그 밖의 기기: GitHub 실행 화면.
  const apple=/iPhone|iPad|iPod|Macintosh/.test(navigator.userAgent);
  const SHORTCUT='조간 브리핑 갱신';
  btn.onclick=()=>{
    if(apple){
      location.href='shortcuts://run-shortcut?name='+encodeURIComponent(SHORTCUT);
      $('#st').textContent='갱신 요청을 보냈어요. 1~2분 뒤 새로 고침하면 새 기사가 보여요.';
    } else {
      location.href=`https://github.com/${user}/${repo}/actions/workflows/brief.yml`;
    }
  };
  // 홈 화면 앱은 예전 화면을 그대로 띄우는 경우가 많아, 다시 열 때 10분 넘었으면 새로 받아온다
  const opened=Date.now();
  document.addEventListener('visibilitychange',()=>{
    if(document.visibilityState==='visible'&&Date.now()-opened>10*60*1000)location.replace(location.pathname+'?t='+Date.now());
  });
} else { $('#re').onclick=()=>load(true); }
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
    ok = sum(1 for r in ("us", "kr", "jp") for p in data["data"][r] if p["items"])
    ops = sum(1 for p in data["data"].get("op", []) if p["items"])
    print(f"빌드 완료: {out}/index.html  (신문 {ok}곳 수집, 사설 {ops}곳, AI 요약 {'성공' if data['ai'] else '없음: ' + str(data['ai_error'] or '키 없음')})")


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
