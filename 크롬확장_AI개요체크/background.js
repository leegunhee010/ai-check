// AI개요 체크 — 백그라운드 오케스트레이터
// 앱(127.0.0.1:5630)에서 질문 받아 → 백그라운드 탭에서 구글 검색 →
// AI 개요 추출 → 앱으로 전송. 이 크롬(로그인 세션)이라 AI 개요가 잘 뜸.
const APP = "http://127.0.0.1:5630";

let run = { active: false, stop: false, i: 0, n: 0, round: 0, rt: 0, msg: "대기 중" };

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.cmd === "start") { startRun(msg.repeats || 1, msg.ids || null, !!msg.incognito); sendResponse({ ok: true }); }
  else if (msg.cmd === "stop") { run.stop = true; sendResponse({ ok: true }); }
  else if (msg.cmd === "status") sendResponse(run);
  return true;
});

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// 중단 즉시 반응용 대기: 0.5초 단위로 쪼개서 run.stop이면 바로 탈출
async function waitOrStop(ms) {
  for (let t = 0; t < ms; t += 500) {
    if (run.stop) return false;
    await sleep(Math.min(500, ms - t));
  }
  return !run.stop;
}

async function startRun(repeats, onlyIds, incognito) {
  if (run.active) return;
  run = { active: true, stop: false, i: 0, n: 0, round: 0, rt: repeats, msg: "질문 불러오는 중..." };
  try {
    const st = await (await fetch(APP + "/api/state")).json();
    let qs = st.questions;
    if (onlyIds) qs = qs.filter((q) => onlyIds.includes(q.id));
    run.n = qs.length;
    if (!qs.length) { run.msg = "질문 없음 — 앱에서 질문을 먼저 추가하세요"; run.active = false; return; }

    const useAio = !st.settings || st.settings.use_google_aio !== false;
    const useGpt = !!(st.settings && st.settings.use_chatgpt);
    const useGemWeb = !!(st.settings && st.settings.use_gemini
                         && st.settings.gemini_mode === "web");

    const warns = [];   // 로그인 필요 등 — 완료 후에도 상태줄에 계속 표시
    const post = (qid, rounds, eng) => fetch(APP + "/api/ext_result", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: qid, rounds, eng,
                             mode: incognito ? "incognito" : "normal",
                             ver: chrome.runtime.getManifest().version }),
    });

    for (let qi = 0; qi < qs.length; qi++) {
      if (run.stop) { run.msg = "중단됨"; break; }
      const q = qs[qi];
      run.i = qi + 1;

      // ── 엔진 1: 구글 AI 개요 (설정에서 켰을 때만) ──
      if (useAio) {
        run.msg = "[AI개요] " + q.q;
        const rounds = [];
        for (let r = 0; r < repeats; r++) {
          if (run.stop) break;
          run.round = r + 1;
          const res = await searchOnce(q.q, incognito);
          rounds.push(res);
          if (res.error && res.error.includes("시크릿")) {
            run.msg = res.error;           // 시크릿 허용 안 켜짐 — 전체 중단
            run.stop = true;
            break;
          }
          if (r < repeats - 1 && !(await waitOrStop(8000 + Math.random() * 7000))) break; // 라운드 간 8~15초
        }
        if (rounds.length) await post(q.id, rounds, "google_aio");
      }

      // ── 엔진 2: ChatGPT (임시채팅 — 메모리 오염 없음) ──
      if (useGpt && !run.stop) {
        run.msg = "[ChatGPT] " + q.q;
        const gptRounds = [];
        let gptFatal = false;
        for (let r = 0; r < repeats; r++) {
          if (run.stop) break;
          run.round = r + 1;
          const res = await searchOnceGpt(q.q);
          gptRounds.push(res);
          if (res.error && (res.error.includes("로그인") || res.error.includes("차단"))) {
            run.msg = "ChatGPT: " + res.error;
            if (!warns.some((w) => w.startsWith("ChatGPT"))) warns.push("ChatGPT: " + res.error.split("—")[0].trim());
            gptFatal = true;             // 로그인/차단이면 반복해봐야 소용없음
            break;
          }
          if (r < repeats - 1 && !(await waitOrStop(6000 + Math.random() * 6000))) break;
        }
        if (gptRounds.length) await post(q.id, gptRounds, "chatgpt");
        if (gptFatal) { /* AI개요는 계속 진행 */ }
      }

      // ── 엔진 3: Gemini 웹 (구글 로그인 세션) ──
      if (useGemWeb && !run.stop) {
        run.msg = "[Gemini] " + q.q;
        const gemRounds = [];
        for (let r = 0; r < repeats; r++) {
          if (run.stop) break;
          run.round = r + 1;
          const res = await searchOnceGem(q.q);
          gemRounds.push(res);
          if (res.error && res.error.includes("로그인")) {
            run.msg = res.error;
            if (!warns.some((w) => w.startsWith("Gemini"))) warns.push("Gemini: 구글 로그인 필요");
            break;
          }
          if (r < repeats - 1 && !(await waitOrStop(6000 + Math.random() * 6000))) break;
        }
        if (gemRounds.length) await post(q.id, gemRounds, "gemini");
      }

      if (qi < qs.length - 1 && !(await waitOrStop(10000 + Math.random() * 10000))) { run.msg = "중단됨"; break; } // 질문 간 10~20초
    }
    if (!run.stop) {
      run.msg = warns.length
        ? "완료 — ⚠ " + warns.join(" / ")
        : "완료! 앱에서 결과 확인";
    }
  } catch (e) {
    run.msg = "오류: " + (e.message || e) + " — 앱(:5630)이 켜져 있나 확인";
  }
  run.active = false;
}

