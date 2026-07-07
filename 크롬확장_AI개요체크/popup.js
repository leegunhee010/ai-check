const $ = (id) => document.getElementById(id);

function refresh() {
  chrome.runtime.sendMessage({ cmd: "status" }, (run) => {
    if (!run) return;
    const el = $("status");
    if (run.active) {
      el.className = "on";
      el.textContent = "측정 중 [" + run.i + "/" + run.n + "] R" + run.round + "/" + run.rt + " — " + run.msg;
    } else {
      el.className = "";
      el.textContent = run.msg || "대기 중";
    }
    $("btnStart").disabled = run.active;
  });
}

$("btnStart").addEventListener("click", () => {
  chrome.runtime.sendMessage({ cmd: "start", repeats: +$("repeats").value,
                               incognito: $("incog").checked });
  setTimeout(refresh, 300);
});
$("btnStop").addEventListener("click", () => {
  chrome.runtime.sendMessage({ cmd: "stop" });
  setTimeout(refresh, 300);
});

document.getElementById("ver").textContent = "v" + chrome.runtime.getManifest().version;
refresh();
setInterval(refresh, 1000);
