"""承認係 (approval): 提案の作成と、人間の返事の照合・反映。

- LLM は使わない。決まった処理だけ。少しでも合わなければ何もしない (安全側)。
- ここには通信の処理はない。返事は {id, author_id, channel_id, content, ts} の辞書で受け取る (file_io.py が作る)。
- 反映先は設定の deploy_root (利用者が決めた練習用フォルダ。git 管理) だけ。
- STOP は STOP.flag を作って、承認の反映を止めるだけ。他のプログラムは止めない。

要点:
- 差分は決まった形 (git diff の出力そのまま、追加・変更・削除のみ) だけ受け付ける。名前の変更、バイナリ、
  権限の変更、リンク、a/ b/ のない形、記号や日本語を含む名前は拒否する。行数も数えて、余分な部分を隠せないようにする。
- 色を決めたファイルの一覧と、git が実際に変えるファイルの一覧が一致しなければ反映しない (反映前と反映後の2回確認)。
- 提案と状態の記録には封印 (鍵つきハッシュ) を付ける。鍵を読めない者が status や待機時刻を書き換えても封印が合わず、無効になる。
- 壊れた記録が1件あっても全体は止まらない。STOP は常に効く。
"""
import datetime as dt, fnmatch, hashlib, hmac, json, os, re, secrets, subprocess, time
from contextlib import contextmanager
from pathlib import Path

ORDER = {"green": 0, "yellow": 1, "red": 2}
JP = {"green": "緑", "yellow": "黄", "red": "赤"}
HEX4 = re.compile(r"^(?:(P-\d{8}-\d{2})\s+)?([0-9a-fA-F]{4})$")
CANCEL = re.compile(r"^CANCEL(?:\s+(P-\d{8}-\d{2}))?$", re.I)
PATH_OK = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*(/[A-Za-z0-9_][A-Za-z0-9._-]*)*$")
FIELD = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
INDEX = re.compile(r"^index [0-9a-f]{4,64}\.\.[0-9a-f]{4,64}( 100644)?$")
OPEN = ("pending", "approved_waiting")
STATUSES = OPEN + ("deployed", "cancelled", "rejected", "expired")
BLANK = {"", "なし", "無し", "null", "none", "n/a", "na", "-", "0", "false", "未定", "tbd"}
ALWAYS_REQUIRED = ("summary", "rollback")
ALWAYS_STRICT = ("summary", "rollback")
MAX_DIFF = 200_000
WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} | {"LPT%d" % i for i in range(1, 10)}
LOCAL = "local"     # ファイルモードの送り手・場所の固定の名前


class PacketError(Exception):
    pass


class ConfigError(Exception):
    pass


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


@contextmanager
def locked(path, timeout=60.0):
    """同時に2つの処理が記録を書かないための排他ロック (path + '.lock')。"""
    f = open(str(path) + ".lock", "a+")
    got = False
    try:
        end = time.time() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.time() > end:
                    raise PacketError("他の処理が使用中です。少し待ってやり直してください")
                time.sleep(0.05)
        yield
    finally:
        if got:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        f.close()


# ---------- 差分の読み取り (決まった形だけ受け付ける) ----------
def check_path(p):
    if not PATH_OK.match(p) or ".." in p.split("/") or p.lower().startswith(".git") or len(p) > 200 \
            or any(seg.endswith(".") or seg.split(".")[0].upper() in WIN_RESERVED for seg in p.split("/")):
        raise PacketError("受け付けられないファイル名: %r（英数字と . _ - / だけ）" % p)
    return p


