# -*- coding: utf-8 -*-
"""
AI노출체크 엔진 — 구글 AI 개요(AI Overview) 노출 측정
======================================================
질문 → 구글 검색(깨끗한 새 세션) → AI 개요 감지 → 텍스트 추출
→ 우리 브랜드 + 경쟁사 언급 여부/순서 판정

- 실제 설치된 Chrome 사용(channel="chrome") — 봇 감지 최소화, 별도 다운로드 불필요
- 라운드마다 새 브라우저 컨텍스트(쿠키/기록 없음) = 개인화 오염 차단
- AI 개요 컨테이너: #m-x-content (2026-07 확인), 없으면 본문 텍스트 슬라이스 폴백
"""
import sys, io, time, json, random, re, urllib.parse

try:
    from playwright.sync_api import sync_playwright   # 자동화 측정용(레거시) — 없어도 앱 동작
except ImportError:
    sync_playwright = None


def _utf8_stdout():
    """콘솔이 cp949일 때 한글/이모지 깨짐 방지 (CLI 실행 시에만 호출)"""
    if sys.stdout and hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

AIO_MARKER = "AI 개요"
AIO_FOOTER = "AI 대답에는 오류가 있을 수 있습니다"
BLOCK_MARKERS = ("비정상적인 트래픽", "unusual traffic", "recaptcha")

import os
HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(HERE, "chrome_profile")   # 로그인 없는 전용 프로필


def _norm(s):
    """비교용 정규화 — 공백 제거 + 소문자"""
    return re.sub(r"\s+", "", str(s)).lower()


def find_mentions(text, names):
    """AI 개요 텍스트에서 브랜드들의 첫 언급 위치 찾기.
    returns [{name, idx}] (언급된 것만, 등장 순서대로)"""
    norm_text = _norm(text)
    out = []
    for name in names:
        n = _norm(name)
        if not n:
            continue
        idx = norm_text.find(n)
        if idx >= 0:
            out.append({"name": name, "idx": idx})
    out.sort(key=lambda m: m["idx"])
    return out


def judge(text, brand_aliases, competitors):
    """답변 텍스트에서 우리/경쟁사 언급 판정 (모든 엔진 공용).
    returns {mentions, exposed, rank, order, n_brands}"""
    all_names = list(brand_aliases) + list(competitors)
    mentions = find_mentions(text, all_names)
    brand_norms = {_norm(a) for a in brand_aliases}
    seen = []   # 등장 순서 (별칭은 '우리'로 통합)
    for m in mentions:
        label = "우리" if _norm(m["name"]) in brand_norms else m["name"]
        if label not in seen:
            seen.append(label)
    exposed = "우리" in seen
    return {"mentions": mentions, "exposed": exposed,
            "rank": (seen.index("우리") + 1) if exposed else None,
            "order": seen, "n_brands": len(seen)}


def extract_aio(page):
    """AI 개요 블록 텍스트 추출. 없으면 None"""
    # 1순위: 고정 id 컨테이너
    try:
        el = page.locator("#m-x-content")
        if el.count() > 0:
            t = el.first.inner_text(timeout=3000)
            if t and AIO_MARKER in t or (t and len(t) > 80):
                return t
    except Exception:
        pass
    # 폴백: 본문에서 마커~푸터 사이 슬라이스
    try:
        body = page.inner_text("body")
        if AIO_MARKER in body:
            start = body.index(AIO_MARKER)
            end = body.find(AIO_FOOTER, start)
            if end < 0:
                end = min(start + 4000, len(body))
            return body[start:end]
    except Exception:
        pass
    return None


def expand_aio(page):
    """'모두 표시' 버튼 눌러 AI 개요 전체 펼치기 (없으면 무시)"""
    for label in ("모두 표시", "자세히 알아보기 전에 더보기", "더보기"):
        try:
            btn = page.get_by_text(label, exact=True)
            if btn.count() > 0:
                btn.first.click(timeout=2000)
                time.sleep(1.5)
                return True
        except Exception:
            continue
    return False


def is_blocked(body_text):
    t = body_text.lower()
    return any(m in body_text or m in t for m in BLOCK_MARKERS)


def human_search(page, question):
    """사람처럼 검색: 구글 홈 → 검색창 타이핑 → 엔터"""
    page.goto("https://www.google.com/?hl=ko", timeout=30000,
              wait_until="domcontentloaded")
    time.sleep(0.8 + random.random())
    box = page.locator("textarea[name=q], input[name=q]").first
    box.click(timeout=8000)
    # 글자 단위 타이핑 (사람 속도)
    box.type(question, delay=60 + int(random.random() * 90))
    time.sleep(0.4 + random.random() * 0.8)
    box.press("Enter")
    page.wait_for_load_state("domcontentloaded", timeout=30000)


