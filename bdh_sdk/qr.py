"""Site-branded QR codes (install the ``bdh-sdk[qr]`` extra)."""
from .branding import monogram_svg

QR_COLOR = "#0b1622"
QR_BORDER = 4  # quiet zone, in modules (the QR spec minimum)
LOGO_FRACTION = 0.26  # logo width as a share of the code width; safe with ECC level H
LONG_TEXT = 200  # longer text drops to ECC L with a smaller logo, so the code isn't too dense to scan
LONG_LOGO_FRACTION = 0.14
ECC_BUDGET = 0.4  # share of each block's correctable codewords the logo may use; the rest is for real-world noise


def _inner(svg):
    """Contents of an SVG document (everything inside the root <svg>)."""
    return svg[svg.index(">") + 1:svg.rindex("</svg>")]


def _codewords(qr):
    """For a made QRCode: {(row, col): codeword index} for every data module
    (border excluded) and the RS block owning each codeword. Mirrors the
    spec's placement: zigzag up/down column pairs from the bottom right,
    skipping column 6 and function patterns; codewords are interleaved data
    first, then error correction, one per block in turn."""
    from copy import copy

    from qrcode.base import rs_blocks

    blank = copy(qr)
    blank.map_data = lambda data, mask: None  # function patterns only
    blank.makeImpl(False, 0)
    n, grid = blank.modules_count, blank.modules

    blocks = rs_blocks(qr.version, qr.error_correction)
    owner = []
    for i in range(max(b.data_count for b in blocks)):
        owner += [k for k, b in enumerate(blocks) if i < b.data_count]
    for i in range(max(b.total_count - b.data_count for b in blocks)):
        owner += [k for k, b in enumerate(blocks) if i < b.total_count - b.data_count]

    where, bit, up, col = {}, 0, True, n - 1
    while col > 0:
        if col == 6:
            col -= 1
        for r in range(n - 1, -1, -1) if up else range(n):
            for c in (col, col - 1):
                if grid[r][c] is None:
                    if bit // 8 < len(owner):  # trailing remainder bits carry nothing
                        where[r, c] = bit // 8
                    bit += 1
        up, col = not up, col - 2
    return where, owner, blocks


def _hole_fits(n, hole, where, owner, blocks):
    """Whether hiding a centred hole (plus its one-module margin) on an n-module
    symbol damages no more than ECC_BUDGET of the codewords each block can correct."""
    h0 = (n - hole) // 2
    damaged = {where[r, c] for r in range(h0 - 1, h0 + hole + 1) for c in range(h0 - 1, h0 + hole + 1)
               if (r, c) in where}
    per_block = [0] * len(blocks)
    for cw in damaged:
        per_block[owner[cw]] += 1
    return all(hit <= (b.total_count - b.data_count) // 2 * ECC_BUDGET for hit, b in zip(per_block, blocks))


def logo_hole(qr, fraction):
    """Largest odd hole (in modules) up to ``fraction`` of the symbol width that
    error correction can always recover, whatever the payload and mask."""
    n = qr.modules_count
    where, owner, blocks = _codewords(qr)
    hole = round(n * fraction) | 1
    while hole > 1 and not _hole_fits(n, hole, where, owner, blocks):
        hole -= 2
    return hole


def qr_svg(text, logo=None):
    """SVG of a QR code for text: round dots, square finder patterns and the
    site's monogram (or ``logo``, SVG text with a 100x100 viewBox) over a
    cleared centre. Error correction H (30%) keeps it scannable with the
    centre covered; text over 200 characters uses L and a smaller logo."""
    import qrcode

    long = len(text) > LONG_TEXT
    level = qrcode.constants.ERROR_CORRECT_L if long else qrcode.constants.ERROR_CORRECT_H
    qr = qrcode.QRCode(error_correction=level, border=QR_BORDER)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    finders = [(QR_BORDER, QR_BORDER), (QR_BORDER, n - QR_BORDER - 7), (n - QR_BORDER - 7, QR_BORDER)]

    # Odd, so it centres on a module; sized from the real codeword layout:
    # a fixed share of the width overloads one RS block on small symbols.
    hole = logo_hole(qr, LONG_LOGO_FRACTION if long else LOGO_FRACTION)
    h0 = (n - hole) // 2
    h1 = h0 + hole

    def skip(r, c):
        in_finder = any(fr <= r < fr + 7 and fc <= c < fc + 7 for fr, fc in finders)
        in_hole = h0 - 1 <= r <= h1 and h0 - 1 <= c <= h1  # plus a one-module margin
        return in_finder or in_hole

    dots = "".join(
        f'<circle cx="{c + .5}" cy="{r + .5}" r=".46"/>'
        for r, row in enumerate(matrix)
        for c, on in enumerate(row)
        if on and not skip(r, c)
    )
    # Finder patterns stay square: rounded corners break some decoders (OpenCV).
    eyes = "".join(
        f'<rect x="{c + .5}" y="{r + .5}" width="6" height="6" fill="none" stroke="{QR_COLOR}" stroke-width="1"/>'
        f'<rect x="{c + 2}" y="{r + 2}" width="3" height="3"/>'
        for r, c in finders
    )
    pad = 0.6
    size = hole + 2 - 2 * pad
    origin = h0 - 1 + pad
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="geometricPrecision">'
        f'<rect width="{n}" height="{n}" fill="#fff"/>'
        f'<g fill="{QR_COLOR}">{dots}</g>'
        f'<g fill="{QR_COLOR}">{eyes}</g>'
        f'<svg x="{origin}" y="{origin}" width="{size}" height="{size}" viewBox="0 0 100 100" class="qr-logo">'
        f'{_inner(logo or monogram_svg())}</svg></svg>'
    )
