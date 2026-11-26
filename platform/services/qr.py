"""QR-Code ohne Fremdbibliothek (ISO/IEC 18004): Byte-Modus, Fehlerkorrektur
Stufe M, Versionen 1–10, also bis 213 Byte — für eine Kurzlink-Adresse mehr
als genug. Liefert die Modulmatrix sowie SVG und PNG daraus.

Übernommen aus der App wegweiser (oaap-apps/apps/wegweiser/qr.py, dort seit
2026-09 im Betrieb und Bit für Bit gegen segno geprüft) für die Kopplung
(RFC-0136 F6): der QR-Code trägt den Browser-Link eines Tickets.

    matrix = encode("https://go.example.org/r/abc123")
    png(matrix)  # bytes
    svg(matrix)  # str
"""
import struct
import zlib

# Je Version: (EC-Codewörter je Block, [(Anzahl Blöcke, Datencodewörter je Block), …]) — Stufe M
BLOCKS = {
    1: (10, [(1, 16)]),
    2: (16, [(1, 28)]),
    3: (26, [(1, 44)]),
    4: (18, [(2, 32)]),
    5: (24, [(2, 43)]),
    6: (16, [(4, 27)]),
    7: (18, [(4, 31)]),
    8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}
ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
         7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}
MAX_BYTES = 213

# ---- GF(256) mit dem Polynom 0x11D

