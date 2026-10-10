"""Split the HPC dataset into chronological train, validation, and test sets."""

from pathlib import Path


def parse_timestamp(line: str) -> int:
    """Extract unix timestamp from an HPC log line."""
    tokens = line.strip().split()
    for tok in tokens[1:6]:
        if tok.isdigit() and len(tok) == 10 and tok.startswith(("10", "11", "12")):
            return int(tok)
    return 0


def split_hpc_dataset(
    input_file: Path = Path("data/HPC.log"),
    output_dir: Path = Path("data/splits"),
    train_ratio: float = 0.70,
    val_ratio: float = 0.15,
) -> tuple[int, int, int]:
    """Sort logs chronologically and create train, val, and test splits."""
    output_dir.mkdir(parents=True, exist_ok=True)

    records: list[tuple[int, int, str]] = []
    with open(input_file, encoding="utf-8", errors="ignore") as f:
        for idx, line in enumerate(f):
            ts = parse_timestamp(line)
            records.append((ts, idx, line))

    # Chronological sort preserving log arrival order on timestamp ties
    records.sort(key=lambda x: (x[0], x[1]))

    total = len(records)
    n_train = int(total * train_ratio)
    n_val = int(total * val_ratio)

    train_records = records[:n_train]
    val_records = records[n_train : n_train + n_val]
    test_records = records[n_train + n_val :]

    for filename, recs in [
        ("train.log", train_records),
        ("val.log", val_records),
        ("test.log", test_records),
    ]:
        with open(output_dir / filename, "w", encoding="utf-8") as out:
            for _, _, line in recs:
                out.write(line)

    return len(train_records), len(val_records), len(test_records)


if __name__ == "__main__":
    n_train, n_val, n_test = split_hpc_dataset()
    print(f"Data split successfully: {n_train} train, {n_val} val, {n_test} test.")
