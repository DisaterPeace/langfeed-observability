"""Tiny helper: load a repo's source files into a single text blob for the agents."""

import os


def read_repo(path: str, exts=(".py",)) -> str:
    """Concatenate all source files under `path` into one labeled string."""
    chunks = []
    for root, _dirs, files in os.walk(path):
        for fname in sorted(files):
            if fname.endswith(exts):
                full = os.path.join(root, fname)
                with open(full, "r", encoding="utf-8") as f:
                    chunks.append(f"### FILE: {full}\n{f.read()}")
    return "\n\n".join(chunks)