EXP = [0] * 512
LOG = [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return EXP[LOG[a] + LOG[b]]


def generator(n):
    """Generatorpolynom (x - a^0)(x - a^1)…(x - a^(n-1)), Koeffizienten vom höchsten Grad."""
    g = [1]
    for i in range(n):
        ng = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            ng[j] ^= c
            ng[j + 1] ^= gf_mul(c, EXP[i])
        g = ng
    return g


def rs_encode(data, n_ec):
    g = generator(n_ec)
    rem = list(data) + [0] * n_ec
    for i in range(len(data)):
        c = rem[i]
        if c:
            for j in range(1, len(g)):
                rem[i + j] ^= gf_mul(g[j], c)
    return rem[len(data):]


# ---- Daten

def capacity(version):
    total = sum(n * k for n, k in BLOCKS[version][1])
    return (total * 8 - 4 - (16 if version >= 10 else 8)) // 8


def choose_version(n_bytes):
    for v in range(1, 11):
        if n_bytes <= capacity(v):
            return v
    raise ValueError(f"Zu lang für einen QR-Code: {n_bytes} Byte, höchstens {MAX_BYTES}")


def data_codewords(payload, version):
    total = sum(n * k for n, k in BLOCKS[version][1])
    bits = []

    def put(val, n):
        for i in range(n - 1, -1, -1):
            bits.append((val >> i) & 1)

    put(0b0100, 4)
    put(len(payload), 16 if version >= 10 else 8)
    for b in payload:
        put(b, 8)
    put(0, min(4, total * 8 - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    i = 0
    while len(words) < total:
        words.append((0xEC, 0x11)[i % 2])
        i += 1
    return words


def final_codewords(words, version):
    ec, groups = BLOCKS[version]
    blocks = []
    pos = 0
    for n, k in groups:
        for _ in range(n):
            blocks.append(words[pos:pos + k])
            pos += k
    ecs = [rs_encode(b, ec) for b in blocks]
    out = []
    for i in range(max(len(b) for b in blocks)):
        for b in blocks:
            if i < len(b):
                out.append(b[i])
    for i in range(ec):
        for e in ecs:
            out.append(e[i])
    return out


# ---- Matrix

class _Grid:
    def __init__(self, version):
        self.version = version
        self.size = version * 4 + 17
        n = self.size
        self.m = [[0] * n for _ in range(n)]
        self.func = [[False] * n for _ in range(n)]

    def setf(self, r, c, v):
        self.m[r][c] = v
        self.func[r][c] = True

    def draw_functions(self):
        n = self.size
        for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)):
            for dr in range(-1, 8):
                for dc in range(-1, 8):
                    r, c = r0 + dr, c0 + dc
                    if 0 <= r < n and 0 <= c < n:
                        inside = 0 <= dr <= 6 and 0 <= dc <= 6
                        dark = inside and (dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4))
                        self.setf(r, c, 1 if dark else 0)
        for i in range(8, n - 8):
            self.setf(6, i, 1 if i % 2 == 0 else 0)
            self.setf(i, 6, 1 if i % 2 == 0 else 0)
        pos = ALIGN[self.version]
        last = pos[-1] if pos else None
        for r in pos:
            for c in pos:
                if (r == 6 and c in (6, last)) or (c == 6 and r == last):
                    continue
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        self.setf(r + dr, c + dc, 0 if max(abs(dr), abs(dc)) == 1 else 1)
        # Platz für Formatinformation (wird je Maske neu geschrieben)
        for i in range(9):
            self.setf(8, i, 0) if not self.func[8][i] else None
            self.setf(i, 8, 0) if not self.func[i][8] else None
        for i in range(8):
            self.setf(8, n - 1 - i, 0)
            self.setf(n - 1 - i, 8, 0)
        self.setf(n - 8, 8, 1)
        if self.version >= 7:
            self.draw_version()

    def draw_version(self):
        rem = self.version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = (self.version << 12) | rem
        n = self.size
        for i in range(18):
            bit = (bits >> i) & 1
            a = n - 11 + i % 3
            b = i // 3
            self.setf(b, a, bit)
            self.setf(a, b, bit)

    def draw_format(self, mask):
        data = (0b00 << 3) | mask  # Stufe M = 00
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = ((data << 10) | rem) ^ 0x5412
        n = self.size

        def bit(i):
            return (bits >> i) & 1

        for i in range(6):
            self.setf(i, 8, bit(i))
        self.setf(7, 8, bit(6))
        self.setf(8, 8, bit(7))
        self.setf(8, 7, bit(8))
        for i in range(9, 15):
            self.setf(8, 14 - i, bit(i))
        for i in range(8):
            self.setf(8, n - 1 - i, bit(i))
        for i in range(8, 15):
            self.setf(n - 15 + i, 8, bit(i))
        self.setf(n - 8, 8, 1)

    def place(self, codewords):
        n = self.size
        bits = [(w >> (7 - k)) & 1 for w in codewords for k in range(8)]
        idx = 0
        col = n - 1
        upward = True
        while col > 0:
            if col == 6:
                col -= 1
            rows = range(n - 1, -1, -1) if upward else range(n)
            for r in rows:
                for c in (col, col - 1):
                    if not self.func[r][c]:
                        self.m[r][c] = bits[idx] if idx < len(bits) else 0
                        idx += 1
            col -= 2
            upward = not upward

    def apply_mask(self, mask):
        n = self.size
        for r in range(n):
            for c in range(n):
                if self.func[r][c]:
                    continue
                if mask == 0:
                    inv = (r + c) % 2 == 0
                elif mask == 1:
                    inv = r % 2 == 0
                elif mask == 2:
                    inv = c % 3 == 0
                elif mask == 3:
                    inv = (r + c) % 3 == 0
                elif mask == 4:
                    inv = (r // 2 + c // 3) % 2 == 0
                elif mask == 5:
                    inv = (r * c) % 2 + (r * c) % 3 == 0
                elif mask == 6:
                    inv = ((r * c) % 2 + (r * c) % 3) % 2 == 0
                else:
                    inv = ((r + c) % 2 + (r * c) % 3) % 2 == 0
                if inv:
                    self.m[r][c] ^= 1

    def penalty(self):
        n = self.size
        m = self.m
        total = 0
        lines = [row[:] for row in m] + [[m[r][c] for r in range(n)] for c in range(n)]
        pat = [1, 0, 1, 1, 1, 0, 1]
        for line in lines:
            run = 1
            for i in range(1, n):
                if line[i] == line[i - 1]:
                    run += 1
                    if run == 5:
                        total += 3
                    elif run > 5:
                        total += 1
                else:
                    run = 1
            # 1:1:3:1:1-Muster mit heller Fläche von 4 Modulen davor oder danach;
            # die Ruhezone jenseits des Randes zählt als hell (ISO 18004:2015, zxing)
            for i in range(n - 6):
                if line[i:i + 7] == pat and (not any(line[max(i - 4, 0):i]) or not any(line[i + 7:i + 11])):
                    total += 40
        for r in range(n - 1):
            for c in range(n - 1):
                if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                    total += 3
        dark = sum(sum(row) for row in m)
        total += int(abs(dark * 100 / (n * n) - 50) // 5) * 10
        return total


def encode(text, mask=None):
    """Text (UTF-8) → Modulmatrix als Liste von Zeilen mit 0/1.
    mask erzwingt eine der acht Masken (sonst die mit der geringsten Strafe)."""
    payload = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    version = choose_version(len(payload))
    words = final_codewords(data_codewords(payload, version), version)
    g = _Grid(version)
    g.draw_functions()
    g.place(words)
    best, best_score = None, None
    for mask in (range(8) if mask is None else (mask,)):
        g.apply_mask(mask)
        g.draw_format(mask)
        score = g.penalty()
        if best_score is None or score < best_score:
            best, best_score = [row[:] for row in g.m], score
        g.apply_mask(mask)
    return best


def svg(matrix, scale=8, quiet=4):
    n = len(matrix)
    s = n + 2 * quiet
    d = "".join(f"M{c + quiet} {r + quiet}h1v1h-1z"
                for r, row in enumerate(matrix) for c, v in enumerate(row) if v)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {s} {s}" width="{s * scale}" '
            f'height="{s * scale}" shape-rendering="crispEdges">'
            f'<rect width="{s}" height="{s}" fill="#fff"/><path d="{d}" fill="#000"/></svg>')


def png(matrix, scale=8, quiet=4):
    """PNG, 1 Bit Graustufe: 0 = schwarz, 1 = weiß."""
    n = len(matrix)
    s = (n + 2 * quiet) * scale
    raw = bytearray()
    for r in range(n + 2 * quiet):
        bits = []
        for c in range(n + 2 * quiet):
            inside = quiet <= r < n + quiet and quiet <= c < n + quiet
            v = matrix[r - quiet][c - quiet] if inside else 0
            bits.extend([0 if v else 1] * scale)
        row = bytearray(b"\x00")
        for i in range(0, len(bits), 8):
            chunk = bits[i:i + 8]
            chunk += [0] * (8 - len(chunk))
            row.append(int("".join(map(str, chunk)), 2))
        raw += row * scale

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", s, s, 1, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    import sys
    text = sys.argv[1] if len(sys.argv) > 1 else "https://example.org/"
    for row in encode(text):
        print("".join("██" if v else "  " for v in row))
