#!/usr/bin/env python3
"""Minimal QR encoder — enough to put a pairing URL on a terminal.

Byte mode, error level L, versions 1-5 (up to 106 bytes). That range is all a
`http://host.local:8788/#t=<token>` URL needs, and staying inside it keeps the
encoder single-block: no interleaving, which is where hand-rolled QR usually
goes wrong.

Why this exists: pairing a phone means typing a 16-character token by hand,
once per phone. With three macbook-iphone pairs that is six chances to fat-finger
a secret and then debug a 401. A camera does not typo.

Stdlib only. Not a general QR library — it will refuse anything it cannot do.
"""
from __future__ import annotations

# version -> (data codewords, ec codewords) for error level L, single block
CAPACITY_L = {1: (19, 7), 2: (34, 10), 3: (55, 15), 4: (80, 20), 5: (108, 26)}
ALIGN_CENTER = {1: None, 2: 18, 3: 22, 4: 26, 5: 30}

# ── GF(256) ──────────────────────────────────────────────────
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


def _mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else EXP[LOG[a] + LOG[b]]


def _rs_generator(n: int) -> list[int]:
    poly = [1]
    for i in range(n):
        nxt = [0] * (len(poly) + 1)
        for j, c in enumerate(poly):
            nxt[j] ^= _mul(c, 1)
            nxt[j + 1] ^= _mul(c, EXP[i])
        poly = nxt
    return poly


def _rs_encode(data: list[int], n_ec: int) -> list[int]:
    gen = _rs_generator(n_ec)
    rem = list(data) + [0] * n_ec
    for i in range(len(data)):
        coef = rem[i]
        if coef:
            for j, g in enumerate(gen):
                rem[i + j] ^= _mul(g, coef)
    return rem[len(data):]


# ── bit stream ───────────────────────────────────────────────
def _codewords(payload: bytes, version: int) -> list[int]:
    n_data, n_ec = CAPACITY_L[version]
    bits: list[int] = []
    def put(value: int, width: int) -> None:
        for i in range(width - 1, -1, -1):
            bits.append((value >> i) & 1)

    put(0b0100, 4)              # byte mode
    put(len(payload), 8)        # count, 8 bits for versions 1-9
    for byte in payload:
        put(byte, 8)
    put(0, min(4, n_data * 8 - len(bits)))          # terminator
    bits.extend([0] * ((8 - len(bits) % 8) % 8))    # pad to a byte boundary

    words = [int("".join(str(b) for b in bits[i:i + 8]), 2) for i in range(0, len(bits), 8)]
    for pad in (0xEC, 0x11):
        while len(words) < n_data:
            words.append(pad)
            pad = 0x11 if pad == 0xEC else 0xEC
    words = words[:n_data]
    return words + _rs_encode(words, n_ec)


# ── matrix ───────────────────────────────────────────────────
def _blank(size: int):
    return [[None] * size for _ in range(size)], [[False] * size for _ in range(size)]


def _place_function_patterns(m, fixed, version: int) -> None:
    size = len(m)

    def finder(r0: int, c0: int) -> None:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                if not (0 <= r < size and 0 <= c < size):
                    continue
                on = (0 <= dr <= 6 and dc in (0, 6)) or (0 <= dc <= 6 and dr in (0, 6)) \
                     or (2 <= dr <= 4 and 2 <= dc <= 4)
                m[r][c] = 1 if on else 0
                fixed[r][c] = True

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    for i in range(8, size - 8):          # timing
        bit = 1 if i % 2 == 0 else 0
        m[6][i] = bit; fixed[6][i] = True
        m[i][6] = bit; fixed[i][6] = True

    centre = ALIGN_CENTER[version]
    if centre is not None:
        for dr in range(-2, 3):
            for dc in range(-2, 3):
                on = max(abs(dr), abs(dc)) != 1
                m[centre + dr][centre + dc] = 1 if on else 0
                fixed[centre + dr][centre + dc] = True

    m[size - 8][8] = 1                     # dark module
    fixed[size - 8][8] = True

    for i in range(9):                     # format-info seats
        for r, c in ((8, i), (i, 8)):
            if 0 <= r < size and 0 <= c < size and not fixed[r][c]:
                fixed[r][c] = True; m[r][c] = 0
    for i in range(8):
        for r, c in ((8, size - 1 - i), (size - 1 - i, 8)):
            if not fixed[r][c]:
                fixed[r][c] = True; m[r][c] = 0