def measure_round(ctx, question, all_names, wait_sec=12):
    """한 라운드: 검색 → AI 개요 추출 → 판정
    returns {shown, blocked, text, mentions}"""
    page = ctx.new_page()
    try:
        try:
            human_search(page, question)
        except Exception:
            # 검색창 타이핑 실패 시 URL 직행 폴백
            page.goto("https://www.google.com/search?q="
                      + urllib.parse.quote(question) + "&hl=ko",
                      timeout=30000, wait_until="domcontentloaded")
        # AI 개요는 스트리밍으로 늦게 뜸 — 폴링 (+차단 감지)
        shown = False
        body = ""
        for _ in range(wait_sec):
            try:
                body = page.inner_text("body")
                if is_blocked(body):
                    return {"shown": False, "blocked": True, "text": "",
                            "mentions": []}
                if AIO_MARKER in body:
                    shown = True
                    break
            except Exception:
                pass
            time.sleep(1)
        if not shown:
            # 디버그: 안 떴을 때 실제 화면 상태 기록 (원인 구분용)
            snippet = re.sub(r"\s+", " ", body)[:250]
            return {"shown": False, "blocked": False, "text": "",
                    "mentions": [], "debug": snippet}
        time.sleep(3)          # 스트리밍 완료 대기
        expand_aio(page)       # 전체 펼치기
        text = extract_aio(page) or ""
        mentions = find_mentions(text, all_names)
        return {"shown": True, "blocked": False, "text": text,
                "mentions": mentions}
    finally:
        try:
            page.close()
        except Exception:
            pass


def measure_question(question, brand_aliases, competitors, repeats=3,
                     headless=False, on_progress=None):
    """질문 하나를 N라운드 측정해 집계.
    returns {question, rounds, aio_rate, expose_rate, avg_rank, competitor_stats}"""
    if sync_playwright is None:
        raise RuntimeError("playwright 미설치 — 구글 AI개요는 크롬확장으로 측정하세요")
    brand_aliases = [a for a in (brand_aliases or []) if str(a).strip()]
    competitors = [c for c in (competitors or []) if str(c).strip()]
    all_names = brand_aliases + competitors
    rounds = []

    with sync_playwright() as p:
        # 전용 프로필(로그인 X) 유지 — 쿠키가 있어야 봇 의심 덜 받음.
        # 개인화 걱정되면 chrome_profile 폴더를 지우면 리셋됨.
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR, channel="chrome", headless=headless,
            user_agent=UA, locale="ko-KR",
            viewport={"width": 1280, "height": 2400},
            args=["--disable-blink-features=AutomationControlled", "--lang=ko-KR"])
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        try:
            for r in range(repeats):
                if on_progress:
                    on_progress(r + 1, repeats)
                try:
                    res = measure_round(ctx, question, all_names)
                except Exception as e:
                    res = {"shown": False, "blocked": False, "text": "",
                           "mentions": [], "error": str(e)[:200]}

                if res.get("blocked"):
                    # 구글 차단 감지 → 더 돌려봐야 다 차단. 즉시 중단하고 표기.
                    res.update({"exposed": False, "rank": None,
                                "order": [], "n_brands": 0})
                    rounds.append(res)
                    break

                # 우리 브랜드 판정 (공용 judge 사용)
                j = judge(res.get("text", ""), brand_aliases, competitors)
                res.update({"exposed": j["exposed"], "rank": j["rank"],
                            "order": j["order"], "n_brands": j["n_brands"]})
                rounds.append(res)
                if r < repeats - 1:
                    time.sleep(15 + random.random() * 15)  # 라운드 간 15~30초 (봇 감지 회피)
        finally:
            ctx.close()

    return aggregate(question, rounds)


def aggregate(question, rounds):
    """라운드 목록 → 집계 결과 (Playwright 측정·크롬확장 측정 공용)"""
    blocked = any(r.get("blocked") for r in rounds)
    valid = [r for r in rounds if not r.get("blocked")]   # 차단 라운드는 통계 제외
    n = len(valid)
    n_shown = sum(1 for r in valid if r["shown"])
    n_exposed = sum(1 for r in valid if r.get("exposed"))
    ranks = [r["rank"] for r in valid if r.get("rank")]
    comp_count = {}
    for r in valid:
        for label in r.get("order", []):
            if label != "우리":
                comp_count[label] = comp_count.get(label, 0) + 1
    return {
        "question": question,
        "rounds": rounds,
        "n": n,
        "blocked": blocked,                                       # 구글 차단 걸림 여부
        "aio_rate": round(n_shown / n * 100) if n else 0,        # AI개요 표시율
        "expose_rate": round(n_exposed / n_shown * 100) if n_shown else 0,  # 노출률(AIO 뜬 것 중)
        "avg_rank": round(sum(ranks) / len(ranks), 1) if ranks else None,
        "competitors_seen": sorted(comp_count.items(), key=lambda x: -x[1]),
    }


