# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
qr.py – kleiner QR-Code-Erzeuger ohne Zusatzpakete (Byte-Modus, Fehlerkorrektur M, Version 1–15).

    m = qr.encode("WIFI:T:WPA;S:Gast;P:geheim;;")   # Liste von Zeilen, True = dunkel
    qr.svg(m)                                      # SVG-Text
    qr.wifi_text(ssid, password, security="WPA")   # WLAN-Text für Handykameras
"""

# Version: (EC-Codewörter je Block, [(Anzahl Blöcke, Datencodewörter je Block), ...]) – Stufe M
_M = {1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]), 5: (24, [(2, 43)]),
      6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]), 9: (22, [(3, 36), (2, 37)]),
      10: (26, [(4, 43), (1, 44)]), 11: (30, [(1, 50), (4, 51)]), 12: (22, [(6, 36), (2, 37)]),
      13: (22, [(8, 37), (1, 38)]), 14: (24, [(4, 40), (5, 41)]), 15: (24, [(5, 41), (5, 42)])}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
          9: [6, 26, 46], 10: [6, 28, 50], 11: [6, 30, 54], 12: [6, 32, 58], 13: [6, 34, 62], 14: [6, 26, 46, 66],
          15: [6, 26, 48, 70]}
_ECL_BITS = 0  # M


def _gf_mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z & 0xFF


def _rs_divisor(degree):
    res = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            res[j] = _gf_mul(res[j], root)
            if j + 1 < degree:
                res[j] ^= res[j + 1]
        root = _gf_mul(root, 0x02)
    return res


def _rs_remainder(data, div):
    res = [0] * len(div)
    for b in data:
        f = b ^ res.pop(0)
        res.append(0)
        for i, c in enumerate(div):
            res[i] ^= _gf_mul(c, f)
    return res


def _capacity(v):
    _, groups = _M[v]
    return sum(n * d for n, d in groups)


def _codewords(data):
    for v in range(1, 16):
        cc = 8 if v < 10 else 16
        if 4 + cc + 8 * len(data) <= _capacity(v) * 8:
            break
    else:
        raise ValueError("Text zu lang für den QR-Code")
    cap = _capacity(v) * 8
    bits = [0, 1, 0, 0] + [(len(data) >> i) & 1 for i in range(cc - 1, -1, -1)]
    for b in data:
        bits += [(b >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, cap - len(bits))
    bits += [0] * (-len(bits) % 8)
    cw = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(cw) < cap // 8:
        cw.append(pad)
        pad ^= 0xEC ^ 0x11
    ec_len, groups = _M[v]
    div = _rs_divisor(ec_len)
    blocks, pos = [], 0
    for n, d in groups:
        for _ in range(n):
            blocks.append(cw[pos:pos + d])
            pos += d
    ecs = [_rs_remainder(b, div) for b in blocks]
    out = []
    for i in range(max(len(b) for b in blocks)):
        out += [b[i] for b in blocks if i < len(b)]
    for i in range(ec_len):
        out += [e[i] for e in ecs]
    return v, out


def _mask_bit(m, x, y):
    return [(x + y) % 2 == 0, y % 2 == 0, x % 3 == 0, (x + y) % 3 == 0, (x // 3 + y // 2) % 2 == 0,
            x * y % 2 + x * y % 3 == 0, (x * y % 2 + x * y % 3) % 2 == 0, ((x + y) % 2 + x * y % 3) % 2 == 0][m]


class _Grid:
    def __init__(self, v):
        self.v, self.n = v, v * 4 + 17
        self.m = [[False] * self.n for _ in range(self.n)]
        self.f = [[False] * self.n for _ in range(self.n)]

    def fn(self, x, y, dark):
        self.m[y][x], self.f[y][x] = dark, True

    def functions(self):
        n = self.n
        for i in range(n):
            self.fn(6, i, i % 2 == 0)
            self.fn(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self.fn(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        al = _ALIGN[self.v]
        last = len(al) - 1
        for i, ay in enumerate(al):
            for j, ax in enumerate(al):
                if (i == 0 and j == 0) or (i == 0 and j == last) or (i == last and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.fn(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.format_bits(0)
        if self.v >= 7:
            rem = self.v
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.v << 12 | rem
            for i in range(18):
                b = (bits >> i) & 1 == 1
                a, c = n - 11 + i % 3, i // 3
                self.fn(a, c, b)
                self.fn(c, a, b)

    def format_bits(self, mask):
        data = _ECL_BITS << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        g = lambda i: (bits >> i) & 1 == 1  # noqa: E731
        n = self.n
        for i in range(6):
            self.fn(8, i, g(i))
        self.fn(8, 7, g(6))
        self.fn(8, 8, g(7))
        self.fn(7, 8, g(8))
        for i in range(9, 15):
            self.fn(14 - i, 8, g(i))
        for i in range(8):
            self.fn(n - 1 - i, 8, g(i))
        for i in range(8, 15):
            self.fn(8, n - 15 + i, g(i))
        self.fn(8, n - 8, True)

    def place(self, cw):
        n, i, total = self.n, 0, len(cw) * 8
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    up = ((right + 1) & 2) == 0
                    y = n - 1 - vert if up else vert
                    if not self.f[y][x] and i < total:
                        self.m[y][x] = (cw[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask):
        for y in range(self.n):
            for x in range(self.n):
                if not self.f[y][x] and _mask_bit(mask, x, y):
                    self.m[y][x] = not self.m[y][x]

    def penalty(self):
        n, m, p = self.n, self.m, 0
        lines = [row for row in m] + [[m[y][x] for y in range(n)] for x in range(n)]
        pat1, pat2 = [True, False, True, True, True, False, True, False, False, False, False], \
            [False, False, False, False, True, False, True, True, True, False, True]
        for line in lines:
            run, prev = 0, None
            for c in line:
                if c == prev:
                    run += 1
                else:
                    if run >= 5:
                        p += run - 2
                    run, prev = 1, c
            if run >= 5:
                p += run - 2
            for i in range(n - 10):
                seg = line[i:i + 11]
                if seg == pat1 or seg == pat2:
                    p += 40
        for y in range(n - 1):
            for x in range(n - 1):
                c = m[y][x]
                if c == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    p += 3
        dark = sum(sum(r) for r in m)
        p += abs(dark * 20 - n * n * 10) // (n * n) * 10
        return p


def encode(text):
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    v, cw = _codewords(data)
    best = None
    for mask in range(8):
        g = _Grid(v)
        g.functions()
        g.place(cw)
        g.apply_mask(mask)
        g.format_bits(mask)
        pen = g.penalty()
        if best is None or pen < best[0]:
            best = (pen, g)
    return best[1].m


def svg(matrix, size=None, border=4, dark="#000", light="#fff"):
    n = len(matrix) + 2 * border
    path = "".join(f"M{x + border},{y + border}h1v1h-1z" for y, row in enumerate(matrix)
                   for x, c in enumerate(row) if c)
    wh = f' width="{size}" height="{size}"' if size else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}"{wh} shape-rendering="crispEdges">'
            f'<rect width="{n}" height="{n}" fill="{light}"/><path d="{path}" fill="{dark}"/></svg>')


def _esc(s):
    return "".join("\\" + c if c in '\\;,:"' else c for c in s)


def wifi_text(ssid, password, security="WPA", hidden=False):
    """Text, den iPhone- und Android-Kameras als „WLAN beitreten“ erkennen."""
    if not password or security in ("", "nopass", "None", "none"):
        t = f"WIFI:T:nopass;S:{_esc(ssid)};;"
    else:
        t = f"WIFI:T:{security};S:{_esc(ssid)};P:{_esc(password)};;"
    if hidden:
        t = t[:-1] + "H:true;;"
    return t
