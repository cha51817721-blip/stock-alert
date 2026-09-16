#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
관심종목 실시간 뉴스 알림 봇
- 네이버 검색 API로 관심종목·산업테마 뉴스를 폴링
- 새로 뜬 기사만 골라 텔레그램으로 원문 링크와 함께 전송
"""

import os
import re
import sys
import json
import time
import html
import argparse
import datetime as dt

import requests

KST = dt.timezone(dt.timedelta(hours=9))
BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE, "state")
STATE_FILE = os.path.join(STATE_DIR, "seen.json")

# 네이버 검색 API는 2026년 7월 NAVER API HUB로 이관됐다.
# 새로 발급한 키는 HUB로, 예전에 받아둔 키는 구 주소로 가야 해서 둘 다 지원하고
# 첫 호출에서 되는 쪽을 자동으로 골라 이후 계속 쓴다.
NAVER_ENDPOINTS = [
    {
        "name": "API HUB",
        "url": "https://naverapihub.apigw.ntruss.com/search/v1/news",
        "id_header": "X-NCP-APIGW-API-KEY-ID",
        "secret_header": "X-NCP-APIGW-API-KEY",
    },
    {
        "name": "구 개발자센터",
        "url": "https://openapi.naver.com/v1/search/news.json",
        "id_header": "X-Naver-Client-Id",
        "secret_header": "X-Naver-Client-Secret",
    },
]
_ACTIVE_ENDPOINT = None      # 한 번 정해지면 그 실행 동안 계속 사용

TG_API = "https://api.telegram.org/bot{}/sendMessage"

TAG_RE = re.compile(r"<[^>]+>")


def log(*a):
    print(f"[{dt.datetime.now(KST):%H:%M:%S}]", *a, flush=True)


def clean(text):
    """네이버 응답의 <b> 태그와 HTML 엔티티 제거."""
    if not text:
        return ""
    return html.unescape(TAG_RE.sub("", text)).strip()


def esc(text):
    """텔레그램 HTML parse_mode용 이스케이프."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------- state

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        return {"news": {}, "stories": {}, "bootstrapped": False}
    s.setdefault("news", {})
    s.setdefault("stories", {})
    s.setdefault("bootstrapped", False)
    s.pop("dart", None)          # 예전 버전에서 남은 공시 기록은 버린다
    return s


def save_state(state, retention_days, story_hours=48):
    cutoff = time.time() - retention_days * 86400
    state["news"] = {k: v for k, v in state["news"].items() if v > cutoff}
    story_cutoff = time.time() - story_hours * 3600
    state["stories"] = {k: v for k, v in state.get("stories", {}).items()
                        if v.get("t", 0) > story_cutoff}
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)
    log(f"state 저장: 기사 {len(state['news'])}건 / 사건 {len(state.get('stories', {}))}건")


# 제목에 흔히 붙는 말머리와 뜻 없는 낱말 — 같은 기사인지 볼 때 무시한다
BRACKET_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)|【[^】]*】|<[^>]*>|「[^」]*」")
STOPWORDS = {
    "단독", "속보", "종합", "특징주", "마감", "개장", "오늘", "어제", "내일",
    "관련", "가운데", "대한", "위해", "통해", "따라", "밝혔다", "말했다", "전했다",
    "이라고", "라고", "지난", "올해", "내년", "기자", "뉴스", "무단", "전재", "재배포",
}


# 낱말 끝에 붙는 조사와 흔한 어미. 긴 것부터 떼어낸다.
# "스타트업이"와 "스타트업"을 같은 말로 세기 위한 장치다.
SUFFIXES = [
    "이라고는", "으로서는", "으로써는", "에게서는",
    "이라고", "으로서", "으로써", "에게서", "에서는", "에게는", "한테는",
    "까지는", "부터는", "보다는", "와의", "과의", "들이", "들을", "들은", "들도",
    "라고", "이란", "이라", "이나", "으로", "에서", "에게", "한테", "까지",
    "부터", "보다", "처럼", "마다", "했다", "한다", "하고", "하는", "였다",
    "았다", "었다", "이다", "된다", "됐다",
    "은", "는", "이", "가", "을", "를", "의", "에", "와", "과", "도",
    "만", "로", "나", "랑", "해", "한", "된", "할",
]