async function searchOnce(query, incognito) {
  const url = "https://www.google.com/search?q=" + encodeURIComponent(query) + "&hl=ko";

  if (incognito) {
    // 시크릿 창에서 검색 — 검색기록 안 남고 개인화 없는 중립 측정
    // (창을 화면에 띄우되 포커스는 안 뺏음 — 최소화하면 증거 캡처가 안 됨)
    let win;
    try {
      win = await chrome.windows.create({ url, incognito: true, focused: false,
                                          width: 1280, height: 1000, left: 40, top: 40 });
    } catch (e) {
      return { shown: false, text: "",
        error: "시크릿 창 생성 실패 — chrome://extensions → AI개요 체크 → 세부정보 → '시크릿 모드에서 허용'을 켜주세요 (" + String(e).slice(0, 80) + ")" };
    }
    let tabId = win && win.tabs && win.tabs[0] ? win.tabs[0].id : null;
    if (win && tabId == null) {
      try {
        const tabs = await chrome.tabs.query({ windowId: win.id });
        tabId = tabs[0] ? tabs[0].id : null;
      } catch (e) {}
    }
    try {
      if (tabId == null) throw new Error("시크릿 탭 없음 — '시크릿 모드에서 허용' 확인");
      await waitLoad(tabId);
      const [inj] = await chrome.scripting.executeScript({
        target: { tabId }, func: extractAio,
      });
      const res = inj?.result || { shown: false, text: "" };
      if (res.shown && win) {
        try { res.shot = await captureWin(win.id); }
        catch (e) { res.shot_err = String(e).slice(0, 100); }
      }
      return res;
    } catch (e) {
      const s = String(e);
      return { shown: false, text: "",
        error: (s.includes("Cannot access") || s.includes("incognito"))
          ? "시크릿 접근 불가 — '시크릿 모드에서 허용'을 켜주세요"
          : s.slice(0, 150) };
    } finally {
      try { await chrome.windows.remove(win.id); } catch (e) {}
    }
  }

  // 일반 모드: 백그라운드 탭 — 화면 안 뺏음
  const tab = await chrome.tabs.create({ url, active: false });
  try {
    await waitLoad(tab.id);
    const [inj] = await chrome.scripting.executeScript({
      target: { tabId: tab.id }, func: extractAio,
    });
    return inj?.result || { shown: false, text: "" };
  } catch (e) {
    return { shown: false, text: "", error: String(e).slice(0, 150) };
  } finally {
    try { await chrome.tabs.remove(tab.id); } catch (e) {}
  }
}

