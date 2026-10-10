"""End-to-end test of the HTTP endpoint inside a GUI FreeCAD.

Run:  DISPLAY=:0 flatpak run --command=freecad org.freecad.FreeCAD <this file>

This is the case the headless suite cannot reach: GuiUp is 1, a Qt event loop
is running, so dispatch_to_gui has somewhere to deliver work and tools/call
actually touches the FreeCAD document model.
"""

import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error

import FreeCAD

MOD = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "FreeCADMCP")
sys.path.insert(0, MOD)

URL = "http://127.0.0.1:9876/mcp"
results = []
LOGF = open(os.path.expanduser("~/e2e_out.txt"), "w", buffering=1)
import builtins
_p = builtins.print
def print(*a, **k):
    _p(*a, **k)
    _p(*a, file=LOGF, flush=True)


def check(label, ok, detail=""):
    results.append((label, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""), flush=True)


def tools_call(name, args=None, rid=1):
    body = {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
            "params": {"name": name, "arguments": args or {}}}
    req = urllib.request.Request(
        URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return {"http_status": exc.code, "body": exc.read().decode()}


def result_text(payload):
    return (payload.get("result", {}).get("content") or [{}])[0].get("text", "")


def is_error(payload):
    return bool(payload.get("result", {}).get("isError"))


def worker():
    """Runs off the GUI thread; the Qt loop services the queue meanwhile."""
    time.sleep(2.0)

    print(f"\n--- GUI-up evidence ---", flush=True)
    check("FreeCAD reports GuiUp", FreeCAD.GuiUp == 1, f"GuiUp={FreeCAD.GuiUp}")

    print("\n--- calls that need the GUI thread ---", flush=True)

    out = tools_call("ping", {}, 1)
    check("ping over tools/call", not is_error(out), result_text(out)[:120])

    out = tools_call("list_documents", {}, 2)
    check("list_documents", not is_error(out), result_text(out)[:160])

    out = tools_call("create_document", {"name": "shttp_e2e"}, 3)
    check("create_document", not is_error(out), result_text(out)[:160])

    out = tools_call("list_documents", {}, 4)
    check("new document is visible", "shttp_e2e" in result_text(out), result_text(out)[:200])

    # A geometry call: proves the document model is really being touched.
    out = tools_call(
        "execute_code",
        {
            "code": (
                "import FreeCAD as App\n"
                "d = App.getDocument('shttp_e2e')\n"
                "box = d.addObject('Part::Box', 'Box')\n"
                "box.Length = 30; box.Width = 20; box.Height = 10\n"
                "d.recompute()\n"
                "print('VOLUME', round(box.Shape.Volume, 3), 'FACES', len(box.Shape.Faces))\n"
            )
        },
        5,
    )
    text = result_text(out)
    check("execute_code creates geometry", not is_error(out) and "VOLUME" in text, text[:220])
    check("volume is the analytic 6000", "6000.0" in text.replace(" ", ""), text[:200])

    out = tools_call("get_objects", {"doc_name": "shttp_e2e"}, 6)
    check("get_objects sees the box", "Box" in result_text(out), result_text(out)[:160])

    print("\n--- error paths ---", flush=True)

    out = tools_call("execute_code", {}, 7)
    check("missing required argument is a tool error", is_error(out), result_text(out)[:120])

    out = tools_call("no_such_tool", {}, 8)
    check("unknown tool is a tool error", is_error(out), result_text(out)[:120])

    out = tools_call("execute_code", {"code": "raise ValueError('boom')"}, 9)
    check("raising code is reported, not crashed", is_error(out), result_text(out)[:160])

    out = tools_call("get_objects", {"doc_name": "no_such_doc"}, 10)
    # Upstream _get_objects_gui swallows the missing-document exception and
    # returns [], so the HTTP layer passes that through unchanged. Asserting an
    # error here would assert behaviour this endpoint does not have. Worth
    # flagging upstream: an unknown document is indistinguishable from an empty
    # one.
    check(
        "unknown document follows upstream's [] rather than an error",
        not is_error(out) and result_text(out).strip() == "[]",
        result_text(out)[:120],
    )

    # The endpoint must still answer after all those failures.
    out = tools_call("ping", {}, 11)
    check("endpoint still alive after errors", not is_error(out), result_text(out)[:100])


def finish():
    from mcp_http import server as http_server

    print("\n" + "=" * 62, flush=True)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"{passed}/{len(results)} checks passed", flush=True)
    for label, ok, detail in results:
        if not ok:
            print(f"  FAILED: {label} -- {detail}", flush=True)
    print("=" * 62, flush=True)

    try:
        print(http_server.stop_http_server(), flush=True)
    except Exception as exc:
        print(f"stop failed: {exc}", flush=True)

    import FreeCADGui
    FreeCADGui.getMainWindow().close()


print(f"FreeCAD {FreeCAD.Version()[0]}.{FreeCAD.Version()[1]}.{FreeCAD.Version()[2]}", flush=True)

from mcp_http import server as http_server
print(http_server.start_http_server(), flush=True)

from PySide import QtCore
QtCore.QTimer.singleShot(500, lambda: threading.Thread(target=worker, daemon=True).start())
QtCore.QTimer.singleShot(60000, finish)