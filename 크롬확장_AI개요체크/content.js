// AI노출체크 앱 페이지(127.0.0.1:5630)와 확장을 잇는 브릿지
// 페이지 → (postMessage) → 여기 → (runtime.sendMessage) → background
// background 상태 → 여기 → (postMessage) → 페이지 상태표시

// 페이지에 "확장 연결됨" 알림 (버전 포함)
window.postMessage({ aicheck_ready: true,
                     aicheck_version: chrome.runtime.getManifest().version }, "*");

// 페이지에서 오는 측정 명령 전달
window.addEventListener("message", (e) => {
  if (e.source !== window || !e.data || !e.data.aicheck_cmd) return;
  chrome.runtime.sendMessage({
    cmd: e.data.aicheck_cmd,            // 'start' | 'stop'
    repeats: e.data.repeats || 1,
    ids: e.data.ids || null,
    incognito: e.data.incognito !== false,
  });
});

// 확장 측정 상태를 페이지로 계속 흘려보냄
setInterval(() => {
  try {
    chrome.runtime.sendMessage({ cmd: "status" }, (run) => {
      if (chrome.runtime.lastError || !run) return;
      window.postMessage({ aicheck_status: run }, "*");
    });
  } catch (e) {}
}, 1500);