# ═══════════════════════════════════════════════════════════════
#  Gemini 엔진 — 구글검색 그라운딩 켠 API (신규유저 시점, 상태 없음)
# ═══════════════════════════════════════════════════════════════
import urllib.request

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "%s:generateContent?key=%s")


def gemini_ask(question, api_key, model=GEMINI_MODEL, timeout=60):
    """Gemini에 질문 1회 (구글검색 그라운딩 ON).
    returns (text, sources, error) — sources=[{u,t}] 그라운딩 출처(어떤 사이트 보고 답했나)"""
    body = {
        "contents": [{"role": "user", "parts": [{"text": question}]}],
        "tools": [{"google_search": {}}],   # 실시간 구글검색 근거 = 실제 유저 경험
    }
    req = urllib.request.Request(
        GEMINI_URL % (model, api_key),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        cand = (data.get("candidates") or [{}])[0]
        parts = cand.get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        # 그라운딩 출처 (제목에 도메인이 들어있음 — 채널 분류용)
        sources = []
        gm = cand.get("groundingMetadata") or {}
        for ch in (gm.get("groundingChunks") or [])[:20]:
            w = ch.get("web") or {}
            if w.get("uri") or w.get("title"):
                sources.append({"u": (w.get("uri") or "")[:300],
                                "t": (w.get("title") or "")[:80]})
        if not text:
            return "", [], "빈 응답: " + json.dumps(data)[:200]
        return text, sources, None
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8"))
            msg = detail.get("error", {}).get("message", "")[:200]
        except Exception:
            msg = ""
        if e.code in (400, 403):
            return "", [], "API 키 오류(%d): %s" % (e.code, msg)
        if e.code == 429:
            return "", [], "쿼터 초과(429): 무료 한도 소진 — 잠시 후 재시도"
        return "", [], "HTTP %d: %s" % (e.code, msg)
    except Exception as e:
        return "", [], str(e)[:200]


def measure_question_gemini(question, brand_aliases, competitors, api_key,
                            repeats=2, on_progress=None):
    """Gemini로 N회 측정·집계. 구글AI개요와 달리 '표시율' 없음(항상 답함)."""
    brand_aliases = [a for a in (brand_aliases or []) if str(a).strip()]
    competitors = [c for c in (competitors or []) if str(c).strip()]
    rounds = []
    fatal = None
    for r in range(repeats):
        if on_progress:
            on_progress(r + 1, repeats)
        text, sources, err = gemini_ask(question, api_key)
        if err:
            rounds.append({"error": err, "text": "", "exposed": False,
                           "rank": None, "order": [], "n_brands": 0, "links": []})
            if "API 키" in err or "쿼터" in err:
                fatal = err     # 키/쿼터 문제면 더 돌려봐야 소용없음
                break
            continue
        j = judge(text, brand_aliases, competitors)
        rounds.append({"text": text, "exposed": j["exposed"], "rank": j["rank"],
                       "order": j["order"], "n_brands": j["n_brands"],
                       "links": sources})
        if r < repeats - 1:
            time.sleep(1.5)

    valid = [r for r in rounds if not r.get("error")]
    n = len(valid)
    n_exposed = sum(1 for r in valid if r["exposed"])
    ranks = [r["rank"] for r in valid if r.get("rank")]
    comp_count = {}
    for r in valid:
        for label in r.get("order", []):
            if label != "우리":
                comp_count[label] = comp_count.get(label, 0) + 1
    return {
        "question": question,
        "rounds": rounds,
        "n": n,
        "error": fatal,
        "expose_rate": round(n_exposed / n * 100) if n else 0,
        "avg_rank": round(sum(ranks) / len(ranks), 1) if ranks else None,
        "competitors_seen": sorted(comp_count.items(), key=lambda x: -x[1]),
    }


# ── CLI 테스트: python engine.py "질문" [반복수] ──
if __name__ == "__main__":
    _utf8_stdout()
    q = sys.argv[1] if len(sys.argv) > 1 else "조형물 제작 업체 추천"
    rep = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    print("[측정]", q, "x%d회" % rep)
    result = measure_question(
        q,
        brand_aliases=["하오팩토리", "haofactory"],
        competitors=["마롱컴퍼니", "예본조형", "헤파이스토스웍스", "턴키스튜디오", "다만드러"],
        repeats=rep,
        on_progress=lambda i, t: print("  라운드 %d/%d..." % (i, t)))
    print(json.dumps({k: v for k, v in result.items() if k != "rounds"},
                     ensure_ascii=False, indent=1))
    for i, r in enumerate(result["rounds"]):
        print("  R%d: AIO=%s 노출=%s 순위=%s 순서=%s" %
              (i + 1, r["shown"], r.get("exposed"), r.get("rank"),
               "→".join(r.get("order", []))))
