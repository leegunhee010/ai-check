# -*- coding: utf-8 -*-
"""
AI노출체크 — 런처 (exe 진입점)
================================
1) GitHub에서 새 버전 확인 → 있으면 "업데이트?" 팝업 → 작은 파일만 교체
2) exe 옆의 app.py(앱 본체)를 로드해 실행
exe는 파이썬+Flask 런타임을 품은 '껍데기'이고, 실제 로직(app.py/engine.py)과
크롬확장은 바깥 파일이라 GitHub로 갱신됩니다. (통검체크와 같은 구조)
"""
import os, sys, json, shutil, importlib.util, urllib.request, urllib.parse, ctypes
import flask            # noqa  (exe 빌드 시 포함)

FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
MEI = getattr(sys, "_MEIPASS", HERE)

REPO = "leegunhee010/ai-check"
RAW = "https://raw.githubusercontent.com/%s/main/" % REPO
UPDATE_FILES = [                                # GitHub로 갱신되는 파일들
    "app.py", "engine.py",
    "크롬확장_AI개요체크/manifest.json",
    "크롬확장_AI개요체크/background.js",
    "크롬확장_AI개요체크/content.js",
    "크롬확장_AI개요체크/popup.html",
    "크롬확장_AI개요체크/popup.js",
]
SEED_FILES = ["app.py", "engine.py", "AI체크_설정.json", "AI체크_데이터.json",
              "version.json"]


def _box(msg, title, flags):
    try:
        return ctypes.windll.user32.MessageBoxW(0, msg, title, flags)
    except Exception:
        print(title, "-", msg)
        return 0


def ensure_files():
    # exe 옆에 파일이 없으면 번들된 사본으로 깔아둠 (최초 실행 직후)
    for f in SEED_FILES:
        dst = os.path.join(HERE, f)
        if not os.path.exists(dst):
            src = os.path.join(MEI, f)
            if os.path.exists(src):
                try:
                    shutil.copy(src, dst)
                except Exception:
                    pass


def _vt(s):
    try:
        return tuple(int(x) for x in str(s).split("."))
    except Exception:
        return (0,)


def local_version():
    try:
        with open(os.path.join(HERE, "version.json"), encoding="utf-8") as f:
            return json.load(f).get("version", "0")
    except Exception:
        return "0"


def check_update():
    try:
        req = urllib.request.Request(RAW + "version.json",
                                     headers={"Cache-Control": "no-cache"})
        rv = json.loads(urllib.request.urlopen(req, timeout=6).read()
                        .decode("utf-8")).get("version", "0")
    except Exception:
        return                       # 인터넷 안 되면 조용히 그냥 실행
    lv = local_version()
    if _vt(rv) <= _vt(lv):
        return
    if _box("새 버전 %s 이(가) 있습니다. (현재 %s)\n\n지금 업데이트할까요?" % (rv, lv),
            "AI노출체크 업데이트", 0x44) != 6:   # MB_YESNO|MB_ICONINFORMATION, 6=예
        return
    try:
        for f in UPDATE_FILES:
            url = RAW + urllib.parse.quote(f)   # 한글 경로 인코딩
            data = urllib.request.urlopen(url, timeout=30).read()
            dst = os.path.join(HERE, f.replace("/", os.sep))
            os.makedirs(os.path.dirname(dst) or HERE, exist_ok=True)
            with open(dst, "wb") as out:
                out.write(data)
        with open(os.path.join(HERE, "version.json"), "w", encoding="utf-8") as out:
            json.dump({"version": rv}, out, ensure_ascii=False)
        _box("업데이트 완료! (버전 %s)\n\n크롬 확장도 갱신됐습니다 —\n"
             "chrome://extensions 에서 [AI개요 체크] 새로고침(↻)을 눌러주세요." % rv,
             "AI노출체크", 0x40)
    except Exception as e:
        _box("업데이트 실패: %s\n기존 버전으로 실행합니다." % (str(e)[:150]),
             "AI노출체크", 0x30)


def run_app():
    spec = importlib.util.spec_from_file_location("app", os.path.join(HERE, "app.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.main()


if __name__ == "__main__":
    try:
        ensure_files()
        check_update()
        run_app()
    except Exception:
        # 어떤 오류든 조용히 죽지 말고 화면에 보여줌
        import traceback
        tb = traceback.format_exc()
        try:
            ctypes.windll.user32.MessageBoxW(0, tb[-900:], "AI노출체크 오류", 0x10)
        except Exception:
            print(tb)
            try:
                input("엔터를 누르면 닫힙니다...")
            except Exception:
                pass
