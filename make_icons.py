"""Генерирует иконки 192x192 и 512x512 без сторонних зависимостей."""
import struct, zlib, math

def png(size):
    def chunk(name, data):
        c = struct.pack(">I", len(data)) + name + data
        return c + struct.pack(">I", zlib.crc32(c[4:]) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))

    cx, cy, r = size / 2, size / 2, size * 0.42
    rows = []
    for y in range(size):
        row = [0]
        for x in range(size):
            dx, dy = x - cx, y - cy
            dist = math.sqrt(dx*dx + dy*dy)
            if dist <= r:
                # Apple blue #0071e3
                row += [0x00, 0x71, 0xe3]
            else:
                # background #f5f5f7
                row += [0xf5, 0xf5, 0xf7]
        rows.append(bytes(row))

    raw = b"".join(rows)
    compressed = zlib.compress(raw, 9)
    idat = chunk(b"IDAT", compressed)
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend

for size, name in [(192, "icon-192.png"), (512, "icon-512.png")]:
    path = f"static/icons/{name}"
    with open(path, "wb") as f:
        f.write(png(size))
    print(f"Created {path}")