def diff_files(diff_text):
    """差分が触るファイルの一覧。git diff の出力そのままの形 (追加・変更・削除) 以外は拒否する。"""
    if len(diff_text) > MAX_DIFF:
        raise PacketError("差分が大きすぎる")
    if "\r" in diff_text or "\x00" in diff_text:
        raise PacketError("差分に CR や NUL が含まれている")
    lines = diff_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    files, i, n = [], 0, len(lines)
    while i < n:
        m = re.match(r"^diff --git a/(\S+) b/(\S+)$", lines[i])
        if not m or m.group(1) != m.group(2):
            raise PacketError("差分の %d行目: 『diff --git a/名前 b/名前』（同じ名前）で始まっていない" % (i + 1))
        path = check_path(m.group(1))
        if path in files:
            raise PacketError("同じファイルが2回出てくる: %s" % path)
        i += 1
        kind, old, new = "modify", None, None
        while i < n and not lines[i].startswith("@@"):
            l = lines[i]
            if l == "new file mode 100644":
                kind = "add"
            elif l == "deleted file mode 100644":
                kind = "delete"
            elif INDEX.match(l):
                pass
            elif l.startswith("--- ") and old is None:
                old = l[4:]
            elif l.startswith("+++ ") and new is None and old is not None:
                new = l[4:]
            else:
                raise PacketError("差分の %d行目: 受け付けられない行（名前の変更・バイナリ・権限の変更などは不可）" % (i + 1))
            i += 1
        want = {"modify": ("a/" + path, "b/" + path), "add": ("/dev/null", "b/" + path), "delete": ("a/" + path, "/dev/null")}[kind]
        if (old, new) != want:
            raise PacketError("%s: ---/+++ の行が見出しと合わない" % path)
        hunks = 0
        while i < n and lines[i].startswith("@@"):
            h = HUNK.match(lines[i])
            if not h:
                raise PacketError("差分の %d行目: 変更箇所の見出しを読めない" % (i + 1))
            a = int(h.group(2)) if h.group(2) is not None else 1
            b = int(h.group(4)) if h.group(4) is not None else 1
            i += 1
            while a > 0 or b > 0:
                if i >= n:
                    raise PacketError("%s: 差分が途中で終わっている" % path)
                c = lines[i][:1]
                if c == " ":
                    a -= 1; b -= 1
                elif c == "-":
                    a -= 1
                elif c == "+":
                    b -= 1
                elif c != "\\":
                    raise PacketError("差分の %d行目: 変更箇所の中に読めない行" % (i + 1))
                if a < 0 or b < 0:
                    raise PacketError("差分の %d行目: 行数が見出しと合わない" % (i + 1))
                i += 1
            while i < n and lines[i].startswith("\\"):
                i += 1
            hunks += 1
        if hunks == 0:
            raise PacketError("%s: 変更箇所がない（空のファイルの追加などは不可）" % path)
        files.append(path)
    if not files:
        raise PacketError("差分から変更ファイルを読み取れない")
    return files


def check_levels(levels):
    """levels.json の形を確かめる。合わなければ ConfigError。"""
    if not isinstance(levels, dict) or not isinstance(levels.get("rules"), list):
        raise ConfigError("levels.json の形が不正")
    for r in levels["rules"]:
        if not isinstance(r, dict) or not isinstance(r.get("pattern"), str) or r.get("level") not in ORDER:
            raise ConfigError("levels.json の決まりが不正: %r" % (r,))
    if not isinstance(levels.get("must_be_red"), list) or not levels["must_be_red"]:
        raise ConfigError("levels.json に must_be_red（赤であるべき場所の例）がない")
    for ex in levels["must_be_red"]:
        if level_of(ex, levels) != "red":
            raise ConfigError("levels.json: %s が赤になっていない（赤であるべき場所が外れている）" % ex)
    rf = levels.get("required_fields")
    if not isinstance(rf, dict) or set(rf) != set(ORDER):
        raise ConfigError("levels.json の required_fields は green / yellow / red の3つ")
    for lv, names in rf.items():
        if not isinstance(names, list) or any(not isinstance(x, str) or not FIELD.match(x) for x in names) \
                or len(set(names)) != len(names) or any(x not in names for x in ALWAYS_REQUIRED):
            raise ConfigError("levels.json の required_fields.%s が不正（英小文字の項目名の一覧。summary と rollback は必須）" % lv)
    for k in ("strict_fields",):
        v = levels.get(k, [])
        if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
            raise ConfigError("levels.json の %s は項目名の一覧" % k)
    if not isinstance(levels.get("field_labels", {}), dict):
        raise ConfigError("levels.json の field_labels は {項目名: 見出し} の形")


def level_of(path, levels):
    """1ファイルの色。大文字小文字は区別しない。一覧にないものは黄。複数に当たれば高い方。"""
    hit, p = None, path.lower()
    for rule in levels.get("rules", []):
        if fnmatch.fnmatchcase(p, rule["pattern"].lower()):
            if hit is None or ORDER[rule["level"]] > ORDER[hit]:
                hit = rule["level"]
    return hit or "yellow"


def classify(files, levels):
    """差分全体の色 (いちばん高いもの)。"""
    return max((level_of(f, levels) for f in files), key=lambda x: ORDER[x])


def filled(v):
    return isinstance(v, str) and len(re.sub(r"\s", "", v)) >= 2 and v.strip().lower() not in BLANK


def load_seal_key(path):
    """封印用の鍵ファイル (setup が作る16進64文字) を読む。無い・壊れていれば ConfigError。"""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise ConfigError("封印用の鍵ファイルがない（%s）。初回は `python -m ee_aidea.approval_cli setup` を実行" % p.name)
    except (OSError, UnicodeDecodeError):
        raise ConfigError("封印用の鍵ファイルを読めない（%s）" % p.name)
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise ConfigError("封印用の鍵ファイルが壊れている（%s）。承認係は起動しません" % p.name)
    return bytes.fromhex(text)


