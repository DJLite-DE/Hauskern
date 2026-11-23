# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
lists.py – Einkaufsliste & Aufgaben für die ganze Familie (Handy, PC, Siri, Wand-Display).

Gespeichert in cfg["lists"] = {"einkauf": {...}, "aufgaben": {...}}, jeder Eintrag:
    {"id", "text", "done": bool, "by": Name, "ts": angelegt, "done_ts"}
"""
import re
import time
import uuid

DEFAULT = {"einkauf": {"name": "Einkaufsliste", "icon": "🛒", "items": []},
           "aufgaben": {"name": "Aufgaben", "icon": "✅", "items": []}}
MAX_ITEMS = 300
KEEP_DONE_DAYS = 7     # erledigte Einträge verschwinden danach von selbst

# Siri: „Milch und Eier auf die Einkaufsliste“, „setz Brot auf die Liste“, „Müll rausbringen auf die Aufgaben“
ADD_RE = re.compile(r"^(?:bitte\s+)?(?:setz(?:e)?|schreib(?:e)?|pack(?:e)?|füg(?:e)?|tu|trag(?:e)?|notier(?:e)?)?\s*"
                    r"(?P<what>.+?)\s+(?:auf|zur|zu der|in die|an die)\s+(?:die\s+|meine\s+|unsere\s+)?"
                    r"(?P<list>einkaufsliste|einkaufszettel|liste|aufgabenliste|aufgaben|to\s?do\s?liste|todo)"
                    r"(?:\s+(?:hinzu|drauf|ein|dazu))?\s*$", re.I)
SHOW_RE = re.compile(r"(was steht|was ist|was muss|was haben|zeig|lies|vorlesen).*?"
                     r"(aufgabenliste|aufgaben|to\s?do|einkaufsliste|einkaufszettel|liste)", re.I)
SPOKEN = {"einkauf": "Einkaufsliste", "aufgaben": "Aufgabenliste"}  # „auf der …“
DONE_RE = re.compile(r"^(?:streich(?:e)?|lösch(?:e)?|hak(?:e)?)\s+(?P<what>.+?)(?:\s+(?:von der|aus der|auf der)\s+"
                     r"(?:einkaufsliste|liste|aufgabenliste|aufgaben))?(?:\s+ab)?\s*$|^(?P<what2>.+?)\s+(?:ist\s+)?"
                     r"(?:gekauft|erledigt|abgehakt)\s*$", re.I)


def ensure(cfg):
    ls = cfg.setdefault("lists", {})
    for k, v in DEFAULT.items():
        if k not in ls:
            ls[k] = {"name": v["name"], "icon": v["icon"], "items": []}
    return ls


def which(word):
    w = (word or "").lower().replace(" ", "")
    return "aufgaben" if w.startswith(("aufgabe", "todo")) else "einkauf"


def split_items(text):
    """„Milch, Eier und 2 Brot“ → ["Milch", "Eier", "2 Brot"]"""
    parts = re.split(r"\s*(?:,|;|\bund\b|\bsowie\b|\+)\s*", text.strip(), flags=re.I)
    out = []
    for p in parts:
        p = re.sub(r"^(?:noch\s+)?(?:ein(?:e|en|em)?\s+|etwas\s+)?", "", p.strip(), flags=re.I).strip(" .!?")
        if p:
            out.append(p[:1].upper() + p[1:80])
    return out


def norm(t):
    return re.sub(r"\s+", " ", (t or "").lower()).strip()


def add(lst, texts, by=""):
    """Einträge hinzufügen. Schon offen vorhandene werden nicht doppelt eingetragen. → (neu, schon_da)"""
    new, dup = [], []
    open_ = {norm(i["text"]): i for i in lst["items"] if not i["done"]}
    for t in texts:
        t = t.strip()
        if not t:
            continue
        if norm(t) in open_:
            dup.append(open_[norm(t)]["text"])
            continue
        done_same = next((i for i in lst["items"] if i["done"] and norm(i["text"]) == norm(t)), None)
        if done_same:  # schon mal gekauft → wieder aktiv statt neuer Eintrag
            done_same.update(done=False, by=by, ts=int(time.time()), done_ts=None)
            new.append(done_same["text"])
            open_[norm(t)] = done_same
            continue
        it = {"id": uuid.uuid4().hex[:8], "text": t[:80], "done": False, "by": by, "ts": int(time.time()), "done_ts": None}
        lst["items"].append(it)
        open_[norm(t)] = it
        new.append(it["text"])
    if len(lst["items"]) > MAX_ITEMS:
        lst["items"] = [i for i in lst["items"] if not i["done"]][-MAX_ITEMS:]
    return new, dup


def find(lst, text):
    """Offenen Eintrag zu einem Text finden (genau, sonst Anfang/Teilwort)."""
    n = norm(text)
    open_ = [i for i in lst["items"] if not i["done"]]
    return (next((i for i in open_ if norm(i["text"]) == n), None)
            or next((i for i in open_ if norm(i["text"]).startswith(n) or n in norm(i["text"]).split()), None))


def set_done(lst, item_id, done=True, by=""):
    it = next((i for i in lst["items"] if i["id"] == item_id), None)
    if it:
        it["done"] = bool(done)
        it["done_ts"] = int(time.time()) if done else None
        if done:
            it["done_by"] = by
    return it


def remove(lst, item_id):
    n = len(lst["items"])
    lst["items"] = [i for i in lst["items"] if i["id"] != item_id]
    return len(lst["items"]) != n


def clear_done(lst):
    n = len(lst["items"])
    lst["items"] = [i for i in lst["items"] if not i["done"]]
    return n - len(lst["items"])


def prune(lists, now=None):
    """Erledigte nach ein paar Tagen von selbst wegräumen. → Anzahl entfernt"""
    now = now or time.time()
    gone = 0
    for lst in lists.values():
        n = len(lst["items"])
        lst["items"] = [i for i in lst["items"] if not (i["done"] and now - (i.get("done_ts") or now) > KEEP_DONE_DAYS * 86400)]
        gone += n - len(lst["items"])
    return gone


def open_items(lst):
    return [i for i in lst["items"] if not i["done"]]


def speak(lst, key="einkauf"):
    name = SPOKEN.get(key, lst["name"])
    o = [i["text"] for i in open_items(lst)]
    if not o:
        return f"Die {name} ist leer."
    if len(o) == 1:
        return f"Auf der {name} steht: {o[0]}."
    return f"Auf der {name} stehen {len(o)} Sachen: " + ", ".join(o[:-1]) + f" und {o[-1]}."


def siri(cfg, text, by, can_edit):
    """→ Antwort oder None (dann ist es kein Listen-Befehl)."""
    t = (text or "").strip().rstrip(".!?")
    ls = ensure(cfg)
    if re.search(r"was (?:müssen|sollen|wollten) wir (?:noch )?(?:einkaufen|kaufen|besorgen)|was brauchen wir", t, re.I):
        return speak(ls["einkauf"], "einkauf")
    m = SHOW_RE.search(t)
    if m and not ADD_RE.match(t):
        return speak(ls[which(m.group(2))], which(m.group(2)))
    m = ADD_RE.match(t)
    if m:
        if not can_edit:
            return "Das darfst du leider nicht."
        key = which(m.group("list"))
        lst = ls[key]
        new, dup = add(lst, split_items(m.group("what")), by)
        if not new and dup:
            return f"{', '.join(dup)} steht schon drauf."
        if not new:
            return None
        msg = (", ".join(new[:-1]) + " und " + new[-1] if len(new) > 1 else new[0]) + f" steht jetzt auf der {SPOKEN[key]}."
        if len(new) > 1:
            msg = msg.replace(" steht jetzt", " stehen jetzt")
        return msg + (f" {', '.join(dup)} war schon drauf." if dup else "")
    m = DONE_RE.match(t)
    if m and re.search(r"(liste|gekauft|erledigt|abgehakt|aufgaben|streich|hak)", t, re.I):
        if not can_edit:
            return "Das darfst du leider nicht."
        what = (m.group("what") or m.group("what2") or "").strip()
        for key in (("aufgaben", "einkauf") if re.search(r"aufgabe|erledigt", t, re.I) else ("einkauf", "aufgaben")):
            it = find(ls[key], what)
            if it:
                set_done(ls[key], it["id"], True, by)
                return f"{it['text']} ist abgehakt."
        return f"{what} habe ich auf keiner Liste gefunden."
    return None