def _place_data(m, fixed, words: list[int]) -> None:
    size = len(m)
    bits = [(w >> i) & 1 for w in words for i in range(7, -1, -1)]
    idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if not fixed[row][c]:
                    m[row][c] = bits[idx] if idx < len(bits) else 0
                    idx += 1
        upward = not upward
        col -= 2


MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def _format_bits(mask: int) -> list[int]:
    # error level L is 01; 15-bit BCH(15,5) with the standard mask 0x5412
    value = (0b01 << 3) | mask
    rem = value << 10
    for i in range(4, -1, -1):
        if rem & (1 << (i + 10)):
            rem ^= 0b10100110111 << i
    bits = ((value << 10) | (rem & 0x3FF)) ^ 0b101010000010010
    return [(bits >> (14 - i)) & 1 for i in range(15)]


def _apply_format(m, mask: int) -> None:
    size = len(m)
    bits = _format_bits(mask)
    for i in range(6):
        m[8][i] = bits[i]
    m[8][7] = bits[6]; m[8][8] = bits[7]; m[7][8] = bits[8]
    for i in range(9, 15):
        m[14 - i][8] = bits[i]
    for i in range(8):
        m[size - 1 - i][8] = bits[i]
    for i in range(8, 15):
        m[8][size - 15 + i] = bits[i]
    m[size - 8][8] = 1


def _penalty(m) -> int:
    size = len(m)
    score = 0
    for line in list(m) + [list(col) for col in zip(*m)]:
        run, prev = 0, None
        for v in line:
            if v == prev:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run, prev = 1, v
        if run >= 5:
            score += 3 + (run - 5)
    for r in range(size - 1):
        for c in range(size - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    dark = sum(sum(row) for row in m)
    score += 10 * (abs(dark * 100 // (size * size) - 50) // 5)
    return score


def encode(text: str) -> list[list[int]]:
    """Return the QR modules as a matrix of 0/1. Raises if the text is too long."""
    payload = text.encode()
    version = next((v for v in sorted(CAPACITY_L) if len(payload) <= CAPACITY_L[v][0] - 2), None)
    if version is None:
        raise ValueError(f"{len(payload)} bytes is beyond this encoder (max {CAPACITY_L[5][0] - 2})")
    words = _codewords(payload, version)
    size = version * 4 + 17

    best = None
    for mask in range(8):
        m, fixed = _blank(size)
        _place_function_patterns(m, fixed, version)
        _place_data(m, fixed, words)
        for r in range(size):
            for c in range(size):
                if not fixed[r][c] and MASKS[mask](r, c):
                    m[r][c] ^= 1
        _apply_format(m, mask)
        score = _penalty(m)
        if best is None or score < best[0]:
            best = (score, m)
    return best[1]


def render(text: str, quiet: int = 4) -> str:
    """QR as half-block text — two module rows per terminal line."""
    m = encode(text)
    size = len(m)
    grid = [[0] * (size + quiet * 2) for _ in range(quiet)]
    for row in m:
        grid.append([0] * quiet + list(row) + [0] * quiet)
    grid.extend([[0] * (size + quiet * 2) for _ in range(quiet)])
    if len(grid) % 2:
        grid.append([0] * len(grid[0]))

    out = []
    for i in range(0, len(grid), 2):
        top, bottom = grid[i], grid[i + 1]
        line = []
        for t, b in zip(top, bottom):
            # dark module = no light: invert, because terminals are usually dark
            t, b = 1 - t, 1 - b
            line.append("█" if t and b else "▀" if t else "▄" if b else " ")
        out.append("".join(line))
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    print(render(sys.argv[1] if len(sys.argv) > 1 else "https://example.com"))