// ── 증거 캡처 공용: 창을 0.3초만 앞으로 → 찍고 → 원래 창 복귀 ──
async function captureWin(winId) {
  let prev = null;
  try { prev = await chrome.windows.getLastFocused(); } catch (e) {}
  await chrome.windows.update(winId, { focused: true });
  await sleep(350);
  const shot = await chrome.tabs.captureVisibleTab(winId, { format: "jpeg", quality: 70 });
  if (prev && prev.id !== winId) {
    try { await chrome.windows.update(prev.id, { focused: true }); } catch (e) {}
  }
  return shot;
}

// ── 작업 창/탭 열기 공용: 창 생성이 null이면 일반 탭으로 폴백 ──
async function openWork(url) {
  try {
    const win = await chrome.windows.create({ url, focused: false,
      width: 1280, height: 1000, left: 60, top: 60 });
    if (win) {
      let tabId = win.tabs && win.tabs[0] ? win.tabs[0].id : null;
      if (tabId == null) {
        try {
          const tabs = await chrome.tabs.query({ windowId: win.id });
          tabId = tabs[0] ? tabs[0].id : null;
        } catch (e) {}
      }
      if (tabId != null) {
        return { tabId, winId: win.id,
                 close: async () => { try { await chrome.windows.remove(win.id); } catch (e) {} } };
      }
      try { await chrome.windows.remove(win.id); } catch (e) {}
    }
  } catch (e) {}
  // 폴백: 현재 창의 백그라운드 탭 (캡처만 안 될 뿐 측정은 됨)
  const tab = await chrome.tabs.create({ url, active: false });
  return { tabId: tab.id, winId: null,
           close: async () => { try { await chrome.tabs.remove(tab.id); } catch (e) {} } };
}

// ── Gemini 웹: 별도 창 열어 입력창에 질문 → 답변 대기 → 추출+캡처 ──
async function searchOnceGem(query) {
  const w = await openWork("https://gemini.google.com/app");
  try {
    await waitLoad(w.tabId);
    const [inj] = await chrome.scripting.executeScript({
      target: { tabId: w.tabId }, func: extractGem, args: [query],
    });
    const res = inj?.result || { shown: false, text: "", error: "결과 없음" };
    if (res.shown && w.winId != null) {
      try { res.shot = await captureWin(w.winId); }
      catch (e) { res.shot_err = String(e).slice(0, 100); }
    }
    return res;
  } catch (e) {
    return { shown: false, text: "", error: String(e).slice(0, 150) };
  } finally {
    await w.close();
  }
}

