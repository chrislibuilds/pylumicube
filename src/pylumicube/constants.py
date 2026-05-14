"""Protocol constants. See PROTOCOL.md."""

# Physical layer
BAUD = 3_000_000
SERIAL_DEVICE = "/dev/ttyAMA0"

# Frame budget
MAX_FRAME_SIZE = 256              # COBS-encoded frame, including overhead and 0x00 delimiter
MAX_DECODED_BODY = 254            # command + body + CRC (max), after COBS decode and excluding delimiter
# PROTOCOL.md §2.5 lists 245, matching the Java daemon. The cube firmware,
# however, discards frames whose between-delimiter byte count reaches 255 (it
# treats that as "oversize"), so the MCU drops a 245-byte payload silently.
# Keep ourselves one byte under the limit. Verified empirically: 244 bytes
# work, 245 bytes do not.
MAX_UAVCAN_PAYLOAD = 244

# Link layer command codes
CMD_PING = 0x00
CMD_INITIALISE = 0x1E
CMD_MESSAGE = 0x2D
CMD_ACKNOWLEDGE = 0xAA
CMD_INITIALISED = 0xB4
CMD_UNINITIALISED = 0xCC
CMD_PONG = 0xFF

PROTOCOL_VERSION = 1
FLUSH_COUNT = 256                  # PONGs to drain before sending INITIALISE
WINDOW_SIZE = 16                   # outstanding MESSAGE frames

# UAVCAN type IDs (TypeId.java)
TYPE_ALLOCATION = 1
TYPE_NODE_STATUS = 341
TYPE_PUBLISHED_FIELDS = 20_000
TYPE_GET_NODE_INFO = 1
TYPE_SUBSCRIBE_DEFAULT_FIELDS = 200
TYPE_GET_PREFERRED_NAME = 202
TYPE_ENUMERATE_FIELDS = 204
TYPE_SET_FIELDS = 216

DEFAULT_PRIORITY = 20              # what the Java daemon uses for almost all traffic

# Allocator convention: daemon reserves an ID for itself and counts down for nodes.
# The Java daemon (Allocator.java line 138) starts the candidate scan at 125.
DAEMON_NODE_ID = 1                 # observed in capture; daemon also self-allocates from the table
ALLOCATOR_FIRST_CANDIDATE = 125

# FieldType enum (FieldType.java)
FIELD_TYPE_NULL = 0
FIELD_TYPE_RAW = 1
FIELD_TYPE_ENUM = 2
FIELD_TYPE_BOOLEAN = 3
FIELD_TYPE_UINT = 4
FIELD_TYPE_INT = 5
FIELD_TYPE_FLOAT = 6
FIELD_TYPE_TIME = 7
FIELD_TYPE_UTF8_CHAR = 8
FIELD_TYPE_UTF8_STRING = 9
FIELD_TYPE_DICTIONARY = 10
