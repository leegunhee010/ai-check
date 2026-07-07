# -*- coding: utf-8 -*-
"""
AI노출체크 — 구글 AI 개요 브랜드 노출 측정 GUI
=================================================
- 질문 목록 관리(카테고리별) + [측정] → engine.py 로 AI 개요 노출 자동 측정
- 우리 브랜드 노출률 / 언급 순위 / 경쟁사 비교
- 결과 JSON 누적 + CSV 내보내기
- 추후: GPT / Gemini 엔진 추가 예정 (엔진 컬럼 확장형 구조)

실행: python app.py → 브라우저 http://127.0.0.1:5610
"""
import os, sys, json, csv, io, time, threading, webbrowser, datetime, socket
import importlib.util, urllib.request, urllib.parse
from flask import Flask, request, jsonify, Response

FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "AI체크_데이터.json")
SETTINGS_FILE = os.path.join(HERE, "AI체크_설정.json")
SHOTS_DIR = os.path.join(HERE, "캡처")
PORT = 5630

_spec = importlib.util.spec_from_file_location("engine", os.path.join(HERE, "engine.py"))
engine = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(engine)

app = Flask(__name__)


def jload(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def jsave(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_data():
    return jload(DATA_FILE, {"questions": [], "results": {}, "next_id": 1})


def load_settings():
    s = jload(SETTINGS_FILE, {})
    return {
        "brand_name": s.get("brand_name", "하오팩토리"),
        "brand_aliases": s.get("brand_aliases", ["하오팩토리", "haofactory"]),
        "competitors": s.get("competitors", []),
        "own_domains": s.get("own_domains", ["haodesign.co.kr"]),   # 우리 채널 판별용
        "repeats": int(s.get("repeats", 2)),
        "show_browser": bool(s.get("show_browser", True)),
        # 구글 AI개요 — 크롬확장이 이 값을 읽고 측정 여부 결정
        "use_google_aio": bool(s.get("use_google_aio", True)),
        "use_gemini": bool(s.get("use_gemini", True)),
        "gemini_mode": s.get("gemini_mode", "web"),        # web=크롬확장 / api=API키
        "use_chatgpt": bool(s.get("use_chatgpt", True)),   # 크롬확장이 읽어감
        "gemini_api_key": s.get("gemini_api_key", ""),
    }


# ── Supabase 중앙 공유 (AI노출체크 전용 프로젝트) ──
# anon 키(공개용·RLS로 ai_measurements 테이블만 허용). 설정 supabase_url/key로 override 가능.
SUPABASE_URL = "https://ryeooxioxpmdkttgvdzh.supabase.co"
SUPABASE_KEY = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJ5ZW9v"
                "eGlveHBtZGt0dGd2ZHpoIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODMzMTI5MjEsImV4cCI6MjA5"
                "ODg4ODkyMX0.K2B_PK1oszMcwb9zzKzubZRsFlxSl-1O2YPOXcyt3BU")
DEVICE = socket.gethostname()


def _supa_cfg():
    s = jload(SETTINGS_FILE, {})
    return (s.get("supabase_url") or SUPABASE_URL), (s.get("supabase_key") or SUPABASE_KEY)


def _supa(method, path, body=None, timeout=12, prefer=None):
    url, key = _supa_cfg()
    h = {"apikey": key, "Authorization": "Bearer " + key,
         "Content-Type": "application/json"}
    if prefer:
        h["Prefer"] = prefer
    elif method == "POST":
        h["Prefer"] = "return=minimal"
    req = urllib.request.Request(
        url + path,
        data=(json.dumps(body).encode("utf-8") if body is not None else None),
        headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            t = r.read().decode("utf-8")
            return json.loads(t) if t else True
    except Exception:
        return None      # 인터넷/테이블 문제면 조용히 스킵 (로컬 저장은 항상 됨)


def _supa_push(question, cat, engine_key, res):
    """측정 결과 요약을 중앙 DB에 기록 — 추이용 append 로그 (백그라운드)"""
    st = load_settings()
    row = {"device": DEVICE, "brand": st["brand_name"], "category": cat,
           "question": question, "engine": engine_key,
           "n": res.get("n", 0), "aio_rate": res.get("aio_rate"),
           "expose_rate": res.get("expose_rate"), "avg_rank": res.get("avg_rank")}
    _supa("POST", "/rest/v1/ai_measurements", row)


def _supa_push_result(qid, engine_key, res):
    """최신 상세 결과를 중앙에 upsert — 모든 PC가 같은 표를 보게"""
    st = load_settings()
    _supa("POST", "/rest/v1/ai_results?on_conflict=brand,question_id,engine",
          {"brand": st["brand_name"], "question_id": qid, "engine": engine_key,
           "ts": res.get("ts", ""), "data": res,
           "updated_at": datetime.datetime.utcnow().isoformat() + "Z"},
          prefer="resolution=merge-duplicates,return=minimal")


# ── 중앙 동기화: 질문 목록 + 결과를 모든 PC가 공유 ─────────────
_SYNC = {"t": 0.0, "ok": None}


def sync_central(force=False):
    """중앙 질문/결과 풀 + 로컬에만 있는 질문 업로드. 15초 캐시.
    오프라인/테이블 없으면 조용히 로컬 모드로 동작."""
    now = time.time()
    if not force and now - _SYNC["t"] < 15:
        return _SYNC["ok"]
    _SYNC["t"] = now
    st = load_settings()
    brand = urllib.parse.quote(st["brand_name"])

    central = _supa("GET", "/rest/v1/ai_questions?select=id,cat,q"
                    "&brand=eq." + brand + "&order=id.asc&limit=5000")
    if central is None:
        _SYNC["ok"] = False
        return False

    with _lock:
        data = load_data()
        by_text = {r["q"]: r for r in central}
        # ① 로컬에만 있는 질문 → 중앙 업로드 (기존 사용자 자동 마이그레이션)
        for q in data["questions"]:
            if q["q"] not in by_text:
                ins = _supa("POST",
                            "/rest/v1/ai_questions?on_conflict=brand,q",
                            {"brand": st["brand_name"], "cat": q["cat"], "q": q["q"]},
                            prefer="resolution=merge-duplicates,return=representation")
                if isinstance(ins, list) and ins:
                    by_text[q["q"]] = ins[0]
        # ② 새 질문 목록 = 중앙 기준 (id도 중앙 id)
        new_qs = [{"id": r["id"], "cat": r.get("cat") or "기본", "q": r["q"]}
                  for r in sorted(by_text.values(), key=lambda x: x["id"])]
        # ③ 로컬 결과의 옛 id → 중앙 id로 이관 (질문 텍스트 매칭)
        old_text = {str(q["id"]): q["q"] for q in data["questions"]}
        new_id = {q["q"]: q["id"] for q in new_qs}
        new_results = {}
        for old_qid, engines in data["results"].items():
            qtext = old_text.get(str(old_qid))
            nid = new_id.get(qtext)
            if nid is not None:
                new_results[str(nid)] = engines
        # ④ 중앙 결과 병합 (같은 질문·엔진이면 최신 ts 승 — 로컬이 최신이면 유지)
        rows = _supa("GET", "/rest/v1/ai_results?select=question_id,engine,ts,data"
                     "&brand=eq." + brand + "&limit=5000") or []
        central_res = {}
        for r in rows:
            qid = str(r["question_id"])
            eng = r["engine"]
            central_res[(qid, eng)] = r
            local = new_results.get(qid, {}).get(eng)
            if local is None or (r.get("ts") or "") > (local.get("ts") or ""):
                new_results.setdefault(qid, {})[eng] = r.get("data") or {}
        # ⑤ 로컬이 더 최신이거나 중앙에 없는 결과 → 역업로드 (오프라인 측정분 복구)
        upload = []
        for qid, engines in new_results.items():
            for eng, res in engines.items():
                c = central_res.get((qid, eng))
                if c is None or (res.get("ts") or "") > (c.get("ts") or ""):
                    upload.append((qid, eng, res))
        data["questions"] = new_qs
        data["results"] = new_results
        jsave(DATA_FILE, data)
    # 업로드는 락 밖에서 (네트워크가 느려도 UI 안 막게)
    for qid, eng, res in upload:
        try:
            _supa_push_result(int(qid), eng, res)
        except Exception:
            pass
    _SYNC["ok"] = True
    return True


# ── 측정 실행 상태 (스레드 1개만) ─────────────────────────────
RUN = {"active": False, "stop": False, "cur_q": "", "cur_i": 0, "cur_n": 0,
       "round": 0, "round_total": 0, "msg": "", "blocked": False, "engine": ""}
_lock = threading.Lock()


def _save_result(qid, engine_key, res):
    res["ts"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    for r in res.get("rounds", []):        # 원문 4000자까지 보관 (증거 열람용)
        if r.get("text"):
            r["text"] = r["text"][:4000]
    with _lock:
        data = load_data()
        data["results"].setdefault(str(qid), {})[engine_key] = res
        jsave(DATA_FILE, data)
    # 중앙 DB로 전송 (실패해도 무시 — 로컬 저장은 위에서 완료)
    cat = next((q["cat"] for q in data["questions"] if q["id"] == qid), "")

    def _push():
        _supa_push(res.get("question", ""), cat, engine_key, dict(res))   # 추이 로그
        _supa_push_result(qid, engine_key, dict(res))                     # 최신 결과 공유
    threading.Thread(target=_push, daemon=True).start()


def _worker(q_ids):
    data = load_data()
    st = load_settings()
    qmap = {q["id"]: q for q in data["questions"]}
    targets = [qmap[i] for i in q_ids if i in qmap]
    RUN.update({"active": True, "stop": False, "cur_i": 0, "cur_n": len(targets),
                "msg": "", "blocked": False, "engine": ""})
    try:
        for i, q in enumerate(targets):
            if RUN["stop"]:
                RUN["msg"] = "중단됨"
                break
            RUN.update({"cur_q": q["q"], "cur_i": i + 1,
                        "round": 0, "round_total": st["repeats"]})

            def prog(r, t):
                RUN.update({"round": r, "round_total": t})

            # 구글 AI개요·ChatGPT·Gemini(web)는 크롬확장이 측정 — 서버는 Gemini API만
            # ── Gemini API (gemini_mode=api일 때만) ──
            if st["use_gemini"] and st["gemini_mode"] == "api" and st["gemini_api_key"]:
                RUN["engine"] = "Gemini"
                try:
                    res = engine.measure_question_gemini(
                        q["q"], st["brand_aliases"], st["competitors"],
                        st["gemini_api_key"], repeats=st["repeats"],
                        on_progress=prog)
                    _save_result(q["id"], "gemini", res)
                    if res.get("error"):
                        RUN["msg"] = "Gemini: " + res["error"]
                        if "API 키" in res["error"]:
                            break   # 키가 틀리면 전부 실패 — 즉시 중단
                except Exception as e:
                    RUN["msg"] = "Gemini 오류: " + str(e)[:200]

            # (Gemini API만 돌므로 질문 간 대기 불필요)
    finally:
        RUN.update({"active": False, "engine": ""})


# ── API ───────────────────────────────────────────────────────
@app.get("/api/state")
def api_state():
    sync_central()                      # 15초마다 중앙과 동기화 (오프라인이면 로컬)
    data = load_data()
    return jsonify({"questions": data["questions"], "results": data["results"],
                    "settings": load_settings(), "run": RUN,
                    "central": _SYNC["ok"]})


@app.post("/api/questions")
def api_add_q():
    body = request.get_json(force=True)
    lines = [l.strip() for l in str(body.get("q", "")).splitlines() if l.strip()]
    cat = str(body.get("cat", "")).strip() or "기본"
    st = load_settings()
    central_ok = True
    for line in lines:
        r = _supa("POST", "/rest/v1/ai_questions?on_conflict=brand,q",
                  {"brand": st["brand_name"], "cat": cat, "q": line},
                  prefer="resolution=merge-duplicates,return=minimal")
        if r is None:
            central_ok = False
    if central_ok:
        sync_central(force=True)        # 중앙 반영분 즉시 내려받기
    else:
        # 오프라인 폴백: 로컬에만 저장 (다음 동기화 때 자동 업로드됨)
        with _lock:
            data = load_data()
            for line in lines:
                data["questions"].append({"id": data["next_id"], "cat": cat, "q": line})
                data["next_id"] += 1
            jsave(DATA_FILE, data)
    return jsonify(ok=True)


@app.post("/api/questions/delete")
def api_del_q():
    ids = set(request.get_json(force=True).get("ids", []))
    # 중앙에서 삭제 (질문 + 그 결과) — 모든 PC에서 사라짐
    for i in ids:
        _supa("DELETE", "/rest/v1/ai_questions?id=eq.%s" % i)
        _supa("DELETE", "/rest/v1/ai_results?question_id=eq.%s" % i)
    with _lock:
        data = load_data()
        data["questions"] = [q for q in data["questions"] if q["id"] not in ids]
        for i in ids:
            data["results"].pop(str(i), None)
        jsave(DATA_FILE, data)
    sync_central(force=True)
    return jsonify(ok=True)


@app.post("/api/settings")
def api_settings():
    body = request.get_json(force=True)
    s = jload(SETTINGS_FILE, {})
    for k in ("brand_name", "brand_aliases", "competitors", "own_domains",
              "repeats", "show_browser", "use_google_aio", "use_gemini",
              "use_chatgpt", "gemini_api_key", "gemini_mode"):
        if k in body:
            s[k] = body[k]
    jsave(SETTINGS_FILE, s)
    return jsonify(ok=True)


@app.post("/api/measure")
def api_measure():
    if RUN["active"]:
        return jsonify(ok=False, err="이미 측정 중")
    ids = request.get_json(force=True).get("ids", [])
    if not ids:
        return jsonify(ok=False, err="선택된 질문 없음")
    st = load_settings()
    if st["use_gemini"] and st["gemini_mode"] == "api":
        if not st["gemini_api_key"]:
            return jsonify(ok=False, err="Gemini API 키가 없음 — 설정에 입력 (또는 Gemini를 크롬확장 모드로)")
        threading.Thread(target=_worker, args=(ids,), daemon=True).start()
    # gemini가 web 모드면 서버는 할 일 없음 — 확장이 AI개요/ChatGPT/Gemini 다 측정
    return jsonify(ok=True)


@app.post("/api/stop")
def api_stop():
    RUN["stop"] = True
    return jsonify(ok=True)


def _save_shot(qid, ridx, data_url):
    """확장이 보낸 dataURL 스크린샷을 캡처/ 폴더에 저장, 파일명 반환"""
    import base64
    try:
        if not str(data_url).startswith("data:image"):
            return None
        b64 = data_url.split(",", 1)[1]
        os.makedirs(SHOTS_DIR, exist_ok=True)
        fname = "q%s_%s_r%d.jpg" % (qid, datetime.datetime.now().strftime("%m%d_%H%M%S"), ridx)
        with open(os.path.join(SHOTS_DIR, fname), "wb") as f:
            f.write(base64.b64decode(b64))
        return fname
    except Exception:
        return None


@app.get("/answer/<qid>/<eng>")
def api_answer(qid, eng):
    """답변 원문 뷰어 — 라운드별 전체 텍스트 + 출처 + 브랜드 형광펜"""
    import html as _html, re as _re
    data = load_data()
    st = load_settings()
    qmap = {str(q["id"]): q for q in data["questions"]}
    r = (data["results"].get(qid) or {}).get(eng)
    if not r or qid not in qmap:
        abort(404)
    names = [(a, "our") for a in st["brand_aliases"]] + \
            [(c, "comp") for c in st["competitors"]]

    def hl(text):
        out = _html.escape(text)
        for name, cls in names:
            if not name.strip():
                continue
            out = _re.sub("(" + _re.escape(_html.escape(name)) + ")",
                          '<mark class="%s">\\1</mark>' % cls, out,
                          flags=_re.IGNORECASE)
        return out.replace("\n", "<br>")

    eng_name = {"gemini": "Gemini", "chatgpt": "ChatGPT"}.get(eng, "구글 AI 개요")
    parts = ["""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>답변 원문 — %s</title><style>
body{font-family:'Segoe UI','Malgun Gothic',sans-serif;max-width:860px;margin:24px auto;padding:0 16px;color:#1a1c20;line-height:1.7}
h1{font-size:18px} .meta{color:#6b7280;font-size:13px;margin-bottom:20px}
.round{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:18px;margin-bottom:16px}
.rhead{font-weight:700;margin-bottom:8px;font-size:14px}
.ok{color:#059669}.no{color:#dc2626}
mark.our{background:#bbf7d0;font-weight:700;padding:1px 3px;border-radius:3px}
mark.comp{background:#fee2e2;padding:1px 3px;border-radius:3px}
.src{margin-top:10px;padding-top:10px;border-top:1px dashed #e5e7eb;font-size:12px}
.src a{color:#2563eb;text-decoration:none;display:block;margin:2px 0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.legend{font-size:12px;color:#6b7280;margin-bottom:14px}
</style></head><body>""" % _html.escape(eng_name)]
    parts.append("<h1>%s — 답변 원문</h1>" % _html.escape(qmap[qid]["q"]))
    parts.append('<div class="meta">%s · 측정 %s · %d라운드</div>'
                 % (eng_name, r.get("ts", ""), len(r.get("rounds", []))))
    parts.append('<div class="legend"><mark class="our">초록</mark> = 우리 브랜드 · '
                 '<mark class="comp">빨강</mark> = 경쟁사</div>')
    for i, rd in enumerate(r.get("rounds", []), 1):
        if rd.get("error"):
            body = '<span class="no">오류: %s</span>' % _html.escape(rd["error"])
        elif rd.get("shown") is False:
            body = '<span class="no">AI 개요 안 뜸</span>'
        else:
            body = hl(rd.get("text") or "(본문 없음)")
        badge = ('<span class="ok">✓ 노출 (%s위)</span>' % rd.get("rank")
                 if rd.get("exposed") else '<span class="no">노출 안 됨</span>')
        shot = (' · <a href="/shots/%s" target="_blank">📷 화면 캡처</a>' % rd["shot"]
                if rd.get("shot") else "")
        if eng == "gemini" and rd.get("tmp") is not None:
            shot += (' · <span style="color:#059669;font-size:12px">🕶 임시채팅(기록 안 남음)</span>'
                     if rd["tmp"] else
                     ' · <span style="color:#d97706;font-size:12px">⚠ 일반채팅(계정 기록 남음)</span>')
        links = rd.get("links") or []
        src = ""
        if links:
            src = ('<div class="src"><b>참고한 출처 %d개</b>' % len(links)
                   + "".join('<a href="%s" target="_blank" rel="noreferrer">%s</a>'
                             % (_html.escape(l.get("u", "")),
                                _html.escape((l.get("t") or l.get("u", ""))[:90]))
                             for l in links) + "</div>")
        parts.append('<div class="round"><div class="rhead">라운드 %d — %s%s</div>%s%s</div>'
                     % (i, badge, shot, body, src))
    parts.append("</body></html>")
    return "".join(parts)


@app.get("/gallery/<qid>/<eng>")
def api_gallery(qid, eng):
    """라운드별 캡처 갤러리 — 📷 하나 누르면 전부 보임"""
    import html as _html
    data = load_data()
    qmap = {str(q["id"]): q for q in data["questions"]}
    r = (data["results"].get(qid) or {}).get(eng)
    if not r or qid not in qmap:
        abort(404)
    eng_name = {"gemini": "Gemini", "chatgpt": "ChatGPT"}.get(eng, "구글 AI 개요")
    parts = ["""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>캡처 — %s</title><style>
body{font-family:'Segoe UI','Malgun Gothic',sans-serif;max-width:1000px;margin:24px auto;padding:0 16px;color:#1a1c20;background:#f6f7f9}
h1{font-size:18px} .meta{color:#6b7280;font-size:13px;margin-bottom:18px}
.shot{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:14px;margin-bottom:18px}
.shead{font-weight:700;margin-bottom:10px;font-size:14px;display:flex;gap:10px;align-items:center}
.ok{color:#059669}.no{color:#dc2626}
img{max-width:100%%;border:1px solid #e5e7eb;border-radius:6px}
a{color:#2563eb}
</style></head><body>""" % _html.escape(eng_name)]
    parts.append("<h1>📷 %s — 캡처 모아보기</h1>" % _html.escape(qmap[qid]["q"]))
    parts.append('<div class="meta">%s · 측정 %s · <a href="/answer/%s/%s">📄 답변 원문 보기</a></div>'
                 % (eng_name, r.get("ts", ""), qid, eng))
    n_shown = 0
    for i, rd in enumerate(r.get("rounds", []), 1):
        badge = ('<span class="ok">✓ 노출 (%s위)</span>' % rd.get("rank")
                 if rd.get("exposed") else '<span class="no">노출 안 됨</span>')
        if rd.get("shot"):
            n_shown += 1
            parts.append('<div class="shot"><div class="shead">라운드 %d %s</div>'
                         '<img src="/shots/%s" loading="lazy" '
                         'onerror="this.outerHTML=\'<i style=color:#9ca3af>캡처 파일은 측정한 PC에만 저장돼 있습니다</i>\'"></div>'
                         % (i, badge, rd["shot"]))
        else:
            reason = rd.get("shot_err") or ("측정 시 캡처 미지원 버전" if rd.get("shown") else "AI 답변 없음")
            parts.append('<div class="shot"><div class="shead">라운드 %d %s '
                         '<span style="color:#9ca3af;font-weight:400;font-size:12px">캡처 없음 — %s</span></div></div>'
                         % (i, badge, _html.escape(str(reason)[:80])))
    if n_shown == 0:
        parts.append('<p style="color:#6b7280">저장된 캡처가 없습니다. 확장 v1.8+로 새로 측정하면 채워집니다.</p>')
    parts.append("</body></html>")
    return "".join(parts)


@app.get("/shots/<path:fname>")
def api_shot(fname):
    from flask import send_from_directory
    fname = os.path.basename(fname)   # 경로 탈출 방지
    return send_from_directory(SHOTS_DIR, fname)


@app.post("/api/ext_result")
def api_ext_result():
    """크롬확장이 보낸 측정 라운드 수신 → 판정·집계·캡처저장"""
    body = request.get_json(force=True)
    qid = body.get("id")
    st = load_settings()
    data = load_data()
    qmap = {q["id"]: q for q in data["questions"]}
    if qid not in qmap:
        return jsonify(ok=False, err="unknown question id")
    eng = body.get("eng", "google_aio")
    if eng not in ("google_aio", "chatgpt", "gemini"):
        return jsonify(ok=False, err="unknown engine")
    rounds = []
    for ridx, rd in enumerate(body.get("rounds", []), 1):
        text = str(rd.get("text", ""))
        j = engine.judge(text, st["brand_aliases"], st["competitors"])
        rounds.append({"shown": bool(rd.get("shown")), "blocked": False,
                       "error": rd.get("error", ""),
                       "text": text, "debug": rd.get("debug", ""),
                       "shot": _save_shot(qid, ridx, rd.get("shot")),
                       "shot_err": rd.get("shot_err", ""),
                       "expanded": rd.get("expanded"),
                       "btn_debug": rd.get("btn_debug", ""),
                       "links": [{"u": str(l.get("u", ""))[:300],
                                  "t": str(l.get("t", ""))[:80]}
                                 for l in (rd.get("links") or [])][:25],
                       "tmp": rd.get("tmp"),   # Gemini 임시채팅으로 측정됐나
                       "exposed": j["exposed"], "rank": j["rank"],
                       "order": j["order"], "n_brands": j["n_brands"]})
    res = engine.aggregate(qmap[qid]["q"], rounds)
    res["source"] = "extension"                       # 크롬확장 측정 표시
    res["mode"] = body.get("mode", "normal")          # incognito=중립 / normal=로그인세션
    res["ext_version"] = body.get("ver", "?")         # 어느 확장 버전으로 측정했나
    _save_result(qid, eng, res)
    return jsonify(ok=True)


@app.post("/api/reset_profile")
def api_reset_profile():
    """전용 크롬 프로필 삭제 = 쿠키/개인화 리셋"""
    import shutil
    try:
        shutil.rmtree(engine.PROFILE_DIR, ignore_errors=True)
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, err=str(e))


@app.get("/api/trend")
def api_trend():
    """중앙 DB에서 이 브랜드의 측정 이력 (추이 차트용)"""
    st = load_settings()
    rows = _supa("GET", "/rest/v1/ai_measurements"
                 "?select=created_at,engine,expose_rate,aio_rate,category,question,device"
                 "&brand=eq." + urllib.parse.quote(st["brand_name"])
                 + "&order=created_at.asc&limit=2000")
    if rows is None:
        return jsonify(ok=False, rows=[],
                       err="중앙 DB 연결 안 됨 (테이블 미생성 또는 오프라인)")
    return jsonify(ok=True, rows=rows)


@app.get("/api/export")
def api_export():
    data = load_data()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["카테고리", "질문",
                "AI개요 표시율%", "AI개요 노출률%", "AI개요 평균순위",
                "ChatGPT 노출률%", "ChatGPT 평균순위",
                "Gemini 노출률%", "Gemini 평균순위",
                "경쟁사(언급횟수)", "측정시각"])
    for q in data["questions"]:
        rs = data["results"].get(str(q["id"]), {})
        g, t, m = rs.get("google_aio"), rs.get("chatgpt"), rs.get("gemini")
        comp = {}
        for r in (g, t, m):
            for name, cnt in (r or {}).get("competitors_seen", []):
                comp[name] = comp.get(name, 0) + cnt
        comps = " / ".join("%s(%d)" % kv for kv in
                           sorted(comp.items(), key=lambda x: -x[1]))
        w.writerow([q["cat"], q["q"],
                    g["aio_rate"] if g else "", g["expose_rate"] if g else "",
                    (g.get("avg_rank") or "") if g else "",
                    t["expose_rate"] if t else "",
                    (t.get("avg_rank") or "") if t else "",
                    m["expose_rate"] if m else "",
                    (m.get("avg_rank") or "") if m else "",
                    comps, (g or t or m or {}).get("ts", "")])
    out = "﻿" + buf.getvalue()   # BOM — 엑셀 한글 깨짐 방지
    fname = "AI노출체크_%s.csv" % datetime.datetime.now().strftime("%Y%m%d_%H%M")
    return Response(out, mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename*=UTF-8''" + fname})


# ── GUI ───────────────────────────────────────────────────────
HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<title>AI노출체크 — 구글 AI 개요</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1a1c20;--sub:#6b7280;--line:#e5e7eb;
--blue:#2563eb;--green:#059669;--red:#dc2626;--amber:#d97706}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI','Malgun Gothic',sans-serif;background:var(--bg);color:var(--ink);padding:24px}
h1{font-size:20px;margin-bottom:4px}
.sub{color:var(--sub);font-size:13px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:14px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
input,textarea,select{border:1px solid var(--line);border-radius:6px;padding:7px 10px;font-size:13px;font-family:inherit}
textarea{width:100%;min-height:54px}
button{border:0;border-radius:6px;padding:8px 14px;font-size:13px;cursor:pointer;background:#eef1f5}
button.pri{background:var(--blue);color:#fff}
button.warn{background:#fee2e2;color:var(--red)}
button:disabled{opacity:.45;cursor:default}
.tab{background:#eef1f5;border-radius:8px 8px 0 0;padding:8px 16px;font-size:13px;color:#6b7280}
.tab.on{background:var(--blue);color:#fff;font-weight:700}
.tab span{opacity:.7;font-size:11px;margin-left:4px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--sub);font-weight:600;font-size:12px;white-space:nowrap}
.pct{font-weight:700}
.g{color:var(--green)}.r{color:var(--red)}.a{color:var(--amber)}
.badge{display:inline-block;background:#eef1f5;border-radius:4px;padding:1px 7px;font-size:11px;color:var(--sub);margin-right:4px}
#status{font-size:13px;color:var(--sub)}
#status.on{color:var(--blue);font-weight:600}
#status.blocked{color:var(--red);font-weight:600}
.order{font-size:12px;color:var(--sub)}
.order b{color:var(--blue)}
label{font-size:13px;color:var(--sub)}
.settings-grid{display:grid;grid-template-columns:110px 1fr;gap:8px;align-items:center;max-width:640px}
</style></head><body>
<h1>🤖 AI노출체크 <span style="font-weight:400;font-size:14px;color:#6b7280">구글 AI 개요 + Gemini</span></h1>
<div class="sub">질문을 넣으면 AI 답변에 우리 브랜드가 노출되는지 자동 체크 · GPT 추가 예정</div>

<div class="card">
 <div class="settings-grid">
  <label>브랜드</label><input id="brand" placeholder="하오팩토리">
  <label>브랜드 별칭</label><input id="aliases" placeholder="하오팩토리, haofactory (쉼표 구분)">
  <label>경쟁사</label><input id="comps" placeholder="마롱컴퍼니, 예본조형 (쉼표 구분)">
  <label>우리 도메인</label><input id="ownd" placeholder="haodesign.co.kr, blog.naver.com/아이디 (쉼표 구분 — 출처 분석용)">
  <label>Gemini API 키</label>
  <div class="row"><input id="gemkey" type="password" placeholder="AIza... (aistudio.google.com에서 무료 발급)" style="flex:1">
   <button onclick="gemkey.type=gemkey.type==='password'?'text':'password'">👁</button></div>
  <label>사용 엔진</label>
  <div class="row">
   <label><input type="checkbox" id="useaio" checked> 구글 AI 개요 (크롬확장·시크릿)</label>
   <label><input type="checkbox" id="usegpt" checked> ChatGPT (크롬확장·임시채팅)</label>
   <label><input type="checkbox" id="usegem" checked> Gemini</label>
   <select id="gemmode" title="Gemini 측정 방식">
    <option value="web">크롬확장 (로그인 세션)</option>
    <option value="api">API (키 필요)</option>
   </select>
  </div>
  <label>반복 횟수</label>
  <div class="row"><select id="repeats"><option>1</option><option selected>2</option><option>3</option><option>5</option></select>
   <button onclick="saveSettings()">설정 저장</button></div>
 </div>
</div>

<div class="card" id="dash" style="display:none">
 <div class="row" style="margin-bottom:12px">
  <span style="font-weight:700">📊 대시보드<span id="dashCat" style="color:#2563eb"></span></span>
  <button onclick="load().then(()=>flashDash())" style="padding:4px 10px;font-size:12px">🔄 새로고침</button>
  <span id="dashTime" style="font-size:11px;color:#9ca3af"></span>
  <span style="flex:1"></span>
  <span style="font-size:11px;color:#9ca3af">2초마다 자동 갱신됨</span>
 </div>
 <div id="dashStats" style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px"></div>
 <div style="display:flex;gap:28px;flex-wrap:wrap;align-items:flex-start">
  <div id="dashDonuts" style="display:flex;gap:20px"></div>
  <div style="min-width:250px;flex:1">
   <div style="font-size:12px;font-weight:600;color:#374151;margin-bottom:8px">🔗 우리 채널 인용 <span style="font-weight:400;color:#9ca3af">노출된 답변의 출처</span></div>
   <div id="dashOwn"></div>
  </div>
  <div style="min-width:250px;flex:1">
   <div style="font-size:12px;font-weight:600;color:#374151;margin-bottom:8px">🌐 인용 출처 TOP <span style="font-weight:400;color:#9ca3af">AI가 참고한 사이트</span></div>
   <div id="dashDom"></div>
  </div>
 </div>
 <div style="margin-top:16px">
  <div style="font-size:12px;font-weight:600;color:#374151;margin-bottom:6px">📈 노출률 추이 <span style="font-weight:400;color:#9ca3af">중앙 DB — 모든 PC 측정 합산</span></div>
  <div id="dashTrend" style="font-size:12px;color:#9ca3af">불러오는 중...</div>
 </div>
 <div id="dashNote" style="font-size:11px;color:#9ca3af;margin-top:10px"></div>
</div>

<div class="card">
 <div class="row" style="margin-bottom:8px">
  <input id="newcat" placeholder="카테고리" style="width:120px">
  <textarea id="newq" placeholder="질문 입력 (여러 줄 = 여러 질문)&#10;예: 조형물 제작 업체 추천"></textarea>
 </div>
 <div class="row">
  <button onclick="addQ()">+ 질문 추가</button>
  <span style="flex:1"></span>
  <button class="pri" id="btnAll" onclick="measureAll()">▶ 전체 측정</button>
  <button class="pri" id="btnSel" onclick="measureSel()">▶ 선택 측정</button>
  <button class="warn" onclick="stopRun()">■ 중단</button>
  <button onclick="delSel()">선택 삭제</button>
  <button onclick="location.href='/api/export'">CSV 내보내기</button>
 </div>
 <div id="status" style="margin-top:8px">대기 중</div>
 <div id="extstatus" style="margin-top:4px;font-size:13px;color:#6b7280">🧩 확장: 연결 안 됨 — 확장 설치 후 이 페이지 새로고침</div>
 <div id="syncstatus" style="margin-top:4px;font-size:13px;color:#6b7280">☁ 중앙 동기화 확인 중...</div>
</div>

<div class="card">
<div id="cattabs" class="row" style="margin-bottom:10px;gap:4px"></div>
<table id="tbl">
 <thead><tr>
  <th><input type="checkbox" id="chkAll" onclick="toggleAll(this)"></th>
  <th>카테고리</th><th>질문</th>
  <th style="border-left:2px solid #d1d5db">구글 AI개요<br><span style="font-weight:400">표시횟수 · 노출횟수 · 순위</span></th>
  <th style="border-left:2px solid #d1d5db">ChatGPT<br><span style="font-weight:400">노출횟수 · 순위</span></th>
  <th style="border-left:2px solid #d1d5db">Gemini<br><span style="font-weight:400">노출횟수 · 순위</span></th>
  <th style="border-left:2px solid #d1d5db">마지막 언급 순서</th><th>경쟁사 (언급횟수)</th><th>측정시각</th><th></th>
 </tr></thead>
 <tbody id="tbody"></tbody>
</table>
</div>

<script>
let STATE=null;
// ── 카테고리 탭 ──────────────────────────────────────────
let CATS=['전체'], curCat='전체';
function renderTabs(){
  CATS=['전체',...new Set(STATE.questions.map(q=>q.cat))];
  if(!CATS.includes(curCat))curCat='전체';
  const el=document.getElementById('cattabs');
  el.innerHTML=CATS.map((c,i)=>{
    const n=c==='전체'?STATE.questions.length:STATE.questions.filter(q=>q.cat===c).length;
    return '<button class="tab'+(c===curCat?' on':'')+'" data-i="'+i+'">'
      +c.replace(/</g,'&lt;')+' <span>'+n+'</span></button>';
  }).join('');
  el.querySelectorAll('.tab').forEach(b=>b.addEventListener('click',()=>{
    curCat=CATS[+b.dataset.i]; renderTabs(); renderTable(); renderDash();
  }));
}
function visibleQs(){
  return curCat==='전체'?STATE.questions:STATE.questions.filter(q=>q.cat===curCat);
}

async function load(){
  STATE = await (await fetch('/api/state')).json();
  renderSettings(); renderTabs(); renderTable(); renderStatus(); renderDash();
  loadTrend(false);
  const t=document.getElementById('dashTime');
  if(t)t.textContent='마지막 갱신 '+new Date().toLocaleTimeString('ko-KR');
  const sy=document.getElementById('syncstatus');
  if(sy){
    if(STATE.central===true){sy.style.color='#059669';sy.textContent='☁ 중앙 동기화 ON — 모든 PC가 같은 질문·결과를 봅니다';}
    else if(STATE.central===false){sy.style.color='#d97706';sy.textContent='☁ 중앙 DB 연결 안 됨 — 로컬 모드 (테이블 미생성 또는 오프라인)';}
  }
}
// ── 📈 추이 차트 (중앙 DB) — 60초 캐시 ──
let TREND=null, trendAt=0;
async function loadTrend(force){
  if(!force && TREND && Date.now()-trendAt<60000) { renderTrend(); return; }
  try{
    const r=await (await fetch('/api/trend')).json();
    TREND=r; trendAt=Date.now();
  }catch(e){ TREND={ok:false,err:'연결 실패'}; }
  renderTrend();
}
function renderTrend(){
  const el=document.getElementById('dashTrend');
  if(!el||!TREND)return;
  if(!TREND.ok){el.innerHTML='⚠ '+(TREND.err||'중앙 DB 연결 안 됨');return;}
  // 현재 카테고리 필터 + 날짜·엔진별 평균 노출률
  let rows=TREND.rows||[];
  if(curCat!=='전체')rows=rows.filter(r=>r.category===curCat);
  if(!rows.length){el.innerHTML='아직 기록 없음 — 측정하면 자동으로 쌓입니다';return;}
  const agg={};   // {date:{engine:[rates]}}
  for(const r of rows){
    const d=(r.created_at||'').slice(0,10);
    if(!d||r.expose_rate==null)continue;
    (((agg[d]=agg[d]||{})[r.engine]=agg[d][r.engine]||[])).push(r.expose_rate);
  }
  const dates=Object.keys(agg).sort();
  const engs={google_aio:['구글 AI개요','#2563eb'],chatgpt:['ChatGPT','#10a37f'],gemini:['Gemini','#7c3aed']};
  const W=460,H=120,PL=30,PB=18;
  let svg='<svg width="'+W+'" height="'+H+'" style="background:#fff;border:1px solid #e5e7eb;border-radius:8px">';
  // y축 눈금 0/50/100
  for(const v of [0,50,100]){
    const y=(H-PB)-(v/100)*(H-PB-10);
    svg+='<line x1="'+PL+'" y1="'+y+'" x2="'+W+'" y2="'+y+'" stroke="#f1f5f9"/>'
       +'<text x="'+(PL-4)+'" y="'+(y+3)+'" text-anchor="end" font-size="9" fill="#9ca3af">'+v+'</text>';
  }
  const x=(i)=>dates.length<2?W/2:PL+8+i*(W-PL-16)/(dates.length-1);
  const y=(v)=>(H-PB)-(v/100)*(H-PB-10);
  let legend='';
  for(const [key,[name,color]] of Object.entries(engs)){
    const pts=dates.map((d,i)=>{
      const arr=(agg[d]||{})[key];
      return arr?{i,v:arr.reduce((a,b)=>a+b,0)/arr.length}:null;
    }).filter(Boolean);
    if(!pts.length)continue;
    svg+='<polyline fill="none" stroke="'+color+'" stroke-width="2" points="'
       +pts.map(p=>x(p.i)+','+y(p.v)).join(' ')+'"/>';
    for(const p of pts)svg+='<circle cx="'+x(p.i)+'" cy="'+y(p.v)+'" r="3" fill="'+color+'"/>';
    legend+='<span style="color:'+color+';margin-right:10px">● '+name+'</span>';
  }
  // x축 날짜 (처음/끝만)
  if(dates.length){
    svg+='<text x="'+x(0)+'" y="'+(H-4)+'" font-size="9" fill="#9ca3af" text-anchor="middle">'+dates[0].slice(5)+'</text>';
    if(dates.length>1)svg+='<text x="'+x(dates.length-1)+'" y="'+(H-4)+'" font-size="9" fill="#9ca3af" text-anchor="middle">'+dates[dates.length-1].slice(5)+'</text>';
  }
  svg+='</svg>';
  el.innerHTML=svg+'<div style="margin-top:4px;font-size:11px">'+legend
    +'<span style="color:#9ca3af">· 총 '+rows.length+'회 측정</span></div>';
}
function flashDash(){
  const d=document.getElementById('dash');
  d.style.transition='background .15s'; d.style.background='#eff6ff';
  setTimeout(()=>{d.style.background='';},300);
}

// ── 📊 대시보드 ──────────────────────────────────────────
function channelOf(s){
  s=s.toLowerCase();
  if(s.includes('blog.naver'))return '네이버 블로그';
  if(s.includes('cafe.naver'))return '네이버 카페';
  if(s.includes('tistory'))return '티스토리';
  if(s.includes('blogspot')||s.includes('blogger'))return '구글 블로거';
  if(s.includes('instagram'))return '인스타그램';
  if(s.includes('youtube')||s.includes('youtu.be'))return '유튜브';
  if(s.includes('facebook'))return '페이스북';
  if(s.includes('threads'))return '스레드';
  if(s.includes('brunch'))return '브런치';
  return '홈페이지/웹';
}
function bar(label,count,max,color){
  const p=Math.max(3,Math.round(count/max*100));
  return '<div style="margin:6px 0">'
    +'<div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:2px">'
    +'<span style="max-width:170px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'+label+'</span><b>'+count+'</b></div>'
    +'<div style="height:10px;background:#eef1f5;border-radius:5px;overflow:hidden">'
    +'<div style="height:100%;width:'+p+'%;background:'+color+';border-radius:5px"></div></div></div>';
}
function donut(a,b,color,title,sub){
  // a/b 도넛 차트 (SVG)
  const C=2*Math.PI*34, f=b?a/b:0, dash=(f*C).toFixed(1);
  return '<div style="text-align:center">'
    +'<svg width="96" height="96" viewBox="0 0 96 96">'
    +'<circle cx="48" cy="48" r="34" fill="none" stroke="#eef1f5" stroke-width="11"/>'
    +'<circle cx="48" cy="48" r="34" fill="none" stroke="'+color+'" stroke-width="11" stroke-linecap="round" '
    +'stroke-dasharray="'+dash+' '+C.toFixed(1)+'" transform="rotate(-90 48 48)"/>'
    +'<text x="48" y="45" text-anchor="middle" font-size="17" font-weight="800" fill="#1a1c20">'+a+'/'+b+'</text>'
    +'<text x="48" y="61" text-anchor="middle" font-size="10" fill="#6b7280">'+(b?Math.round(f*100):0)+'%</text>'
    +'</svg>'
    +'<div style="font-size:12px;font-weight:600;margin-top:2px">'+title+'</div>'
    +'<div style="font-size:11px;color:#9ca3af">'+sub+'</div></div>';
}
function statCard(n,label,color){
  return '<div style="background:#f8fafc;border:1px solid #e5e7eb;border-radius:10px;padding:10px 18px;min-width:110px">'
    +'<div style="font-size:24px;font-weight:800;color:'+(color||'#1a1c20')+'">'+n+'</div>'
    +'<div style="font-size:12px;color:#6b7280">'+label+'</div></div>';
}
function renderDash(){
  const dash=document.getElementById('dash');
  const res=STATE.results, qs=visibleQs();   // 선택된 카테고리 기준
  const dc=document.getElementById('dashCat');
  if(dc)dc.textContent=curCat==='전체'?'':' — '+curCat;
  const own=(STATE.settings.own_domains||[]).map(x=>x.toLowerCase()).filter(Boolean);
  const aliases=(STATE.settings.brand_aliases||[]).map(x=>x.toLowerCase().replace(/\\s+/g,''));
  let measured=0,aioN=0,aioExp=0,gemN=0,gemExp=0,gptN=0,gptExp=0,linkRounds=0;
  const chOwn={},domAll={};
  for(const q of qs){
    const r=res[q.id]||{};
    if(r.google_aio||r.gemini||r.chatgpt)measured++;
    if(r.google_aio){aioN++;if(r.google_aio.expose_rate>0)aioExp++;}
    if(r.gemini){gemN++;if(r.gemini.expose_rate>0)gemExp++;}
    if(r.chatgpt){gptN++;if(r.chatgpt.expose_rate>0)gptExp++;}
    for(const rr of Object.values(r)){
      for(const rd of (rr.rounds||[])){
        if(!rd.exposed)continue;               // 노출된 답변의 출처만 분석
        const links=rd.links||[];
        if(links.length)linkRounds++;
        for(const l of links){
          const s=((l.u||'')+' '+(l.t||'')).toLowerCase();
          // 도메인 집계 (Gemini 리다이렉트 주소는 제목으로 대체)
          let dom='';
          try{dom=new URL(l.u).hostname.replace(/^www\\./,'');}catch(e){}
          if(!dom||dom.includes('vertexaisearch'))dom=(l.t||'?').toLowerCase();
          domAll[dom]=(domAll[dom]||0)+1;
          // 우리 채널인가 (도메인 or 브랜드명 포함)
          if(own.some(o=>s.includes(o))||aliases.some(a=>a&&s.includes(a))){
            const c=channelOf(s);
            chOwn[c]=(chOwn[c]||0)+1;
          }
        }
      }
    }
  }
  if(!measured){dash.style.display='none';return;}
  dash.style.display='';
  const engs=[['구글 AI개요',aioExp,aioN],['ChatGPT',gptExp,gptN],['Gemini',gemExp,gemN]];
  const withData=engs.filter(e=>e[2]>0);
  const best=withData.length?withData.slice().sort((a,b)=>b[1]/b[2]-a[1]/a[2])[0][0]:'-';
  document.getElementById('dashStats').innerHTML=
    statCard(measured+'/'+qs.length,'측정된 질문')
    +statCard(aioExp+gptExp+gemExp,'노출 잡힌 횟수(질문×엔진)', '#059669')
    +statCard(best,'더 잘 잡히는 엔진','#2563eb');
  const engColors={'구글 AI개요':'#2563eb','ChatGPT':'#10a37f','Gemini':'#7c3aed'};
  document.getElementById('dashDonuts').innerHTML=
    engs.map(e=>donut(e[1],e[2],engColors[e[0]],e[0],'노출된 질문')).join('');
  const chColors={'홈페이지/웹':'#2563eb','네이버 블로그':'#03c75a','티스토리':'#ff5544',
    '구글 블로거':'#f4b400','인스타그램':'#d62976','유튜브':'#ff0000',
    '네이버 카페':'#03c75a','페이스북':'#1877f2','브런치':'#00c6be','스레드':'#333'};
  const ownEntries=Object.entries(chOwn).sort((a,b)=>b[1]-a[1]);
  const maxOwn=Math.max(1,...ownEntries.map(e=>e[1]));
  document.getElementById('dashOwn').innerHTML=ownEntries.length
    ?ownEntries.map(e=>bar(e[0],e[1],maxOwn,chColors[e[0]]||'#2563eb')).join('')
    :'<span style="font-size:12px;color:#9ca3af">아직 없음 — 우리 도메인/브랜드가 출처로 안 잡힘</span>';
  const domEntries=Object.entries(domAll).sort((a,b)=>b[1]-a[1]).slice(0,8);
  const maxDom=Math.max(1,...domEntries.map(e=>e[1]));
  const isOwnDom=(dm)=>own.some(o=>dm.includes(o))||aliases.some(a=>a&&dm.includes(a));
  document.getElementById('dashDom').innerHTML=domEntries.length
    ?domEntries.map(e=>bar((isOwnDom(e[0])?'⭐ ':'')+e[0],e[1],maxDom,isOwnDom(e[0])?'#059669':'#94a3b8')).join('')
    :'<span style="font-size:12px;color:#9ca3af">출처 데이터 없음</span>';
  document.getElementById('dashNote').textContent=
    linkRounds?'출처 분석은 노출된 답변 기준. 확장 v1.5+/새 측정부터 출처가 수집됩니다.'
    :'⚠ 출처 데이터가 아직 없어요 — 확장 업데이트(v1.5) 후 새로 측정하면 채워집니다.';
}
let settingsDirty=false;   // 저장 안 한 변경 있으면 폴링이 덮어쓰지 않게
function renderSettings(){
  const s=STATE.settings;
  if(settingsDirty) return;
  if(document.activeElement && ['brand','aliases','comps','gemkey','ownd'].includes(document.activeElement.id)) return;
  brand.value=s.brand_name; aliases.value=s.brand_aliases.join(', ');
  comps.value=s.competitors.join(', '); repeats.value=s.repeats;
  ownd.value=(s.own_domains||[]).join(', ');
  gemkey.value=s.gemini_api_key; usegem.checked=s.use_gemini; usegpt.checked=s.use_chatgpt;
  useaio.checked=s.use_google_aio; gemmode.value=s.gemini_mode||'web';
}
function pct(v,inv){ if(v===''||v==null)return '';
  const cls = inv? (v>=50?'r':'g') : (v>=50?'g':(v>0?'a':'r'));
  return '<span class="pct '+cls+'">'+v+'%</span>'; }
function counts(r){
  // 라운드에서 직접 세기: 총 몇 번 측정, 몇 번 표시, 몇 번 노출
  const rs=(r.rounds||[]).filter(x=>!x.blocked&&!x.error);
  return {n:rs.length,
          shown:rs.filter(x=>x.shown!==false).filter(x=>x.shown===true||x.shown==null).length,
          shownStrict:rs.filter(x=>x.shown===true).length,
          exp:rs.filter(x=>x.exposed).length};
}
function frac(a,b,goodHigh){
  const cls=b===0?'':(a/b>=0.5?'g':(a>0?'a':'r'));
  return '<b class="'+cls+'">'+a+'</b>/'+b;
}
function aioCell(r){
  if(!r) return '<td style="border-left:2px solid #e5e7eb"></td>';
  const c=counts(r);
  const blocked=r.blocked?' <span class="r" title="구글 차단으로 일부 라운드 무효">⚠</span>':'';
  const src=r.source==='extension'
    ?(r.mode==='incognito'
      ?' <span class="badge" title="크롬확장·시크릿 탭 — 중립(개인화 없음) 측정">🕶</span>'
      :' <span class="badge" title="크롬확장·일반 탭 — 로그인 세션 측정">🧩</span>')
    :' <span class="badge" title="자동화 브라우저 측정 — AI개요 억제될 수 있음">🤖</span>';
  const nShots=(r.rounds||[]).filter(rd=>rd.shot).length;
  const shots=nShots?' <a href="/gallery/'+r._qid+'/google_aio" target="_blank" title="화면 캡처 '+nShots+'장 모아보기" style="text-decoration:none">📷<sub style="font-size:9px">'+nShots+'</sub></a>':'';
  return '<td style="border-left:2px solid #e5e7eb;white-space:nowrap" title="'+c.n+'번 검색 → AI개요 '+c.shownStrict+'번 뜸 → 그중 '+c.exp+'번 우리 언급">'
    +'표시 '+frac(c.shownStrict,c.n)+' · 노출 '+frac(c.exp,c.shownStrict)
    +(r.avg_rank?' · <b>'+r.avg_rank+'위</b>':'')+blocked+src+shots
    +' <a href="/answer/'+r._qid+'/google_aio" target="_blank" title="답변 원문 보기" style="text-decoration:none">📄</a></td>';
}
function askCell(r,eng){
  // ChatGPT/Gemini 공용 (질문하면 항상 답하는 엔진)
  if(!r) return '<td style="border-left:2px solid #e5e7eb"></td>';
  if(r.error) return '<td style="border-left:2px solid #e5e7eb" class="r" title="'+r.error.replace(/"/g,'&quot;')+'">오류 ⚠</td>';
  const c=counts(r);
  const errRounds=(r.rounds||[]).filter(x=>x.error);
  // 전 라운드가 실패면 숫자 대신 원인을 대놓고 표시
  if(errRounds.length && c.n===0){
    const msg=(errRounds[0].error||'').split('—')[0].trim();
    return '<td style="border-left:2px solid #e5e7eb" class="r" title="'+errRounds[0].error.replace(/"/g,'&quot;')+'">'
      +'⚠ '+msg.slice(0,28)
      +' <a href="/answer/'+r._qid+'/'+eng+'" target="_blank" style="text-decoration:none">📄</a></td>';
  }
  const errBadge=errRounds.length?' <span class="a" title="'+errRounds.map(x=>x.error).join(String.fromCharCode(10)).replace(/"/g,'&quot;')+'">⚠'+errRounds.length+'</span>':'';
  const nShots=(r.rounds||[]).filter(rd=>rd.shot).length;
  const shots=nShots?' <a href="/gallery/'+r._qid+'/'+eng+'" target="_blank" title="화면 캡처 '+nShots+'장 모아보기" style="text-decoration:none">📷<sub style="font-size:9px">'+nShots+'</sub></a>':'';
  return '<td style="border-left:2px solid #e5e7eb;white-space:nowrap" title="'+c.n+'번 질문 → '+c.exp+'번 우리 언급">'
    +'노출 '+frac(c.exp,c.n)
    +(r.avg_rank?' · <b>'+r.avg_rank+'위</b>':'')+errBadge+shots
    +' <a href="/answer/'+r._qid+'/'+eng+'" target="_blank" title="답변 원문 + 참고 출처 보기" style="text-decoration:none">📄</a></td>';
}
function lastOrder(rs){
  // 가장 최근 측정된 엔진 결과에서 언급 순서 뽑기
  const engNames={gemini:'Gemini',chatgpt:'ChatGPT',google_aio:'AI개요'};
  for(const key of ['chatgpt','gemini','google_aio']){
    const r=rs[key]; if(!r) continue;
    const last=[...r.rounds].reverse().find(x=>(x.order||[]).length);
    if(last) return {order:last.order, eng:engNames[key]};
  }
  return null;
}
function renderTable(){
  const keep=new Set(selIds());          // 다시 그려도 선택 유지
  const keepAll=document.getElementById('chkAll').checked;
  const tb=document.getElementById('tbody'); tb.innerHTML='';
  for(const q of visibleQs()){
    const rs=STATE.results[q.id]||{};
    const g=rs.google_aio, m=rs.gemini, t=rs.chatgpt;
    if(g)g._qid=q.id; if(m)m._qid=q.id; if(t)t._qid=q.id;
    const lo=lastOrder(rs);
    const order=lo? '<span class="badge">'+lo.eng+'</span> '
      +lo.order.map(o=>o==='우리'?'<b>'+STATE.settings.brand_name+'</b>':o).join(' → '):'';
    const comp={};
    for(const r of [g,m,t]) for(const c of (r?.competitors_seen||[])) comp[c[0]]=(comp[c[0]]||0)+c[1];
    const comps=Object.entries(comp).sort((a,b)=>b[1]-a[1])
      .map(c=>'<span class="badge">'+c[0]+' '+c[1]+'</span>').join('');
    const ts=[g,m,t].filter(Boolean).map(r=>r.ts||'').sort().pop()||'';
    tb.insertAdjacentHTML('beforeend',
     '<tr><td><input type="checkbox" class="rowchk" value="'+q.id+'"></td>'+
     '<td><span class="badge">'+q.cat+'</span></td><td>'+q.q+'</td>'+
     aioCell(g)+askCell(t,'chatgpt')+askCell(m,'gemini')+
     '<td class="order" style="border-left:2px solid #e5e7eb">'+order+'</td>'+
     '<td>'+comps+'</td>'+
     '<td style="white-space:nowrap;color:#6b7280;font-size:12px">'+ts+'</td>'+
     '<td><button onclick="measureOne('+q.id+')">측정</button></td></tr>');
  }
  document.querySelectorAll('.rowchk').forEach(c=>{ if(keep.has(+c.value)) c.checked=true; });
  document.getElementById('chkAll').checked=keepAll;
}
function renderStatus(){
  const el=document.getElementById('status'), run=STATE.run;
  if(run.active){
    el.className='on';
    el.textContent='측정 중 ['+run.cur_i+'/'+run.cur_n+'] '+run.cur_q
      +(run.engine?' ('+run.engine+')':'')+' — 라운드 '+run.round+'/'+run.round_total
      +(run.msg?' · '+run.msg:'');
  } else if(run.blocked){ el.className='blocked'; el.textContent='⚠ '+run.msg; }
  else { el.className=''; el.textContent=run.msg||'대기 중'; }
  btnAll.disabled=btnSel.disabled=run.active;
}
async function saveSettings(){
  await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({brand_name:brand.value.trim(),
    brand_aliases:aliases.value.split(',').map(s=>s.trim()).filter(Boolean),
    competitors:comps.value.split(',').map(s=>s.trim()).filter(Boolean),
    own_domains:ownd.value.split(',').map(s=>s.trim()).filter(Boolean),
    repeats:+repeats.value, use_gemini:usegem.checked, use_chatgpt:usegpt.checked,
    use_google_aio:useaio.checked,
    gemini_mode:gemmode.value, gemini_api_key:gemkey.value.trim()})});
  settingsDirty=false;
  load();
}
// 체크박스·선택박스는 건드리면 즉시 자동 저장 (텍스트는 수정 중 표시 후 저장버튼)
for(const id of ['usegem','usegpt','useaio','gemmode','repeats'])
  document.getElementById(id).addEventListener('change',saveSettings);
for(const id of ['brand','aliases','comps','gemkey'])
  document.getElementById(id).addEventListener('input',()=>{settingsDirty=true;});
async function addQ(){
  if(!newq.value.trim())return;
  await fetch('/api/questions',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({cat:newcat.value,q:newq.value})});
  newq.value=''; load();
}
function selIds(){return [...document.querySelectorAll('.rowchk:checked')].map(c=>+c.value);}
function toggleAll(c){document.querySelectorAll('.rowchk').forEach(x=>x.checked=c.checked);}
async function delSel(){
  const ids=selIds(); if(!ids.length)return;
  if(!confirm(ids.length+'개 질문 삭제?'))return;
  await fetch('/api/questions/delete',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({ids})}); load();
}
// ── 크롬확장 브릿지 (구글 AI개요 측정) ──────────────────
let extReady=false, extRun=null, extVer='';
window.addEventListener('message',e=>{
  if(e.source!==window||!e.data)return;
  if(e.data.aicheck_ready){extReady=true;extVer=e.data.aicheck_version||'';renderExtStatus();}
  if(e.data.aicheck_status){extRun=e.data.aicheck_status;renderExtStatus();}
});
function renderExtStatus(){
  const el=document.getElementById('extstatus');
  if(!extReady){el.textContent='🧩 확장: 연결 안 됨 — 확장 설치 후 이 페이지 새로고침';el.style.color='#6b7280';return;}
  if(extRun&&extRun.active){
    el.style.color='#2563eb';
    el.textContent='🧩 측정 중 ['+extRun.i+'/'+extRun.n+'] '+extRun.msg;
  } else {
    el.style.color='#059669';
    el.textContent='🧩 확장 연결됨'+(extVer?' (v'+extVer+')':'')
      +(extRun&&extRun.msg&&extRun.msg!=='대기 중'?' — '+extRun.msg:'');
  }
}
function extStart(ids){
  if(!extReady)return false;
  window.postMessage({aicheck_cmd:'start',repeats:+repeats.value,ids,incognito:true},'*');
  return true;
}
async function _measure(ids){
  // 1) 서버 측정 (Gemini 등 API 엔진)
  const r=await (await fetch('/api/measure',{method:'POST',
   headers:{'Content-Type':'application/json'},body:JSON.stringify({ids})})).json();
  // 2) 확장 측정 (구글 AI개요 — 시크릿탭) 동시 시작
  const ext=extStart(ids);
  if(!r.ok&&!ext)alert('측정이 시작되지 않았습니다:\\n· 서버(Gemini): '+(r.err||'?')+'\\n· 확장(구글AI개요): 연결 안 됨 — 확장 v1.2 설치/새로고침 후 이 페이지 새로고침');
  else if(!ext)document.getElementById('extstatus').textContent='🧩 확장 연결 안 됨 — 구글AI개요는 측정 안 됨 (Gemini만 진행)';
  else if(!r.ok)document.getElementById('status').textContent='서버(Gemini) 안 돌아감: '+(r.err||'')+' — 확장(구글AI개요)만 진행';
  load();
}
function measureAll(){_measure(visibleQs().map(q=>q.id));}   // 현재 탭 기준
function measureSel(){const ids=selIds(); if(!ids.length)return alert('질문을 선택하세요'); _measure(ids);}
function measureOne(id){_measure([id]);}
async function stopRun(){
  await fetch('/api/stop',{method:'POST'});
  if(extReady)window.postMessage({aicheck_cmd:'stop'},'*');
}
async function resetProfile(){
  if(!confirm('전용 크롬 프로필(쿠키)을 리셋할까요?'))return;
  await fetch('/api/reset_profile',{method:'POST'}); alert('리셋 완료');
}
load(); setInterval(load,2000);
</script></body></html>"""


@app.get("/")
def index():
    return HTML


def _msgbox(msg, title="AI노출체크"):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x10)
    except Exception:
        print(title, "-", msg)


def main():
    url = "http://127.0.0.1:%d" % PORT
    # 이미 실행 중이면(중복 더블클릭) 브라우저만 열고 종료
    try:
        s = socket.socket()
        s.settimeout(1)
        busy = s.connect_ex(("127.0.0.1", PORT)) == 0
        s.close()
    except Exception:
        busy = False
    if busy:
        print("이미 실행 중 — 브라우저만 엽니다.")
        webbrowser.open(url)
        return
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print("=" * 46)
    print(" AI노출체크 실행됨!")
    print(" 브라우저가 자동으로 안 열리면 주소창에 입력:")
    print("   " + url)
    print(" (이 검은 창을 닫으면 프로그램이 꺼집니다)")
    print("=" * 46)
    try:
        app.run(host="127.0.0.1", port=PORT, debug=False)
    except Exception as e:
        _msgbox("서버 실행 오류:\n%s\n\n포트 %d를 다른 프로그램이 쓰고 있는지 확인하세요."
                % (str(e)[:300], PORT))
        raise


if __name__ == "__main__":
    main()