// 페이지 안에서 실행: 입력 → 전송 → 스트리밍 완료 대기 → 추출
async function extractGem(query) {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  // 로그인 확인
  if (location.hostname.includes("accounts.google")) {
    return { shown: false, text: "", error: "Gemini: 구글 로그인 필요" };
  }
  // 입력창(Quill 에디터) 뜰 때까지 대기
  let editor = null;
  for (let i = 0; i < 20; i++) {
    editor = document.querySelector('rich-textarea .ql-editor, div[contenteditable="true"][role="textbox"]');
    if (editor) break;
    await sleep(1000);
  }
  if (!editor) {
    return { shown: false, text: "",
             error: "Gemini 입력창 못 찾음 — 로그인/화면 확인: "
               + (document.body.innerText || "").replace(/\s+/g, " ").slice(0, 100) };
  }
  // 임시 채팅 버튼이 보이면 눌러서 기록 안 남김 (성공 여부를 tmp로 기록)
  let usedTmp = false;
  try {
    const tmp = [...document.querySelectorAll('button, [role="button"]')]
      .find((b) => /임시 채팅|Temporary chat/i.test(b.getAttribute("aria-label") || b.innerText || ""));
    if (tmp) { tmp.click(); await sleep(1500); usedTmp = true;
      editor = document.querySelector('rich-textarea .ql-editor, div[contenteditable="true"][role="textbox"]') || editor; }
  } catch (e) {}
  // 질문 입력 (Quill은 innerText 세팅 + input 이벤트)
  editor.focus();
  editor.innerHTML = "";
  document.execCommand("insertText", false, query);
  await sleep(600);
  if (!(editor.innerText || "").trim()) {   // execCommand 실패 폴백
    editor.innerText = query;
    editor.dispatchEvent(new InputEvent("input", { bubbles: true }));
    await sleep(600);
  }
  // 전송: 버튼 클릭 → 폴백 Enter
  const sendBtn = document.querySelector('button[aria-label*="보내기"], button[aria-label*="Send"], button.send-button');
  if (sendBtn) sendBtn.click();
  else editor.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true }));
  // 답변 대기: 응답 텍스트가 3초 연속 안 변하면 완료 (최대 90초)
  const lastResp = () => {
    const rs = document.querySelectorAll("model-response, .model-response-text, message-content");
    return rs.length ? rs[rs.length - 1] : null;
  };
  let stable = 0, lastLen = 0;
  for (let i = 0; i < 90; i++) {
    const m = lastResp();
    if (m) {
      const len = (m.innerText || "").length;
      if (len > 0 && len === lastLen) { stable++; if (stable >= 3) break; }
      else stable = 0;
      lastLen = len;
    }
    await sleep(1000);
  }
  const m = lastResp();
  if (!m || !(m.innerText || "").trim()) {
    return { shown: false, text: "", error: "Gemini 답변 없음(시간초과)" };
  }
  // 출처 링크 (구글 내부링크 제외)
  const links = [];
  const seen = new Set();
  for (const a of m.querySelectorAll('a[href^="http"]')) {
    const h = a.href;
    if (/google\.com|gemini\.google|gstatic/.test(h)) continue;
    if (seen.has(h)) continue;
    seen.add(h);
    links.push({ u: h.slice(0, 300),
                 t: (a.innerText || "").trim().replace(/\s+/g, " ").slice(0, 60) });
    if (links.length >= 25) break;
  }
  return { shown: true, text: (m.innerText || "").slice(0, 6000), links, tmp: usedTmp };
}

// ── ChatGPT: 임시채팅 별도 창 열어 질문 → 답변 대기 → 추출+캡처 ──
async function searchOnceGpt(query) {
  const url = "https://chatgpt.com/?temporary-chat=true&q=" + encodeURIComponent(query);
  const w = await openWork(url);
  try {
    await waitLoad(w.tabId);
    const [inj] = await chrome.scripting.executeScript({
      target: { tabId: w.tabId }, func: extractGpt,
    });
    const res = inj?.result || { shown: false, text: "", error: "결과 없음" };
    if (res.shown && w.winId != null) {
      try { res.shot = await captureWin(w.winId); }
      catch (e) { res.shot_err = String(e).slice(0, 100); }
    }
    return res;
  } catch (e) {
    return { shown: false, text: "", error: String(e).slice(0, 150) };
  } finally {
    await w.close();
  }
}