def normalize_word(w):
    """낱말 끝의 조사·어미를 떼어낸다. 떼고 나서 너무 짧아지면 원래 것을 쓴다."""
    for suf in SUFFIXES:
        if len(w) > len(suf) and w.endswith(suf):
            stem = w[:-len(suf)]
            if len(stem) >= 2:
                return stem
            break
    return w


def story_tokens(title):
    """제목에서 뜻이 있는 낱말만 뽑아낸다. 같은 사건인지 비교하는 데 쓴다."""
    t = BRACKET_RE.sub(" ", title or "")
    t = re.sub(r"[^0-9A-Za-z가-힣]+", " ", t)
    out = set()
    for w in t.split():
        w = normalize_word(w.lower())
        if len(w) < 2 or w in STOPWORDS:
            continue
        out.add(w)
    return out


def story_bigrams(title):
    """제목에서 공백·기호를 다 뺀 뒤 두 글자씩 잘라낸다.
    "공급 계약"과 "공급계약"처럼 띄어쓰기만 다른 제목을 같게 보기 위한 장치."""
    t = BRACKET_RE.sub(" ", title or "")
    t = re.sub(r"[^0-9A-Za-z가-힣]+", "", t).lower()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _containment(a, b):
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def same_story(a, b, min_ratio, min_shared, ga=None, gb=None,
               ba=None, bb=None, body_ratio=0.5, body_shared=10,
               pub_a=None, pub_b=None, near_hours=6,
               near_ratio=0.35, near_shared=9):
    """두 기사가 같은 사건을 다루는지 판단한다.
    제목이 아예 다르게 뽑혀도 본문 요약이 거의 같은 경우가 많아 본문까지 본다."""
    # ① 제목 낱말 — 겹치는 개수와 비율을 함께 본다
    if a and b:
        shared = len(a & b)
        smaller = min(len(a), len(b))
        if shared >= min(min_shared, smaller):
            ratio = shared / smaller
            if ratio >= (0.9 if smaller < min_shared else min_ratio):
                return True

    # ② 제목 글자 — 띄어쓰기나 조사만 다른 재송고를 잡는다
    if ga and gb and min(len(ga), len(gb)) >= 8:
        if _containment(ga, gb) >= 0.75:
            return True

    # ③ 제목+본문 낱말 — 언론사마다 제목을 딴판으로 뽑은 같은 사건을 잡는다.
    #    본문까지 합치면 글이 길어져서, 겹치는 낱말 개수 조건을 넉넉히 건다.
    if ba and bb:
        shared = len(ba & bb)
        smaller = min(len(ba), len(bb))
        ratio = shared / smaller
        if shared >= body_shared and ratio >= body_ratio:
            return True

        # ④ 비슷한 시각에 나온 기사끼리는 기준을 낮춘다.
        #    보도자료를 받아쓴 기사들은 몇 분~몇 시간 안에 몰려 나오기 때문이다.
        if pub_a and pub_b and abs(pub_a - pub_b) <= near_hours * 3600:
            if shared >= near_shared and ratio >= near_ratio:
                return True
    return False


def news_key(url, title):
    """같은 기사가 여러 경로로 들어와도 한 번만 잡히도록 키를 정규화."""
    u = (url or "").split("?")[0].rstrip("/").lower()
    u = re.sub(r"^https?://(www\.|m\.)?", "", u)
    t = re.sub(r"[^0-9a-z가-힣]", "", (title or "").lower())[:40]
    return u or t


# ---------------------------------------------------------------- naver news

def parse_naver_date(s):
    try:
        return dt.datetime.strptime(s, "%a, %d %b %Y %H:%M:%S %z")
    except (ValueError, TypeError):
        return None


