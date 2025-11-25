import sys

MAX_PREFIX_SIZE = 99999     # 5 ASCII digits
PREFIX_LEN = 5              # server expects exactly 5 bytes


def find_jpegs(data):
    """Find JPEG frames (SOI=FFD8, EOI=FFD9) and yield raw frame bytes."""
    i = 0
    n = len(data)

    while True:
        # find SOI
        while i + 1 < n and not (data[i] == 0xFF and data[i+1] == 0xD8):
            i += 1
        if i + 1 >= n:
            break
        start = i
        i += 2

        # find EOI
        while i + 1 < n and not (data[i] == 0xFF and data[i+1] == 0xD9):
            i += 1
        if i + 1 >= n:
            break

        end = i + 2
        i = end

        yield data[start:end]


def write_prefixed_frames(frames, out):
    frame_count = 0

    for frame in frames:
        L = len(frame)

        # If frame <= 99999 → write normally
        if L <= MAX_PREFIX_SIZE:
            prefix = f"{L:05d}".encode("ascii")
            out.write(prefix)
            out.write(frame)
            frame_count += 1
            continue

        # Otherwise split frame into multiple chunks
        pos = 0
        while pos < L:
            chunk = frame[pos:pos + MAX_PREFIX_SIZE]
            chunk_len = len(chunk)
            prefix = f"{chunk_len:05d}".encode("ascii")
            out.write(prefix)
            out.write(chunk)

            pos += MAX_PREFIX_SIZE
            frame_count += 1

    return frame_count


def convert(input_path, output_path):
    with open(input_path, "rb") as f:
        data = f.read()

    frames = list(find_jpegs(data))
    if not frames:
        print("No JPEG frames found.")
        return 1

    with open(output_path, "wb") as out:
        count = write_prefixed_frames(frames, out)

    print(f"Successfully wrote {count} prefixed frames → {output_path}")
    return 0


def main():
    if len(sys.argv) < 2:
        print("Usage: python convert.py input.mjpeg [output.mjpeg]")
        return

    inp = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "temp.Mjpeg"
    convert(inp, out)


if __name__ == "__main__":
    main()