// 페이지 안에서 실행: 답변 스트리밍 끝날 때까지 대기 → 텍스트+출처 추출
async function extractGpt() {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const lastMsg = () => {
    const msgs = document.querySelectorAll('[data-message-author-role="assistant"]');
    return msgs.length ? msgs[msgs.length - 1] : null;
  };
  const streaming = () =>
    !!document.querySelector('button[data-testid="stop-button"], [data-testid="composer-stop-button"]');

  // 로그인 벽/차단 감지 + 자동 제출 폴백
  let submitted = false;
  let stable = 0, lastLen = 0;
  for (let i = 0; i < 90; i++) {           // 최대 90초
    const bodyText = document.body.innerText || "";
    if (/로그인|Log in|Sign up/.test(bodyText.slice(0, 400))
        && !document.querySelector('#prompt-textarea, [contenteditable="true"]')) {
      return { shown: false, text: "", error: "ChatGPT 로그인 필요 — 이 크롬에서 chatgpt.com 로그인해 주세요" };
    }
    if (/Cloudflare|사람인지 확인|unusual activity/i.test(bodyText.slice(0, 400))) {
      return { shown: false, text: "", error: "ChatGPT 차단/확인 페이지 — 잠시 후 재시도" };
    }
    const m = lastMsg();
    if (m) {
      const len = (m.innerText || "").length;
      // 스트리밍 끝 + 텍스트 3초 연속 그대로면 완료
      if (!streaming() && len > 0 && len === lastLen) {
        stable++;
        if (stable >= 3) break;
      } else stable = 0;
      lastLen = len;
    } else if (i > 8 && !submitted) {
      // q 파라미터가 자동 제출 안 된 경우 — 전송 버튼 클릭
      const send = document.querySelector('button[data-testid="send-button"]');
      if (send) { send.click(); submitted = true; }
    }
    await sleep(1000);
  }
  const m = lastMsg();
  if (!m || !(m.innerText || "").trim()) {
    return { shown: false, text: "",
             error: "답변 없음(시간초과) — " + (document.body.innerText || "").replace(/\s+/g, " ").slice(0, 120) };
  }
  // 출처 링크 (답변 안 인용링크만, chatgpt/openai 내부링크 제외)
  const links = [];
  const seen = new Set();
  for (const a of m.querySelectorAll('a[href^="http"]')) {
    const h = a.href;
    if (h.includes("chatgpt.com") || h.includes("openai.com")) continue;
    if (seen.has(h)) continue;
    seen.add(h);
    links.push({ u: h.slice(0, 300),
                 t: (a.innerText || "").trim().replace(/\s+/g, " ").slice(0, 60) });
    if (links.length >= 25) break;
  }
  return { shown: true, text: (m.innerText || "").slice(0, 6000), links };
}

function waitLoad(tabId) {
  return new Promise((resolve) => {
    const to = setTimeout(done, 20000);
    function done() { clearTimeout(to); chrome.tabs.onUpdated.removeListener(onUpd); resolve(); }
    function onUpd(id, info) { if (id === tabId && info.status === "complete") done(); }
    chrome.tabs.onUpdated.addListener(onUpd);
  });
}