def _call_naver(ep, query, cid, csec, display, sort):
    """한 엔드포인트로 호출. (성공여부, 기사목록, 상태코드) 반환."""
    params = {"query": query, "display": display, "start": 1, "sort": sort}
    headers = {ep["id_header"]: cid, ep["secret_header"]: csec}
    try:
        r = requests.get(ep["url"], params=params, headers=headers, timeout=15)
    except (requests.RequestException, ValueError) as e:
        log(f"  네이버 호출 예외({ep['name']}): {type(e).__name__} {e}")
        return False, [], None
    if r.status_code != 200:
        return False, [], r.status_code
    try:
        return True, r.json().get("items", []), 200
    except ValueError:
        log(f"  네이버 응답이 JSON이 아님({ep['name']}): {r.text[:120]}")
        return False, [], 200


def fetch_naver_news(query, cid, csec, display=30, sort="date"):
    global _ACTIVE_ENDPOINT

    if _ACTIVE_ENDPOINT is not None:
        ok, items, code = _call_naver(_ACTIVE_ENDPOINT, query, cid, csec, display, sort)
        if not ok:
            log(f"  네이버 API 오류 {code} ({_ACTIVE_ENDPOINT['name']}) — 검색어: {query}")
            return []
    else:
        # 첫 호출: 어느 주소가 이 키를 받아주는지 찾는다
        items, code = [], None
        for ep in NAVER_ENDPOINTS:
            ok, items, code = _call_naver(ep, query, cid, csec, display, sort)
            if ok:
                _ACTIVE_ENDPOINT = ep
                log(f"네이버 검색 API 연결됨 → {ep['name']}")
                break
            if code == 429:
                log("  네이버 API 호출 한도 초과(429)")
                return []
        else:
            log(f"  네이버 API 연결 실패 (마지막 응답 {code}). "
                f"키가 맞는지, 콘솔에서 검색 API 이용신청을 했는지 확인하세요.")
            return []

    out = []
    for it in items:
        out.append({
            "title": clean(it.get("title")),
            "desc": clean(it.get("description")),
            "url": it.get("originallink") or it.get("link"),
            "naver_url": it.get("link"),
            "pub": parse_naver_date(it.get("pubDate")),
        })
    return out


def is_price_noise(item, noise):
    """주가가 얼마 올랐다는 얘기만 있고 이유가 없는 기사인지 판단.
    시세 단어가 있어도 수주·실적 같은 알맹이가 함께 있으면 남긴다."""
    if not noise:
        return False
    blob = f"{item['title']} {item['desc']}"
    if not any(w in blob for w in noise.get("price_move_words", [])):
        return False                      # 시세 기사가 아예 아님
    return not any(w in blob for w in noise.get("substance_words", []))


def matches(item, must_any, require_any, exclude_any, must_in_title=False):
    blob = f"{item['title']} {item['desc']}"
    # must_in_title이면 핵심어가 제목에 있어야 한다. 본문에 한 번 스친 기사를 막는다.
    haystack = item["title"] if must_in_title else blob
    if must_any and not any(k in haystack for k in must_any):
        return False
    if require_any and not any(k in blob for k in require_any):
        return False
    if exclude_any and any(k in blob for k in exclude_any):
        return False
    return True


