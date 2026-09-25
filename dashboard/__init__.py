"""Renders docs/index.html, the combined agent dashboard.

scripts/build_dashboard.py is the entry point: it loads docs/data/*.json,
maintains the *_history.json files, and calls the section renderers here.
One module per agent under sections/; page.py holds the shell. To add an
agent, see docs/INFRASTRUCTURE.md.
"""