// ── 페이지 안에서 실행: AI 개요 대기 → "더보기" 펼치기 → 전체 추출 ──
async function extractAio() {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  let shown = false;
  for (let i = 0; i < 15; i++) {           // AI 개요 스트리밍 최대 15초 대기
    if (document.body.innerText.includes("AI 개요")) { shown = true; break; }
    await sleep(1000);
  }
  if (!shown)
    return { shown: false, text: "", debug: document.body.innerText.replace(/\s+/g, " ").slice(0, 200) };
  await sleep(2500);                        // 본문 스트리밍 마무리 대기

  // AI 개요 전체 블록 찾기: 'AI 개요' 라벨에서 조상으로 올라가며 본문 있는 첫 컨테이너
  function aioRoot() {
    let label = null;
    for (const el of document.querySelectorAll("div,span,h1,h2,strong")) {
      const own = [...el.childNodes].filter((n) => n.nodeType === 3)
        .map((n) => n.textContent.trim()).join("");
      if (own.includes("AI 개요")) { label = el; break; }
    }
    let cur = label;
    for (let i = 0; i < 14 && cur; i++) {
      if ((cur.innerText || "").length > 300) return cur;
      cur = cur.parentElement;
    }
    return null;
  }

  // "더보기"/"모두 표시" 클릭 — AI 개요 블록 안에서만 찾음 (다른 더보기 오클릭 방지)
  let root = aioRoot();
  const scope = root || document;
  // "더보기" 후보: 화면에 실제로 보이는(크기 있는) 요소
  const isVisible = (b) => {
    const r = b.getBoundingClientRect();
    return b.offsetParent !== null && r.height > 14 && r.width > 30;
  };
  // AI 개요 라벨의 위치 (이 아래에 있는 버튼만 후보)
  const labelY = (() => {
    for (const el of document.querySelectorAll("div,span,h1,h2,strong")) {
      const own = [...el.childNodes].filter((n) => n.nodeType === 3)
        .map((n) => n.textContent.trim()).join("");
      if (own.includes("AI 개요")) return el.getBoundingClientRect().top;
    }
    return 0;
  })();
  const findBtn = () => {
    // 문서 전체에서 찾음 (본문 블록 바깥에 붙은 큰 '더보기' 알약 버튼 포함)
    const all = [...document.querySelectorAll('div[role="button"], button, a, [jsaction], [jsname]')]
      .filter(isVisible);
    const norm = (b) => (b.innerText || "").trim().replace(/\s+/g, "");
    // '더보기'만 (출처 패널의 '모두 표시'는 본문 펼침이 아니므로 제외)
    let pool = all.filter((b) => {
      const t = norm(b);
      return t.includes("더보기") && t.length <= 8;
    });
    // AI개요 라벨보다 아래 + 넓은 것(알약 버튼) 우선, 그다음 라벨과 가까운 순
    pool = pool.filter((b) => b.getBoundingClientRect().top > labelY - 10);
    pool.sort((a, b) => {
      const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
      return (rb.width - ra.width) || (ra.top - rb.top);
    });
    // 알약 버튼은 넓다 — 폭 300px 이상이 있으면 그놈이 확실
    return pool[0] || null;
  };
  let btn = findBtn();
  let expanded = false, btnDebug = "";
  if (btn) {
    const rect = btn.getBoundingClientRect();
    btnDebug = "btn: [" + (btn.innerText || "").trim().slice(0, 10) + "] " + btn.tagName
             + " " + Math.round(rect.width) + "x" + Math.round(rect.height);
    btn.scrollIntoView({ block: "center" });
    await sleep(400);
    // 진짜 마우스처럼: 버튼 중심 좌표로 이벤트 시퀀스
    const r2 = btn.getBoundingClientRect();
    const cx = r2.left + r2.width / 2, cy = r2.top + r2.height / 2;
    const target = document.elementFromPoint(cx, cy) || btn;
    for (const type of ["pointerover", "pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
      target.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true,
        view: window, clientX: cx, clientY: cy }));
    }
    await sleep(2500);                       // 펼침 대기
    // 펼쳐짐 판정 = 더보기 버튼이 화면에서 사라졌는가
    const still = findBtn();
    expanded = !still;
    if (!expanded) { try { still.click(); await sleep(2000); expanded = !findBtn(); } catch (e) {} }
    btnDebug += " expanded=" + expanded;
    root = aioRoot() || root;
  } else {
    const cands = [...scope.querySelectorAll('div[role="button"], button, a, [jsaction]')];
    btnDebug = "btn NOT found(visible). texts: " + cands.filter(isVisible).slice(0, 30)
      .map((b) => (b.innerText || "").trim().replace(/\s+/g, " ").slice(0, 10))
      .filter(Boolean).slice(0, 12).join(" | ");
  }

  // 본문 추출: 펼쳐진 블록 vs #m-x-content 중 더 긴 쪽
  let text = document.querySelector("#m-x-content")?.innerText || "";
  if (root && (root.innerText || "").length > text.length) text = root.innerText;
  if (!text) {
    const body = document.body.innerText;
    const s = body.indexOf("AI 개요");
    if (s >= 0) {
      let e = body.indexOf("AI 대답에는 오류가", s);
      if (e < 0) e = Math.min(s + 6000, body.length);
      text = body.slice(s, e);
    }
  }
  // 인용 출처 링크 수집 (AI 개요 블록 안의 링크만 — 채널 분석용)
  const links = [];
  if (root) {
    const seen = new Set();
    for (const a of root.querySelectorAll('a[href^="http"]')) {
      let href = a.href;
      // 구글 리다이렉트 링크면 실제 주소 꺼내기
      try {
        if (href.includes("google.com/url?")) {
          const real = new URL(href).searchParams.get("q") || new URL(href).searchParams.get("url");
          if (real) href = real;
        }
      } catch (e) {}
      if (href.includes("google.com/search")) continue;   // 내부 검색링크 제외
      if (seen.has(href)) continue;
      seen.add(href);
      links.push({ u: href.slice(0, 300),
                   t: (a.innerText || "").trim().replace(/\s+/g, " ").slice(0, 60) });
      if (links.length >= 25) break;
    }
  }
  return { shown: true, text, expanded, btn_debug: btnDebug, links };
}