def collect_news(cfg, state, cid, csec, now):
    lookback = dt.timedelta(minutes=cfg["poll"]["news_lookback_minutes"])
    noise = cfg.get("noise_filter")
    dup_cfg = cfg.get("duplicate_filter") or {}
    dup_on = dup_cfg.get("enabled", True)
    min_ratio = dup_cfg.get("similarity", 0.7)
    min_shared = dup_cfg.get("min_shared_words", 4)
    body_ratio = dup_cfg.get("body_similarity", 0.5)
    body_shared = dup_cfg.get("body_min_shared_words", 10)
    near_hours = dup_cfg.get("near_hours", 6)
    near_ratio = dup_cfg.get("near_similarity", 0.35)
    near_shared = dup_cfg.get("near_min_shared_words", 9)
    window = dup_cfg.get("window_hours", 48) * 3600

    # 이미 보낸 사건들의 제목 낱말 — 언론사만 바뀐 같은 기사를 잡아내는 데 쓴다
    cutoff = time.time() - window
    known = [(set(v.get("w", [])), set(v.get("g", [])), set(v.get("b", [])), v.get("p"))
             for v in state.get("stories", {}).values()
             if v.get("t", 0) > cutoff]

    dropped = [0]
    dup_dropped = [0]
    hits = []

    def scan(entries, label, emoji, cap):
        picked = []
        for ent in entries:
            for q in ent["queries"]:
                for item in fetch_naver_news(q, cid, csec):
                    if not item["url"]:
                        continue
                    if item["pub"] and now - item["pub"] > lookback:
                        continue
                    if not matches(item, ent.get("must_include_any"),
                                   ent.get("require_any"),
                                   ent.get("exclude_any"),
                                   ent.get("require_in_title", False)):
                        continue
                    if is_price_noise(item, noise):
                        dropped[0] += 1
                        continue
                    key = news_key(item["url"], item["title"])
                    if key in state["news"] or any(h["key"] == key for h in picked):
                        continue
                    toks = story_tokens(item["title"])
                    grams = story_bigrams(item["title"])
                    btoks = story_tokens(f"{item['title']} {item['desc']}")
                    pub_ts = item["pub"].timestamp() if item.get("pub") else None
                    if dup_on and any(
                            same_story(toks, kw, min_ratio, min_shared, grams, kg,
                                       btoks, kb, body_ratio, body_shared,
                                       pub_ts, kp, near_hours, near_ratio, near_shared)
                            for kw, kg, kb, kp in known):
                        dup_dropped[0] += 1
                        continue
                    if dup_on:
                        # 같은 사이클 안에서 연달아 들어온 재탕도 막는다
                        known.append((toks, grams, btoks, pub_ts))
                    picked.append({
                        "key": key, "kind": label, "emoji": emoji,
                        "subject": ent["name"], "tokens": sorted(toks),
                        "grams": sorted(grams), "btokens": sorted(btoks),
                        "pub_ts": pub_ts, **item,
                    })
                time.sleep(0.12)   # 네이버 API 호출 간 최소 간격
        picked.sort(key=lambda h: h["pub"] or now, reverse=True)
        return picked[:cap]

    hits += scan(cfg["stocks"], "종목", "🔔",
                 cfg["poll"]["max_news_per_cycle"])
    hits += scan(cfg["themes"], "산업", "🏭",
                 cfg["poll"]["max_theme_news_per_cycle"])
    if dropped[0]:
        log(f"시세만 다룬 기사 {dropped[0]}건 제외")
    if dup_dropped[0]:
        log(f"이미 보낸 사건의 재탕 기사 {dup_dropped[0]}건 제외")
    return hits


# ---------------------------------------------------------------- telegram

def render(hit):
    lines = [f"{hit['emoji']} <b>{esc(hit['subject'])}</b> · {hit['kind']}"]
    lines.append(esc(hit["title"]))
    if hit.get("desc"):
        lines.append(f"<i>{esc(hit['desc'][:160])}</i>")

    when = f" · {hit['pub'].astimezone(KST):%m/%d %H:%M}" if hit.get("pub") else ""
    link = f'🔗 <a href="{esc(hit["url"])}">기사 원문</a>{when}'
    if hit.get("naver_url") and hit["naver_url"] != hit["url"]:
        link += f' · <a href="{esc(hit["naver_url"])}">네이버</a>'
    lines.append(link)
    return "\n".join(lines)


