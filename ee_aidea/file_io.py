"""ファイルだけで承認を回すモード (Discord などの通信なし)。

- 人間は `python -m ee_aidea.approval_cli reply <返事>` を実行する。返事は inbox (既定 data/inbox.txt) に1行追記される。
- 承認係 (`run` / `run --once`) は inbox の新しい行を読み、承認係に渡す。知らせは画面と outbox (既定 data/outbox.log) に出す。
- 送り手・場所は固定の "local"。

限界: 同じPCでファイルを書けるもの (AI のサブエージェントを含む) は、inbox に承認を書けてしまう。
このモードは「一人で、AI に inbox・鍵・設定を書かせない設定にして使う練習用」。README の「正直な限界」を参照。
"""
import json
from pathlib import Path

from .approval import LOCAL, locked, PacketError

MAX_TEXT = 200


def append_inbox(path, text, now):
    """返事を1行追記する。(通し番号, 時刻) を返す。"""
    text = str(text).strip()
    if not text or len(text) > MAX_TEXT or any(c in text for c in "\r\n\x00  \x85"):
        raise PacketError("返事は1行・%d文字まで" % MAX_TEXT)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with locked(p):
        rows = read_all(p)
        n = (rows[-1]["n"] + 1) if rows else 1
        ts = now.isoformat(timespec="seconds")
        with open(p, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"n": n, "ts": ts, "text": text}, ensure_ascii=False) + "\n")
    return n, ts


def read_all(path):
    """inbox の全行。読めない行は飛ばす (承認として扱わない)。通し番号が増えていない行も飛ばす。"""
    p = Path(path)
    if not p.exists():
        return []
    out, last = [], 0
    try:
        text = p.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for l in text.split("\n"):
        if not l.strip():
            continue
        try:
            r = json.loads(l)
        except ValueError:
            continue
        if not isinstance(r, dict) or isinstance(r.get("n"), bool) or not isinstance(r.get("n"), int) \
                or not isinstance(r.get("ts"), str) or not isinstance(r.get("text"), str) or r["n"] <= last:
            continue
        last = r["n"]
        out.append(r)
    return out


def read_new(path, after):
    """after (通し番号) より新しい返事を、承認係に渡す形で返す。読めなければ None。"""
    rows = read_all(path)
    if rows is None:
        return None
    after = int(after or 0)
    return [{"id": str(r["n"]), "author_id": LOCAL, "channel_id": LOCAL, "content": r["text"], "ts": r["ts"]}
            for r in rows if r["n"] > after]


class Outbox:
    """知らせを画面に出し、outbox にも残す。"""

    def __init__(self, path, now_fn, echo=print):
        self.path, self.now, self.echo = Path(path), now_fn, echo
        self.last_error = None

    def post(self, text, title=None):
        body = ("%s\n%s" % (title, text)) if title else text
        self.echo(body)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                f.write("[%s]\n%s\n\n" % (self.now().isoformat(timespec="seconds"), body))
            return True
        except OSError as e:
            self.last_error = type(e).__name__
            return False
