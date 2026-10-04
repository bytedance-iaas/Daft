#!/usr/bin/env python3
"""Assemble the visualizer mockups: inline the shared parts into each page (pages are self-contained HTML)."""
import re, sys, pathlib

HERE = pathlib.Path(__file__).parent
OUT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / 'out'
OUT.mkdir(parents=True, exist_ok=True)
parts = {p.name: p.read_text() for p in (HERE / 'parts').iterdir()}

def shell(active):
    s = parts['shell.html']
    return re.sub(r'\{\{nav:(\w+)\}\}', lambda m: 'active' if m.group(1) == active else '', s)

for page in sorted((HERE / 'pages').glob('*.html')):
    src = page.read_text()
    src = re.sub(r'\{\{shell:(\w+)\}\}', lambda m: shell(m.group(1)), src)
    src = re.sub(r'\{\{([\w.]+)\}\}', lambda m: parts[m.group(1)].rstrip('\n'), src)
    (OUT / page.name).write_text(src)
    print(page.name, len(src.encode()), 'bytes')