def send_telegram(token, chat_id, text, dry=False):
    if dry:
        print("\n----- (전송 미리보기) -----\n" + text)
        return True
    try:
        r = requests.post(
            TG_API.format(token),
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": False},
            timeout=15)
        if r.status_code != 200:
            log(f"  텔레그램 전송 실패 {r.status_code}: {r.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        log(f"  텔레그램 예외: {type(e).__name__} {e}")
        return False


# ---------------------------------------------------------------- 점검

def run_check(naver_id, naver_secret, tg_token, tg_chat):
    """키 4개가 실제로 작동하는지 확인하고 결과를 찍는다."""
    print("=" * 46)
    print(" 설정 점검")
    print("=" * 46)

    print("\n[1/2] 네이버 검색 API")
    items = fetch_naver_news("삼성전자", naver_id, naver_secret, display=3)
    naver_ok = bool(items)
    if naver_ok:
        print(f"  ✅ 정상 — 기사 {len(items)}건 받았습니다.")
        print(f"     예시: {items[0]['title'][:50]}")
        print(f"     원문: {items[0]['url']}")
    else:
        print("  ❌ 실패 — 위 로그의 상태코드를 확인하세요.")
        print("     401/403 이면 키가 틀렸거나 검색 API 이용신청이 안 된 상태입니다.")
        print("     404 이면 주소 문제입니다. 알려주세요.")

    print("\n[2/2] 텔레그램")
    if not (tg_token and tg_chat):
        print("  ⏭️  토큰이나 chat id가 없어 건너뜁니다.")
        tg_ok = False
    else:
        tg_ok = send_telegram(
            tg_token, tg_chat,
            "🧪 <b>연결 점검</b>\n이 메시지가 보이면 텔레그램 설정은 끝난 겁니다.")
        print("  ✅ 전송 성공 — 텔레그램을 확인해 보세요." if tg_ok
              else "  ❌ 전송 실패 — 위 오류를 확인하세요.")

    print("\n" + "=" * 46)
    if naver_ok and tg_ok:
        print(" 전부 정상입니다. 이제 자동으로 돌기 시작합니다.")
        return 0
    print(" 문제가 있습니다. 위 내용을 확인해 주세요.")
    return 1


def run_probe(cfg, naver_id, naver_secret):
    """읽음 기록과 시간 제한을 무시하고, 지금 필터에 걸리는 기사를 전부 보여준다.
    검색어와 제외어를 손본 뒤 의도대로 걸리는지 확인하는 용도."""
    cfg = json.loads(json.dumps(cfg))          # 원본을 건드리지 않으려고 복사
    cfg["poll"]["news_lookback_minutes"] = 1440
    cfg["poll"]["max_news_per_cycle"] = 500
    cfg["poll"]["max_theme_news_per_cycle"] = 500

    now = dt.datetime.now(KST)
    hits = collect_news(cfg, {"news": {}, "stories": {}}, naver_id, naver_secret, now)

    print("\n" + "=" * 60)
    print(f" 최근 24시간 필터 통과 기사 — 총 {len(hits)}건")
    print("=" * 60)

    by_subject = {}
    for h in hits:
        by_subject.setdefault(f"{h['emoji']} {h['subject']}", []).append(h)

    for subject in [f"{'🔔'} {s['name']}" for s in cfg["stocks"]] + \
                   [f"{'🏭'} {t['name']}" for t in cfg["themes"]]:
        found = by_subject.get(subject, [])
        print(f"\n{subject} — {len(found)}건")
        if not found:
            print("   (없음) 검색어가 좁거나 오늘 기사가 없는 경우입니다.")
        for h in found[:8]:
            when = f"{h['pub'].astimezone(KST):%m/%d %H:%M}" if h.get("pub") else "시각미상"
            print(f"   [{when}] {h['title'][:60]}")
            print(f"            {h['url']}")
        if len(found) > 8:
            print(f"   … 외 {len(found) - 8}건")

    print("\n" + "=" * 60)
    print(" 이 목록은 화면에만 나오고 텔레그램으로는 가지 않습니다.")
    print(" 빠진 기사가 있으면 config.json의 queries를 넓히고,")
    print(" 엉뚱한 기사가 있으면 exclude_any에 그 단어를 넣으세요.")
    return 0


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="텔레그램으로 보내지 않고 화면에만 출력")
    ap.add_argument("--force", action="store_true",
                    help="조용한 시간대 무시하고 실행")
    ap.add_argument("--reset", action="store_true",
                    help="기록을 지우고 처음부터 (첫 실행처럼 동작)")
    ap.add_argument("--check", action="store_true",
                    help="키가 제대로 붙었는지만 점검하고 종료")
    ap.add_argument("--probe", action="store_true",
                    help="필터 시험용 — 최근 24시간에서 걸리는 기사를 전부 화면에 나열")
    args = ap.parse_args()

    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)

    naver_id = os.environ.get("NAVER_CLIENT_ID", "")
    naver_secret = os.environ.get("NAVER_CLIENT_SECRET", "")
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not (naver_id and naver_secret):
        log("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 가 없습니다. 종료합니다.")
        return 1
    if not args.dry_run and not (tg_token and tg_chat):
        log("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 없습니다. 종료합니다.")
        return 1

    if args.check:
        return run_check(naver_id, naver_secret, tg_token, tg_chat)

    if args.probe:
        return run_probe(cfg, naver_id, naver_secret)

    now = dt.datetime.now(KST)
    if not args.force and now.hour in cfg["poll"].get("quiet_hours_kst", []):
        log(f"조용한 시간대({now.hour}시)라 건너뜁니다.")
        return 0

    state = ({"news": {}, "stories": {}, "bootstrapped": False}
             if args.reset else load_state())
    first_run = not state["bootstrapped"]

    hits = collect_news(cfg, state, naver_id, naver_secret, now)

    # 한 기사가 종목과 산업 테마에 동시에 걸리는 경우가 있어 사이클 안에서도 한 번 더 걸러낸다.
    # 종목 스캔이 먼저 돌기 때문에 겹치면 종목 알림 쪽이 남는다.
    unique, seen_now = [], set()
    for h in hits:
        if h["key"] in seen_now:
            continue
        seen_now.add(h["key"])
        unique.append(h)
    if len(unique) != len(hits):
        log(f"사이클 내 중복 {len(hits) - len(unique)}건 제거")
    hits = unique

    log(f"새로 잡힌 기사 {len(hits)}건")

    if first_run:
        # 첫 실행에 과거 기사가 한꺼번에 쏟아지지 않게 기록만 하고 넘어감
        for h in hits:
            state["news"][h["key"]] = time.time()
            state["stories"][h["key"]] = {"w": h.get("tokens", []),
                                          "g": h.get("grams", []),
                                          "b": h.get("btokens", []),
                                          "p": h.get("pub_ts"), "t": time.time()}
        state["bootstrapped"] = True
        save_state(state, cfg["poll"]["seen_retention_days"],
                   (cfg.get("duplicate_filter") or {}).get("window_hours", 48))
        msg = (f"✅ <b>관심종목 실시간 뉴스 알림 시작</b>\n"
               f"종목 {len(cfg['stocks'])}개 · 산업 {len(cfg['themes'])}개 감시 중\n"
               f"기존 기사 {len(hits)}건은 읽음 처리했습니다. 지금부터 새로 뜨는 것만 보냅니다.")
        send_telegram(tg_token, tg_chat, msg, args.dry_run)
        return 0

    sent = 0
    for h in hits:
        if send_telegram(tg_token, tg_chat, render(h), args.dry_run):
            state["news"][h["key"]] = time.time()
            state["stories"][h["key"]] = {"w": h.get("tokens", []),
                                          "g": h.get("grams", []),
                                          "b": h.get("btokens", []),
                                          "p": h.get("pub_ts"), "t": time.time()}
            sent += 1
            time.sleep(0.4)        # 텔레그램 rate limit 여유
    log(f"{sent}건 전송")
    save_state(state, cfg["poll"]["seen_retention_days"],
               (cfg.get("duplicate_filter") or {}).get("window_hours", 48))
    return 0


if __name__ == "__main__":
    sys.exit(main())
