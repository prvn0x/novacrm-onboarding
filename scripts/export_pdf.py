"""Export the design doc to PDF for the submission: Markdown → styled HTML → headless Chrome.

uv run python scripts/export_pdf.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import markdown

SOURCE = Path("docs/REQUIREMENTS_AND_DESIGN.md")
HTML = Path("var/design.html")
PDF = Path("docs/NovaCRM_Onboarding_Requirements_and_Design.pdf")
CHROMES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    shutil.which("chromium") or "",
    shutil.which("google-chrome") or "",
]
CSS = """
@page { size: A4; margin: 16mm 14mm; }
body { font: 10.5pt/1.45 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; color: #1a1a1a; }
h1 { font-size: 20pt; margin: 0 0 4pt; } h2 { font-size: 14pt; margin: 18pt 0 6pt;
  border-bottom: 1px solid #ddd; padding-bottom: 3pt; page-break-after: avoid; }
h3 { font-size: 11.5pt; margin: 12pt 0 4pt; page-break-after: avoid; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0 10pt; font-size: 9pt;
  page-break-inside: auto; } tr { page-break-inside: avoid; }
th, td { border: 1px solid #d0d0d0; padding: 3pt 5pt; vertical-align: top; text-align: left; }
th { background: #f3f4f6; }
pre { background: #f6f8fa; border: 1px solid #e1e4e8; padding: 6pt 8pt; font-size: 8pt;
  line-height: 1.3; white-space: pre-wrap; page-break-inside: avoid; }
code { font: 8.5pt Menlo, Consolas, monospace; } hr { border: 0; border-top: 1px solid #eee; }
"""


def main() -> None:
    body = markdown.markdown(
        SOURCE.read_text(encoding="utf-8"), extensions=["tables", "fenced_code", "sane_lists"]
    )
    HTML.parent.mkdir(parents=True, exist_ok=True)
    HTML.write_text(
        f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head>"
        f"<body>{body}</body></html>",
        encoding="utf-8",
    )
    chrome = next((c for c in CHROMES if c and Path(c).exists()), None)
    if chrome is None:
        sys.exit(f"no Chrome found; open {HTML} in a browser and print it to PDF")
    subprocess.run(
        [chrome, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
         f"--print-to-pdf={PDF.resolve()}", HTML.resolve().as_uri()],
        check=True, capture_output=True,
    )  # fmt: skip
    print(f"wrote {PDF}")


if __name__ == "__main__":
    main()
