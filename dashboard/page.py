"""The dashboard page shell: <head>, CSS, header, footer.

Sections are rendered by dashboard/sections/*.py and passed in already as
HTML, in display order."""

from __future__ import annotations

from dashboard.common import favicon_data_uri


def render_page(sections: list[str], generated_at: str) -> str:
    """The full docs/index.html document."""
    favicon = favicon_data_uri()
    main = "\n    ".join(sections)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Jzx Portfolio Management</title>
  <link rel="icon" type="image/png" href="{favicon}">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0f172a;
      color: #e2e8f0;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif;
      min-height: 100vh;
    }}
    header {{
      background: #1e293b;
      border-bottom: 1px solid #334155;
      padding: 16px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      position: sticky;
      top: 0;
      z-index: 100;
    }}
    .logo {{
      font-size: 20px;
      font-weight: 700;
      color: #f1f5f9;
      letter-spacing: -0.3px;
    }}
    .logo span {{ color: #3b82f6; }}
    .meta {{
      font-size: 12px;
      color: #64748b;
    }}
    main {{
      max-width: 1100px;
      margin: 32px auto;
      padding: 0 20px;
      display: flex;
      flex-direction: column;
      gap: 24px;
    }}
    .agent-card {{
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 10px;
      padding: 20px 24px;
    }}
    .card-header {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      padding-bottom: 14px;
      border-bottom: 1px solid #334155;
      margin-bottom: 4px;
    }}
    .agent-name {{
      display: block;
      font-size: 18px;
      font-weight: 700;
      color: #f1f5f9;
    }}
    .agent-role {{
      display: block;
      font-size: 12px;
      color: #64748b;
      margin-top: 2px;
    }}
    table td, table th {{
      padding: 6px 8px;
      border-bottom: 1px solid #1e293b;
      vertical-align: middle;
    }}
    a {{ text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    footer {{
      text-align: center;
      color: #334155;
      font-size: 11px;
      padding: 32px 20px;
    }}
    .ned-grid {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 12px;
    }}
    @media (max-width: 900px) {{
      .ned-grid {{ grid-template-columns: repeat(2, 1fr); }}
    }}
    @media (max-width: 700px) {{
      .card-header {{ flex-direction: column; gap: 8px; }}
      table {{ font-size: 11px; }}
      .ned-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="logo"><img src="{favicon}" alt="" width="26" height="26" style="vertical-align:-6px;border-radius:50%;margin-right:6px"> <span>Jzx</span> Portfolio Management</div>
    <div class="meta">Auto-updated by GitHub Actions &nbsp;·&nbsp; Generated: {generated_at}</div>
  </header>
  <main>
    {main}
  </main>
  <footer>
    Bob the Bot · Singapore Slinger · Bob USA · Wally the Watcher · Selling Sally · Harry Hindsight · Theo · Ned · Transcripts &nbsp;·&nbsp; JohnnyM77/Reporting-Agent
  </footer>
</body>
</html>"""
