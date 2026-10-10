import sys, os, json, time, threading, base64, urllib.request
import FreeCAD, FreeCADGui
MOD = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "FreeCADMCP")
sys.path.insert(0, MOD)
LOG = open(os.path.expanduser("~/mm_out.txt"), "w", buffering=1)
def log(*a): print(*a, file=LOG, flush=True)

results = []
def check(label, ok, detail=""):
    results.append((label, ok, detail))
    log(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))

URL = "http://127.0.0.1:9876/mcp"
def rpc(method, params=None, rid=1):
    body = {"jsonrpc":"2.0","id":rid,"method":method}
    if params is not None: body["params"] = params
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode())

def tc(name, args=None, rid=1):
    return rpc("tools/call", {"name":name, "arguments":args or {}}, rid)

def blocks(p): return p.get("result",{}).get("content",[])
def kinds(p): return [b.get("type") for b in blocks(p)]

def valid_png(data):
    try: raw = base64.b64decode(data, validate=True)
    except Exception: return None
    return raw if raw[:8] == b"\x89PNG\r\n\x1a\n" else None

def finish():
    log("\n" + "="*60)
    passed = sum(1 for _,ok,_ in results if ok)
    log(f"{passed}/{len(results)} checks passed")
    for l,ok,d in results:
        if not ok: log(f"  FAILED: {l} -- {d}")
    log("="*60)
    from mcp_http import server as hs
    try: log(hs.stop_http_server())
    except Exception as e: log("stop:", e)
    FreeCADGui.getMainWindow().close()

def worker():
    time.sleep(2.0)
    from mcp_http import server as hs
    log("GuiUp =", FreeCAD.GuiUp)
    log(hs.start_http_server())

    # make something worth photographing
    out = tc("execute_code", {"code":
        "import FreeCAD as App\n"
        "d = App.newDocument('mm')\n"
        "b = d.addObject('Part::Box','Box')\n"
        "b.Length=40; b.Width=30; b.Height=20\n"
        "d.recompute()\n"
        "print('VOL', round(b.Shape.Volume,1))\n"}, 1)
    log("geometry:", blocks(out)[0]["text"][:120])

    # text mode
    p = tc("list_documents", {"content_mode":"text"}, 2)
    check("text mode: no image", kinds(p) == ["text"], str(kinds(p)))

    # image mode
    p = tc("list_documents", {"content_mode":"image"}, 3)
    ks = kinds(p)
    check("image mode: text+image", ks == ["text","image"], str(ks))
    img = [b for b in blocks(p) if b.get("type")=="image"]
    if img:
        raw = valid_png(img[0]["data"])
        check("image is a real PNG", raw is not None,
              f"{len(base64.b64decode(img[0]['data']))} bytes" if img else "no image")
        check("image mimeType is image/png", img[0].get("mimeType")=="image/png",
              str(img[0].get("mimeType")))
    else:
        check("image is a real PNG", False, "no image block returned")

    # multimodal
    p = tc("list_documents", {"content_mode":"multimodal"}, 4)
    ks = kinds(p)
    imgs = [b for b in blocks(p) if b.get("type")=="image"]
    check("multimodal: three images", len(imgs)==3, f"blocks={ks}")
    check("all multimodal images decode", all(valid_png(i["data"]) for i in imgs),
          f"{len(imgs)} images")
    total = sum(len(base64.b64decode(i["data"])) for i in imgs)
    log(f"   multimodal payload: {total/1024:.1f} KB decoded across {len(imgs)} views")

    # capture inside the capture tool must not recurse
    p = tc("get_active_screenshot", {"view_name":"Isometric","content_mode":"multimodal"}, 5)
    ks = kinds(p)
    check("screenshot tool returns one image, not four", ks.count("image")==1, str(ks))
    raw = valid_png(blocks(p)[0]["data"]) if blocks(p) else None
    check("screenshot tool returns a real PNG", raw is not None,
          f"{len(raw)/1024:.1f} KB" if raw else blocks(p)[0]["text"][:120])

    # plain call, no mode
    p = tc("list_documents", {}, 6)
    check("omitted mode stays text", kinds(p) == ["text"], str(kinds(p)))

    # endpoint healthy after images
    p = tc("ping", {"content_mode":"image"}, 7)
    check("endpoint healthy after images", kinds(p) == ["text","image"], str(kinds(p)))

    finish()

from PySide import QtCore
QtCore.QTimer.singleShot(500, lambda: threading.Thread(target=worker, daemon=True).start())
QtCore.QTimer.singleShot(180000, finish)
