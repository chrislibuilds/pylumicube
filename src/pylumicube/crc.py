"""CRC-16/CCITT-FALSE.

Polynomial 0x1021, init 0xFFFF, no reflect, no final xor, big-endian on wire.
See PROTOCOL.md §2.1 and ref_data/lumicube-daemon/.../common/CRC16.java.
"""


def _build_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
        table.append(crc)
    return tuple(table)


_TABLE = _build_table()


def calculate(data: bytes | bytearray | memoryview, offset: int = 0, length: int | None = None) -> int:
    if length is None:
        length = len(data) - offset
    crc = 0xFFFF
    end = offset + length
    for index in range(offset, end):
        crc = (((crc << 8) & 0xFFFF) ^ _TABLE[((crc >> 8) ^ data[index]) & 0xFF])
    return crc