def make_seal_key(path):
    """鍵ファイルを新しく作る。既にあれば何もしない (上書きすると今までの記録がすべて無効になるため)。"""
    p = Path(path)
    if p.exists():
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "x", encoding="utf-8", newline="\n") as f:
        f.write(secrets.token_bytes(32).hex() + "\n")
    return True


class Approval:
    def __init__(self, data_dir, cfg, levels, seal_key, now_fn=None, ledger_path=None, levels_sha=None):
        if not isinstance(seal_key, (bytes, bytearray)) or len(seal_key) < 16:
            raise ConfigError("封印用の鍵がない")
        check_levels(levels)
        self._key = bytes(seal_key)
        self.data = Path(data_dir)
        self.cfg, self.levels = cfg, levels
        self.owner_id = str(cfg.get("owner_id", LOCAL))
        self.channel_id = str(cfg.get("channel_id", LOCAL))
        self.tz = dt.timezone(dt.timedelta(hours=cfg.get("utc_offset_hours", 9)))
        self.now = now_fn or (lambda: dt.datetime.now(self.tz))
        self.ledger_path = ledger_path
        self.unit = cfg.get("unit", "pt")
        self.root = Path(cfg["deploy_root"]).resolve()
        (self.data / "proposals").mkdir(parents=True, exist_ok=True)
        self.state_path = self.data / "state.json"
        self.notices, self._bad_reported = [], set()
        self.state = self._load_state()
        self.max_rev = int(self.state.get("rev", 0))
        if levels_sha is not None:
            if self.state.get("levels_sha") is None:
                self.state["levels_sha"] = levels_sha
                self.save_state()
            elif self.state["levels_sha"] != levels_sha:
                raise ConfigError("levels.json が、前回確認したときから変わっています。"
                                  "内容を確認し、問題なければ `python -m ee_aidea.approval_cli pin-levels` を実行してください")

    def __repr__(self):
        return "<Approval data=%s>" % self.data

    # ---------- 封印 ----------
    def _mac(self, obj):
        body = {k: v for k, v in obj.items() if k != "seal"}
        raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hmac.new(self._key, raw, hashlib.sha256).hexdigest()

    def _sealed_ok(self, obj):
        return isinstance(obj, dict) and isinstance(obj.get("seal"), str) and hmac.compare_digest(obj["seal"], self._mac(obj))

    def _write(self, path, obj):
        obj["seal"] = self._mac(obj)
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    # ---------- 状態 ----------
    def _fresh_state(self):
        return {"processed": [], "last_message_id": None, "stop": None, "levels_sha": None, "closed": [],
                "rev": 0, "revs": {}}

    def _load_state(self):
        if not self.state_path.exists():
            st = self._fresh_state()
            has_props = any((self.data / "proposals").glob("P-*"))
            if self.stop_flag.exists() or has_props:
                # 旗だけ残っている、または提案があるのに状態の記録がない（消された疑い）→ 止めた状態で始める
                st["stop"] = {"at": self.now().isoformat(timespec="seconds"), "resume_hash": None}
                if has_props:
                    self.stop_flag.write_text("state missing\n", encoding="utf-8")
                    self.audit("state_missing")
                    self.notices.append("提案の記録があるのに、状態の記録（state.json）がありません。安全のため承認の反映を止めました。"
                                        "`STOP` と返事すると再開用の4文字を出します。")
            return st
        try:
            st = json.loads(self.state_path.read_text(encoding="utf-8"))
            ok = self._sealed_ok(st) and isinstance(st.get("processed"), list) \
                and (st.get("stop") is None or isinstance(st.get("stop"), dict))
            if ok and not isinstance(st.get("closed"), list):
                st["closed"] = []
            if ok and (not isinstance(st.get("rev"), int) or isinstance(st.get("rev"), bool) or not isinstance(st.get("revs"), dict)):
                st["rev"], st["revs"] = (st.get("rev") if isinstance(st.get("rev"), int) else 0), {}
        except Exception:
            ok = False
        if ok:
            return st
        st = self._fresh_state()
        st["stop"] = {"at": self.now().isoformat(timespec="seconds"), "resume_hash": None}
        self.stop_flag.write_text("state invalid\n", encoding="utf-8")
        self.audit("state_invalid")
        self.notices.append("状態の記録（state.json）が壊れているか、書き換えられています。安全のため承認の反映を止めました。"
                            "`STOP` と返事すると再開用の4文字を出します。")
        return st

    def save_state(self):
        self._write(self.state_path, self.state)
        self.max_rev = max(getattr(self, "max_rev", 0), self.state.get("rev", 0))

    # ---------- 巻き戻しの検知 (通し番号) ----------
    def bump(self):
        self.state["rev"] = int(self.state.get("rev", 0)) + 1
        return self.state["rev"]

    def on_rollback(self, known_rev, how):
        """記録が古い時点に戻されたと分かったとき: 受付中の提案をすべて無効にし、止める。"""
        dead = []
        for d in sorted((self.data / "proposals").glob("P-*")):
            try:
                p = json.loads((d / "packet.json").read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(p, dict) and p.get("status") in OPEN and d.name not in self.state["closed"]:
                dead.append(d.name)
        self.state["closed"] = (self.state["closed"] + dead)[-2000:]
        self.state["revs"] = {}
        self.state["rev"] = max(int(self.state.get("rev", 0)), int(known_rev)) + 1
        self.state["stop"] = {"at": self.now().isoformat(timespec="seconds"), "resume_hash": None}
        self.stop_flag.write_text("rollback\n", encoding="utf-8")
        self.save_state()
        self.audit("rollback_detected", how=how, known_rev=known_rev, closed=dead)
        self.notices.append("**記録が古い時点に戻されています（巻き戻しを検知）。** 安全のため承認の反映を止め、受付中だった提案%sを無効にしました。"
                            "心当たりがなければ、PCの中のファイルが書き換えられた可能性があります。`STOP` と返事すると再開用の4文字を出します。"
                            % (("（%s）" % "、".join(dead)) if dead else ""))

    def reload(self):
        """常駐中に毎回呼ぶ: 別の処理（new / send）が書いた状態を読み直す。番号が逆戻りしていたら巻き戻し。
        限界: 常駐していない間に data/ をまるごと古い写しに戻されると、この方法では気づけない。"""
        seen = getattr(self, "max_rev", 0)
        mem = self.state
        self.state = self._load_state()
        if not self.state_path.exists() and (mem.get("rev", 0) > 0 or mem.get("processed")):
            self.state = mem                      # 状態のファイルが消された → 手元の状態を正として、巻き戻し扱い
            self.on_rollback(seen, "state_deleted")
        elif int(self.state.get("rev", 0)) < seen:
            self.state["processed"] = mem.get("processed", [])
            self.state["last_message_id"] = mem.get("last_message_id")
            self.on_rollback(seen, "rev_went_back")
        else:
            self.max_rev = max(seen, int(self.state.get("rev", 0)))

    def audit(self, event, **kw):
        row = {"ts": self.now().isoformat(timespec="seconds"), "event": event}
        row.update(kw)
        with open(self.data / "audit.log", "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    @property
    def stop_flag(self):
        return self.data / "STOP.flag"

    def stopped(self):
        return self.state.get("stop") is not None or self.stop_flag.exists()

    # ---------- 提案の記録 ----------
    def pdir(self, pid):
        return self.data / "proposals" / pid

    def load(self, pid):
        """封印と形を確かめて読む。合わなければ PacketError。"""
        try:
            p = json.loads((self.pdir(pid) / "packet.json").read_text(encoding="utf-8"))
        except Exception:
            raise PacketError("%s の記録を読めない" % pid)
        if not self._sealed_ok(p) or p.get("id") != pid or p.get("status") not in STATUSES or p.get("level") not in ORDER:
            raise PacketError("%s の記録が壊れているか、書き換えられている" % pid)
        if p["status"] in OPEN and pid in self.state.get("closed", []):
            raise PacketError("%s は終了済みなのに、受付中の古い記録に戻されている" % pid)
        if p["status"] in OPEN and self.state.get("revs", {}).get(pid) != p.get("rev"):
            raise PacketError("%s の記録が、状態の記録と合わない（古い版に戻された疑い）" % pid)
        return p

    def store(self, p):
        p["rev"] = self.bump()                    # 通し番号を進め、提案と状態の両方に同じ番号を書く
        self._write(self.pdir(p["id"]) / "packet.json", p)
        if p["status"] in OPEN:
            self.state["revs"][p["id"]] = p["rev"]
        else:
            # 終わった提案の番号を状態に残す（古い packet.json を戻して生き返らせるのを防ぐ）
            self.state["revs"].pop(p["id"], None)
            if p["id"] not in self.state["closed"]:
                self.state["closed"] = (self.state["closed"] + [p["id"]])[-2000:]
        self.save_state()

    def all(self):
        """有効な提案だけを返す。壊れた・書き換えられた記録は無効として飛ばし、1回だけ知らせる。"""
        out = []
        for d in sorted((self.data / "proposals").glob("P-*")):
            if not d.is_dir():
                continue
            try:
                out.append(self.load(d.name))
            except PacketError:
                if d.name not in self._bad_reported:
                    self._bad_reported.add(d.name)
                    self.audit("packet_invalid", id=d.name)
                    self.notices.append("%s は記録が壊れているか書き換えられているため、無効として扱います（反映しません）。" % d.name)
        return out

    # ---------- 提案の作成 ----------
    def numstat_files(self, diff_bytes):
        """git が実際に触ると言うファイルの一覧。取れなければ None。"""
        r = self.git("apply", "--numstat", "-z", "-p1", "-", input_bytes=diff_bytes)
        if r.returncode:
            return None
        out = []
        for ent in r.stdout.split(b"\0"):
            if not ent:
                continue
            parts = ent.split(b"\t")
            if len(parts) != 3:
                return None
            out.append(parts[2].decode("utf-8", "replace"))
        return sorted(out)

    def required(self, level):
        return list(self.levels["required_fields"][level])

    def new_proposal(self, diff_bytes, fields):
        try:
            text = diff_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise PacketError("差分が UTF-8 でない")
        if not isinstance(fields, dict):
            raise PacketError("項目のファイルは {項目名: 文} の形")
        files = diff_files(text)
        self.require_repo()
        if self.numstat_files(diff_bytes) != sorted(files):
            raise PacketError("差分から読んだファイルの一覧が、git の読み方と一致しない。受け付けません")
        if self.ignored(files):
            raise PacketError("git の管理から外されている場所（.gitignore）を含む差分は受け付けません")
        level = classify(files, self.levels)
        need = self.required(level)
        strict = set(ALWAYS_STRICT) | set(self.levels.get("strict_fields", []))     # 「なし」等も不可
        missing = [k for k in need if not (filled(fields.get(k)) if k in strict
                                           else isinstance(fields.get(k), str) and fields[k].strip())]
        if missing:
            raise PacketError("%sの提案に必要な項目がない、または中身がない: %s" % (JP[level], "、".join(missing)))
        now = self.now()
        day = now.strftime("%Y%m%d")
        n = 1 + sum(1 for d in (self.data / "proposals").glob("P-%s-*" % day))
        if n > 99:
            raise PacketError("1日の提案数の上限 (99)")
        pid = "P-%s-%02d" % (day, n)
        h = sha256_bytes(diff_bytes)
        for p in self.all():
            if p["status"] in OPEN and p["diff_sha256"][:4] == h[:4]:
                raise PacketError("先頭4文字が、受付中の提案 %s と同じ。差分を少し変えて作り直す" % p["id"])
        self.pdir(pid).mkdir(parents=True)
        (self.pdir(pid) / "change.diff").write_bytes(diff_bytes)
        p = {"id": pid, "level": level, "files": files,
             "summary": fields["summary"].strip(), "rollback": fields["rollback"].strip(),
             "fields": {k: fields[k].strip() for k in need if k not in ALWAYS_REQUIRED},
             "spent": self.spent(pid), "diff_sha256": h, "wait_until": None,
             "created_at": now.isoformat(timespec="seconds"),
             "expires_at": (now + dt.timedelta(days=self.cfg.get("expire_days", 7))).isoformat(timespec="seconds"),
             "status": "pending", "sent": False}
        self.store(p)
        self.audit("created", id=pid, level=level, files=files)
        return p

    def spent(self, pid):
        """帳簿から、この提案IDを ref に持つ spend の合計 (引いた分と参考換算)。帳簿が読めなければ None。"""
        try:
            rows = [json.loads(l) for l in Path(self.ledger_path).read_text(encoding="utf-8").split("\n") if l.strip()]
            sp = [r for r in rows if r.get("type") == "spend" and r.get("ref") == pid]
            return {"burned": round(-sum(float(r["amount"]) for r in sp), 2),
                    "equiv": round(sum(float(r.get("equiv") or 0) for r in sp), 2)}
        except Exception:
            return None

    def render(self, p):
        """人間に見せる文面 (題名, 本文)。項目は levels.json の required_fields の順に出す。"""
        labels = self.levels.get("field_labels", {})
        L = ["■ 何を変えるか\n%s\n変更ファイル: %s" % (p["summary"], "、".join(p["files"]))]
        for k, v in p.get("fields", {}).items():
            L.append("■ %s\n%s" % (labels.get(k, k), v))
        L.append("■ %s\n%s" % (labels.get("rollback", "戻せるか・戻し方"), p["rollback"]))
        y = p.get("spent")
        L.append("■ 消費 (%s)\n%s" % (self.unit, "引いた分 %.2f（参考換算 %.2f）" % (y["burned"], y["equiv"]) if y else "帳簿を読めず不明"))
        h = p["diff_sha256"]
        L.append("■ ハッシュ\n**%s**%s" % (h[:4], h[4:]))
        exp = p["expires_at"][:16].replace("T", " ")
        if p["level"] == "green":
            L.append("通知のみ（緑）。自動で反映します。")
        elif p["level"] == "yellow":
            L.append("承認＝先頭4文字 `%s` を返事 ／ 破棄＝CANCEL ／ 全停止＝STOP（期限 %s）" % (h[:4], exp))
        else:
            L.append("承認＝先頭4文字 `%s` を返事 ＋ %s時間の待機（待機中は CANCEL で破棄）／ 全停止＝STOP（期限 %s。待機が期限内に終わる必要あり）" % (
                h[:4], self.cfg.get("wait_hours_red", 24), exp))
        title = "[%s] %s %s" % (JP[p["level"]], p["id"], p["summary"].splitlines()[0][:60])
        return title, "\n\n".join(L)[:3900]

    def mark_sent(self, pid):
        p = self.load(pid)
        p["sent"] = True
        self.store(p)
        self.audit("sent", id=pid)

    # ---------- 反映 ----------
    def git(self, *args, input_bytes=None):
        return subprocess.run(["git", "-c", "user.name=ee-aidea", "-c", "user.email=ee-aidea@localhost",
                               "-c", "core.autocrlf=false", "-c", "core.quotepath=true"] + list(args),
                              cwd=str(self.root), input=input_bytes, capture_output=True, timeout=60)

    def require_repo(self):
        """反映先そのものが git の管理フォルダであること (親フォルダの git を使わない)。"""
        if not (self.root / ".git").exists():
            raise PacketError("反映先が git の管理下にない（%s に .git がない）" % self.root.name)
        r = self.git("rev-parse", "--show-toplevel")
        if r.returncode or Path(r.stdout.decode("utf-8", "replace").strip()).resolve() != self.root:
            raise PacketError("反映先の git の場所が想定と違う")

    def ignored(self, files):
        """.gitignore で管理から外れている場所が含まれるか（含まれると、失敗時に元へ戻せない）。"""
        r = self.git("check-ignore", "--no-index", "--", *files)
        return r.returncode == 0

    def changed_paths(self):
        r = self.git("status", "--porcelain", "-z", "--untracked-files=all")
        if r.returncode:
            return None
        out = []
        for ent in r.stdout.split(b"\0"):
            if ent:
                out.append(ent[3:].decode("utf-8", "replace"))
        return sorted(out)

    def deploy(self, p):
        """検査してから反映する。(成功か, 返事)。失敗したら元に戻す。"""
        diff = (self.pdir(p["id"]) / "change.diff").read_bytes()
        try:
            self.require_repo()
        except PacketError as e:
            return False, "%s。何もしていません" % e
        r = self.git("rev-parse", "HEAD")
        if r.returncode:
            return False, "反映先に履歴がない。何もしていません"
        prev = r.stdout.decode().strip()
        if self.changed_paths() != []:
            return False, "反映先に未記録の変更がある。何もしていません"
        if self.numstat_files(diff) != sorted(p["files"]):
            return False, "差分が触るファイルが、承認されたファイルの一覧と一致しない。何もしていません"
        if self.ignored(p["files"]):
            return False, "git の管理から外されている場所を含む。何もしていません"
        if self.git("apply", "--check", "-p1", "-", input_bytes=diff).returncode:
            return False, "差分が今のファイルに当てはまらない（git apply --check 失敗）。何もしていません"

        def undo():
            self.git("reset", "--hard", prev); self.git("clean", "-fd")
        if self.git("apply", "-p1", "-", input_bytes=diff).returncode:
            undo()
            return False, "反映に失敗。元に戻しました"
        if self.changed_paths() != sorted(p["files"]):
            undo()
            return False, "実際に変わったファイルが、承認されたファイルの一覧と一致しない。元に戻しました"
        why = self.post_check(p["files"])
        if why:
            undo()
            return False, "反映後チェックに失敗（%s）。元に戻しました" % why
        self.git("add", "-A")
        if self.git("commit", "-m", "%s %s" % (p["id"], p["diff_sha256"])).returncode:
            undo()
            return False, "記録（コミット）に失敗。元に戻しました"
        p["prev_commit"] = prev
        return True, "反映済み。戻す場合は前版 %s" % prev[:8]

    def post_check(self, files):
        for f in files:
            fp = self.root / f
            if fp.is_symlink():
                return "%s がリンクになっている" % f
            if not fp.exists():
                continue
            try:
                if f.endswith(".json"):
                    json.loads(fp.read_text(encoding="utf-8"))
                elif f.endswith(".py"):
                    compile(fp.read_text(encoding="utf-8"), f, "exec")
            except Exception as e:
                return "%s が読めない: %s" % (f, type(e).__name__)
        return None

    def recheck(self, p):
        """承認・反映の直前に、差分と色を計算し直す。問題があれば理由。"""
        try:
            diff = (self.pdir(p["id"]) / "change.diff").read_bytes()
        except OSError:
            return "差分のファイルがありません"
        if sha256_bytes(diff) != p["diff_sha256"]:
            return "差分が変更されています"
        try:
            files = diff_files(diff.decode("utf-8"))
        except (PacketError, UnicodeDecodeError):
            return "差分を読めません"
        if files != p["files"]:
            return "差分のファイルの一覧が作成時と違います"
        level = classify(files, self.levels)
        if level != p["level"]:
            return "色の判定が作成時と違います（作成時 %s、今 %s）" % (JP[p["level"]], JP[level])
        return None

    def finish(self, p, ok, msg, how):
        p["status"] = "deployed" if ok else "rejected"
        p["result"] = msg
        self.store(p)
        self.audit("deployed" if ok else "rejected", id=p["id"], how=how, detail=msg)
        return "%s: %s" % (p["id"], msg)

    def when(self, s):
        t = dt.datetime.fromisoformat(s)
        if t.tzinfo is None:
            raise PacketError("時刻にタイムゾーンがない")
        return t

    # ---------- 返事の処理 ----------
    def handle(self, msg):
        """返事を1件処理し、こちらからの知らせの一覧を返す。msg = {id, author_id, channel_id, content, ts}"""
        mid = str(msg.get("id"))
        if mid in self.state["processed"]:
            return []
        self.state["processed"] = (self.state["processed"] + [mid])[-500:]
        try:
            return self._handle(msg)
        except Exception as e:      # 何が起きても落とさない。反映はしていない前提で、やり直しを頼む
            self.audit("handle_error", message=mid, kind=type(e).__name__)
            if str(msg.get("author_id")) == self.owner_id and str(msg.get("channel_id")) == self.channel_id:
                return ["処理中にエラー（%s）が起きました。何もしていない可能性が高いので、状況を確認してからもう一度返事してください。" % type(e).__name__]
            return []
        finally:
            self.save_state()

    def _sent_at(self, msg):
        try:
            return self.when(str(msg.get("ts")))
        except Exception:
            return None

    def _handle(self, msg):
        if str(msg.get("channel_id")) != self.channel_id:
            self.audit("ignored_other_channel", message=str(msg.get("id")))
            return []
        if str(msg.get("author_id")) != self.owner_id:
            self.audit("ignored_other_id", message=str(msg.get("id")), author=str(msg.get("author_id"))[:30])
            return []
        text = str(msg.get("content") or "").strip()
        if text.upper() == "STOP":
            return [self.do_stop()]          # STOP は古くても受け付ける（止める側は常に通す）
        m = CANCEL.match(text)
        if m:
            return [self.do_cancel(m.group(1))]   # CANCEL も古くても受け付ける（止める・取り消す側は常に通す）
        max_age = self.cfg.get("max_message_age_minutes")
        if max_age and HEX4.match(text):
            sent = self._sent_at(msg)
            if sent is None or abs((self.now() - sent).total_seconds()) > max_age * 60:
                self.audit("ignored_stale", message=str(msg.get("id")))
                return ["%s分より前の承認・再開の返事は処理しません（承認係が止まっていた間の返事や、古い返事の再処理を防ぐため）。必要なら、もう一度返事してください。" % max_age]
        m = HEX4.match(text)
        if m:
            return [self.do_code(m.group(1), m.group(2).lower())]
        self.audit("ignored_text", message=str(msg.get("id")))
        return []

    def do_stop(self):
        stop = self.state.get("stop") or {}
        if self.stopped() and stop.get("resume_hash"):
            if not self.stop_flag.exists():
                self.stop_flag.write_text(str(stop.get("at")) + "\n", encoding="utf-8")
            return "すでに停止中です。再開するには、再開用の4文字 `%s` を返事してください（STOP では再開しません）。" % stop["resume_hash"][:4]
        try:
            taken = {p["diff_sha256"][:4] for p in self.all() if p["status"] in OPEN}
        except Exception:
            taken = set()
        while True:
            h = sha256_bytes(secrets.token_bytes(32))
            if h[:4] not in taken:
                break
        self.state["stop"] = {"at": self.now().isoformat(timespec="seconds"), "resume_hash": h}
        self.bump()
        self.stop_flag.write_text(self.state["stop"]["at"] + "\n", encoding="utf-8")
        self.audit("stop")
        return ("STOP を受け付けました。停止の旗（STOP.flag）を作り、承認の反映を止めました。"
                "（止まるのは承認の反映だけです。ほかのプログラムは止まりません。）"
                "再開するには、再開用の4文字 `%s` を返事してください。" % h[:4])

    def do_cancel(self, pid):
        cands = [p for p in self.all() if p["status"] in OPEN and p["level"] != "green" and (pid is None or p["id"] == pid)]
        if not cands:
            return "CANCEL: 対象の提案がありません。"
        p = cands[-1]
        p["status"] = "cancelled"
        self.store(p)
        self.audit("cancelled", id=p["id"])
        return "%s を破棄しました。" % p["id"]

    def do_code(self, pid, code):
        stop = self.state.get("stop") or {}
        if self.stopped():
            if pid is None and stop.get("resume_hash") and hmac.compare_digest(stop["resume_hash"][:4], code):
                return self.do_resume()
            self.audit("code_while_stopped")
            return "停止中のため、承認は受け付けません。先に再開用の4文字で再開してください（分からなければ `STOP` と返事すると表示します）。"
        cands = [p for p in self.all() if p["status"] == "pending" and p["level"] in ("yellow", "red")
                 and p.get("sent") is True and p["diff_sha256"][:4] == code and (pid is None or p["id"] == pid)]
        if not cands:
            self.audit("code_no_match", code=code)
            return "該当なし（`%s` に合う受付中の提案がありません）。何もしていません。" % code
        if len(cands) > 1:
            self.audit("code_ambiguous", code=code)
            return "候補が複数あります。提案IDを付けて返事し直してください（例: `%s %s`）。何もしていません。" % (cands[0]["id"], code)
        p = cands[0]
        now = self.now()
        if now > self.when(p["expires_at"]):
            p["status"] = "expired"; self.store(p); self.audit("expired", id=p["id"])
            return "%s は期限切れです。何もしていません。" % p["id"]
        why = self.recheck(p)
        if why:
            return self.finish(p, False, why + "。提案を無効にしました", "recheck")
        if p["level"] == "red":
            until = now + dt.timedelta(hours=self.cfg.get("wait_hours_red", 24))
            if until > self.when(p["expires_at"]):
                self.audit("red_too_late", id=p["id"])
                return ("%s: 承認できません。%s時間の待機が期限（%s）までに終わらないためです。提案を作り直してください。何もしていません。"
                        % (p["id"], self.cfg.get("wait_hours_red", 24), p["expires_at"][:16].replace("T", " ")))
            p["status"], p["wait_until"] = "approved_waiting", until.isoformat(timespec="seconds")
            self.store(p)
            self.audit("approved_waiting", id=p["id"], wait_until=p["wait_until"])
            return "%s: 承認を受け付けました。待機中です（終了 %s）。待機中は CANCEL で破棄できます。" % (
                p["id"], until.strftime("%m/%d %H:%M"))
        self.audit("approved", id=p["id"])
        ok, msg = self.deploy(p)
        return self.finish(p, ok, "照合OK（ハッシュ全桁一致）。" + msg if ok else msg, "approval")

    def do_resume(self):
        back = []
        for p in self.all():
            if p["status"] == "approved_waiting":     # 停止をまたいだ赤は、承認からやり直す
                p["status"], p["wait_until"] = "pending", None
                self.store(p)
                back.append(p["id"])
        if self.stop_flag.exists():
            self.stop_flag.unlink()
        self.state["stop"] = None
        self.bump()
        self.audit("resumed", back_to_pending=back)
        extra = ("待機中だった %s は、承認前の状態に戻しました。必要なら、もう一度4文字を返事してください（待機は最初から）。" % "、".join(back)) if back else ""
        return "再開しました（STOP.flag を外しました）。" + extra

    # ---------- 時間で動くもの ----------
    def tick(self):
        """期限切れ、赤の待機終了、緑の自動反映。知らせる文の一覧を返す。"""
        out, now = [], self.now()
        if self.state.get("stop") and not self.stop_flag.exists():
            self.stop_flag.write_text(str(self.state["stop"].get("at")) + "\n", encoding="utf-8")
        for p in self.all():
            try:
                if p["status"] == "pending" and now > self.when(p["expires_at"]):
                    p["status"] = "expired"; self.store(p); self.audit("expired", id=p["id"])
                    out.append("%s は期限切れになりました。" % p["id"])
                    continue
                if self.stopped():
                    continue
                if p["status"] == "approved_waiting":
                    if p["level"] != "red" or not p.get("wait_until"):
                        out.append(self.finish(p, False, "待機の記録が不正。提案を無効にしました", "invalid"))
                        continue
                    if now < self.when(p["wait_until"]):
                        continue
                    if now > self.when(p["expires_at"]):
                        p["status"] = "expired"; self.store(p); self.audit("expired", id=p["id"])
                        out.append("%s は待機中に期限切れになりました。反映していません。" % p["id"])
                        continue
                    why = self.recheck(p)
                    if why:
                        out.append(self.finish(p, False, why + "。提案を無効にしました", "recheck"))
                        continue
                    ok, msg = self.deploy(p)
                    out.append(self.finish(p, ok, "待機が終わりました。" + msg if ok else msg, "wait_over"))
                elif p["status"] == "pending" and p["level"] == "green" and p.get("sent") is True:
                    why = self.recheck(p)
                    if why:
                        out.append(self.finish(p, False, why + "。提案を無効にしました", "recheck"))
                        continue
                    ok, msg = self.deploy(p)
                    out.append(self.finish(p, ok, "（緑・自動）" + msg if ok else msg, "green_auto"))
            except Exception as e:
                self.audit("tick_error", id=p.get("id"), kind=type(e).__name__)
        notes, self.notices = self.notices, []
        return notes + out
