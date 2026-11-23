# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
changelog.py – „Was ist neu?“ aus LIESMICH.md (Abschnitte „## Neu in Version X: Titel“).
"""
import html
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
HEAD = re.compile(r"^## Neu in Version ([\d.]+):?\s*(.*)$")


def vtuple(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (0,)


def sections(text=None):
    """→ [(version, titel, [zeilen])] in Datei-Reihenfolge (neueste zuerst)."""
    if text is None:
        try:
            with open(os.path.join(HERE, "LIESMICH.md"), encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return []
    out, cur = [], None
    for line in text.splitlines():
        m = HEAD.match(line)
        if m:
            cur = (m.group(1), m.group(2).strip(), [])
            out.append(cur)
        elif line.startswith("## ") or line.startswith("# "):
            cur = None
        elif cur is not None:
            cur[2].append(line)
    return [(v, t, _trim(ls)) for v, t, ls in out]


def _trim(lines):
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    return lines


def since(old, new, text=None, limit=3):
    """Abschnitte mit old < Version ≤ new (höchstens `limit`, neueste zuerst)."""
    o, n = vtuple(old or "0"), vtuple(new)
    return [s for s in sections(text) if o < vtuple(s[0]) <= n][:limit]


def plain(lines):
    """Für Tk: Markdown-Zeichen weg, Aufzählungen als •."""
    out = []
    for ln in lines:
        s = ln.replace("**", "").replace("`", "")
        m = re.match(r"^(\s*)- (.*)$", s)
        if m:
            s = ("    " if m.group(1) else "") + "• " + m.group(2)
        elif s.startswith("  ") and out and out[-1].lstrip().startswith("•"):
            out[-1] += " " + s.strip()  # umbrochene Aufzählungszeile anhängen
            continue
        out.append(s)
    return "\n".join(out)


def to_html(lines):
    """Für das Handy: sicheres, einfaches HTML (escapen, dann **fett**, `code`, Listen, Absätze)."""
    def inline(t):
        t = html.escape(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        return re.sub(r"`(.+?)`", r"<code>\1</code>", t)
    out, in_list, para = [], False, []

    def flush():
        nonlocal para
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            para = []
    for ln in lines:
        m = re.match(r"^\s*- (.*)$", ln)
        if m:
            flush()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>" + inline(m.group(1)) + "</li>")
        elif ln.startswith("  ") and in_list and out[-1].startswith("<li>"):
            out[-1] = out[-1][:-5] + " " + inline(ln.strip()) + "</li>"
        elif not ln.strip():
            flush()
            if in_list:
                out.append("</ul>")
                in_list = False
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            para.append(ln.strip())
    flush()
    if in_list:
        out.append("</ul>")
    return "".join(out)
