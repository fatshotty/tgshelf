# Telegram upload part size: SaveBigFilePart requires equal power-of-two parts
# (max 512KiB); only the last part may be shorter.
PART_SIZE = 512 * 1024

# Download chunk for upload.GetFile with precise=False: limit must be 1MiB and
# offsets must be multiples of it; the exact HTTP range is trimmed client-side.
CHUNK_SIZE = 1024 * 1024

# Node ids: legacy 10-char format is preserved (old .strm URLs embed them).
# ROOT_ID is recognised by id only, never by name.
ROOT_ID = "0000000000"
ROOT_NAME = "root"
NODE_ID_LENGTH = 10
NODE_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
