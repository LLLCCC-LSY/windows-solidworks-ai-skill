"""Read-only Python/SolidWorks COM diagnosis. Never launches SolidWorks."""

import argparse
import importlib.util
import json
import platform
import struct
import sys
from pathlib import Path


def probe():
    report = {"platform": platform.system(), "python": platform.python_version(),
              "python_bits": struct.calcsize("P") * 8, "auto_launch": False,
              "connected": False, "dependencies": {}}
    for module in ("pythoncom", "win32com", "mcp"):
        report["dependencies"][module] = importlib.util.find_spec(module) is not None
    if sys.platform != "win32" or not report["dependencies"]["pythoncom"]:
        return report
    import pythoncom
    import win32com.client
    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    raw = app = doc = None
    try:
        raw = pythoncom.GetActiveObject("SldWorks.Application").QueryInterface(pythoncom.IID_IDispatch)
        app = win32com.client.Dispatch(raw)

        def read(obj, name):
            value = getattr(obj, name)
            return value() if callable(value) and not hasattr(value, "_oleobj_") else value

        report["revision"] = str(read(app, "RevisionNumber"))
        doc = read(app, "ActiveDoc")
        report["active_document"] = read(doc, "GetTitle") if doc is not None else None
        report["connected"] = True
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        raw = app = doc = None
        pythoncom.CoUninitialize()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--save-report", action="store_true")
    args = parser.parse_args()
    result = probe()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.save_report:
        if args.workspace is None:
            parser.error("--save-report requires --workspace.")
        report_dir = args.workspace / "Reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "solidworks-diagnosis.json").write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
