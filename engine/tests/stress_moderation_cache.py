import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.game import lore, moderation

PACK = "zzcachepack"
TERMS = ["zzqterm" + c + "x" for c in "bdfghjknpsvwy"]


def forms(term):
    return term, term.replace("e", "3"), " ".join(term)


def main():
    monkeypatch = pytest.MonkeyPatch()
    failures = []
    counts = []
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lists = root / PACK / "moderation"
            lists.mkdir(parents=True)
            path = lists / "blocklist.en.txt"
            path.write_text("zzqbaseword\n", encoding="utf-8")
            (lists / "always_blocklist.en.txt").write_text("zzqalwaysbase\n", encoding="utf-8")
            monkeypatch.setattr(lore, "LORE_ROOT", root)
            monkeypatch.setattr(moderation, "DEFAULT_PACK_ID", PACK)
            moderation.reset_pack_cache()
            published = 0
            lock = threading.Lock()
            start = threading.Barrier(9)

            def writer():
                nonlocal published
                start.wait()
                for j in range(len(TERMS)):
                    time.sleep(0.01)
                    temporary = path.with_suffix(".tmp")
                    temporary.write_text(
                        "zzqbaseword\n" + "\n".join(TERMS[: j + 1]) + "\n", encoding="utf-8"
                    )
                    os.replace(temporary, path)
                    moderation.reset_pack_cache()
                    with lock:
                        published = j + 1
                return published

            def worker(index):
                start.wait()
                for i in range(5000):
                    with lock:
                        p = published
                    if i % 4 == 0:
                        text = "the quiet rain falls n%d" % (index * 5000 + i)
                        result = moderation.check_input_cached(text, pack_id=PACK)
                        assert not result.blocked, (index, i, "clean text blocked")
                    else:
                        j = (i + index) % len(TERMS)
                        form = (i // len(TERMS) + index) % 3
                        result = moderation.check_input_cached(forms(TERMS[j])[form], pack_id=PACK)
                        assert j >= p or result.blocked, (
                            index,
                            i,
                            j,
                            p,
                            form,
                            "published term passed",
                        )
                return 5000

            with ThreadPoolExecutor(max_workers=9) as pool:
                write = pool.submit(writer)
                workers = [(index, pool.submit(worker, index)) for index in range(8)]
                for index, future in workers:
                    try:
                        counts.append(future.result())
                    except (Exception, moderation.ListsUnavailable) as exc:
                        failures.append(("worker %d" % index, type(exc).__name__, str(exc)))
                try:
                    assert write.result() == 13
                except (Exception, moderation.ListsUnavailable) as exc:
                    failures.append(("writer", type(exc).__name__, str(exc)))
            for term in TERMS:
                for text in forms(term):
                    assert moderation.check_input_cached(text, pack_id=PACK).blocked, text
            assert len(moderation._VERDICTS) <= moderation._VERDICT_MAX
            assert sum(counts) == 40000 or failures
    except (Exception, moderation.ListsUnavailable) as exc:
        failures.append(("final checks", type(exc).__name__, str(exc)))
    finally:
        monkeypatch.undo()
        moderation.reset_pack_cache()
    if failures:
        for place, name, message in failures:
            print("FAIL %s: %s: %s" % (place, name, message))
        return 1
    print("PASS moderation_cache calls=40000 workers=8 edits=13")
    print("PASS published terms blocked in plain, leet and spaced forms; clean text passes")
    print("PASS verdict cache stays within its bound")
    return 0


if __name__ == "__main__":
    sys.exit(main())
