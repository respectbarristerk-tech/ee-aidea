"""帳簿 (ledger)。追記のみの JSON Lines。標準ライブラリだけで動き、ネットには出ない。

- 1行1取引。書き換え・削除はしない。誤りは adjust で打ち消す。
- 各行は直前の行の SHA-256 (prev_hash) を持つ。最後の行のハッシュは <帳簿>.head にも残す。
- 追記はファイルロック (<帳簿>.lock) の中で「検査→計算→書き込み→head更新」を一度に行う。
- 1回の操作で書く行は、全部書くか、1行も書かないか (grant-month も同じ)。
- 金額は 1/100 単位の整数で計算する。小数2桁より細かい値・数値でない値は拒否する (丸めない)。
- 単位 (既定 pt) は内部の目安。実際のお金は動かない。現実のお金や外部のポイントと交換しない。

使い方 (ee-aidea.json のあるフォルダで):
  python -m ee_aidea.ledger verify                     帳簿の検査 (異常なら終了コード1)
  python -m ee_aidea.ledger summary [--month 2030-01]  持ち分とその月の実績 (異常なら数字を出さず終了コード1)
  python -m ee_aidea.ledger seal                       head 記録が無い帳簿に、検査OKなら head を作る (初回のみ)
  python -m ee_aidea.ledger grant-month 2030-01        月初の発行 (budgets.json に従う。同じ月は1回だけ)
  python -m ee_aidea.ledger spend --dept qa --model example-large --tokens-in 41000 --tokens-out 3200 --ref R-1 --note 再確認
  python -m ee_aidea.ledger transfer --from research --to engineering --amount 50 --ref R-2 --note 依頼
"""
import argparse, datetime as dt, hashlib, json, math, os, re, sys, time
from contextlib import contextmanager
from pathlib import Path

from . import config as CF

TREASURY = "treasury"
TYPES = ("grant", "spend", "adjust", "transfer")
ZERO = "0" * 64
MAX_CENTS = 10 ** 14  # これを超える金額は入力ミスとみなす


class LedgerError(Exception):
    pass


# ---------- 金額 ----------
def cents(x):
    """金額を 1/100 単位の整数に。数値以外・非有限・大きすぎ・小数2桁より細かい値は拒否 (丸めない)。"""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise LedgerError("金額が数値でない: %r" % (x,))
    if not math.isfinite(x):
        raise LedgerError("金額が有限の数でない: %r" % (x,))
    if abs(x) > MAX_CENTS / 100:
        raise LedgerError("金額が大きすぎる: %r" % (x,))
    c = round(x * 100)
    if abs(x * 100 - c) > 1e-6:
        raise LedgerError("金額は小数2桁まで: %r" % (x,))
    return int(c)


def dumps(row):
    return json.dumps(row, ensure_ascii=False, separators=(",", ":"))


def sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def head_path(path):
    return Path(str(path) + ".head")


# ---------- ロック ----------
@contextmanager
def locked(path, timeout=30.0):
    """<path>.lock を排他ロックする。ロック用ファイルは消さない (中身は使わない)。"""
    try:
        f = open(str(path) + ".lock", "a+")
    except OSError as e:
        raise LedgerError("帳簿のフォルダを使えない (%s)" % type(e).__name__)
    got = False
    try:
        end = time.time() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.time() > end:
                    raise LedgerError("帳簿が他の処理で使用中 (%d秒待っても空かない)" % timeout)
                time.sleep(0.05)
        yield
    finally:
        if got:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        f.close()


# ---------- 読み取り ----------
def read(path):
    """(行の一覧, 元の文字列の一覧, 末尾が改行で終わるか)。ファイルが無ければ空。"""
    p = Path(path)
    if not p.exists():
        return [], [], True
    data = p.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise LedgerError("帳簿異常: UTF-8 として読めない")
    if "\r" in text:
        raise LedgerError("帳簿異常: 改行が CRLF に変わっている (エディタや git の自動変換の疑い。中身の書き換えとは限らない)")
    raw = [l for l in text.split("\n") if l.strip()]
    rows = []
    for i, l in enumerate(raw, 1):
        try:
            r = json.loads(l)
        except ValueError:
            raise LedgerError("帳簿異常: %d行目がJSONとして読めない" % i)
        if not isinstance(r, dict):
            raise LedgerError("帳簿異常: %d行目が取引の形 (オブジェクト) でない" % i)
        rows.append(r)
    return rows, raw, (not data) or data.endswith(b"\n")


def _dept_ok(d):
    return d == TREASURY or (isinstance(d, str) and bool(CF.NAME.match(d)))


# ---------- 1行の形の検査 (append と verify で共通) ----------
def check_row(r, protected=()):
    """1行の中身が決まりに合うか。合わなければ理由の文字列、合えば None。
    課の名前は形だけを見る (設定から外した課の過去の行も読めるように)。設定にあるかは追記のときに見る。"""
    seq = r.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        return "seq が整数でない"
    t, d = r.get("type"), r.get("dept")
    if t not in TYPES:
        return "type が不正 (%r)" % (t,)
    if not _dept_ok(d):
        return "dept が不正 (%r)" % (d,)
    if not isinstance(r.get("ts"), str):
        return "ts がない"
    try:
        a = cents(r.get("amount"))
    except LedgerError as e:
        return str(e)
    for k in ("tokens_in", "tokens_out"):
        if k in r and (isinstance(r[k], bool) or not isinstance(r[k], int) or r[k] < 0):
            return "%s が0以上の整数でない" % k
    for k in ("equiv", "shortfall", "real_cost"):
        if k in r and r[k] is not None:
            try:
                if cents(r[k]) < 0:
                    return "%s が負" % k
            except LedgerError:
                return "%s が不正" % k
    if not isinstance(r.get("ref", ""), str) or not str(r.get("ref", "")).strip():
        return "ref がない"
    if t == "grant" and a <= 0:
        return "grant は正の額"
    if t == "spend" and a > 0:
        return "spend は0か負の額"
    if t == "transfer":
        if not _dept_ok(r.get("to")) or r.get("to") == d:
            return "transfer の宛先が不正"
        if a <= 0:
            return "transfer は正の額"
        if d in protected:
            return "守られた課 (%s) の持ち分は他へ移せない" % d
    if t == "adjust" and d in protected and a < 0:
        return "守られた課 (%s) の持ち分を adjust で減らすことはできない" % d
    return None


def apply(hold, r):
    """1行を持ち分に反映する (hold は {課: 1/100 単位})。"""
    a = cents(r["amount"])
    if r["type"] == "transfer":
        hold[r["dept"]] = hold.get(r["dept"], 0) - a
        hold[r["to"]] = hold.get(r["to"], 0) + a
    else:  # grant(+), spend(-), adjust(+-)
        hold[r["dept"]] = hold.get(r["dept"], 0) + a


def negatives(hold):
    return [d for d, v in hold.items() if v < 0]


def _num(b, k, integer=False, lo=0, hi=None):
    v = b.get(k)
    if isinstance(v, bool) or not isinstance(v, int if integer else (int, float)) or not math.isfinite(v):
        raise LedgerError("budgets.json の %s が%sでない" % (k, "整数" if integer else "数値"))
    if v < lo or (hi is not None and v > hi):
        raise LedgerError("budgets.json の %s が範囲外 (%r)" % (k, v))
    return v


def equiv_cost(rates, model, tokens_in, tokens_out):
    """参考換算: 同じトークン数を API の従量課金で払ったら、いくらになるか (rates の通貨で)。記録用の目安。"""
    def num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
    if not isinstance(rates, dict) or not isinstance(rates.get("models"), dict) or not num(rates.get("fx_rate")) or rates["fx_rate"] <= 0:
        raise LedgerError("rates.json の形が不正 (models と正の fx_rate が必要)")
    m = rates["models"].get(model)
    if m is None:
        return None
    if not isinstance(m, dict) or not num(m.get("in_usd_per_mtok")) or not num(m.get("out_usd_per_mtok")):
        raise LedgerError("rates.json の %s の単価が不正" % model)
    usd = tokens_in / 1e6 * m["in_usd_per_mtok"] + tokens_out / 1e6 * m["out_usd_per_mtok"]
    return round(usd * rates["fx_rate"], 2)


class Ledger:
    """1冊の帳簿。departments は追記を許す課、protected は他へ移せない・adjust で減らせない課。"""

    def __init__(self, path, departments, protected=(), unit="pt", utc_offset_hours=9, currency_label="円"):
        try:
            self.departments, self.protected = CF.check_departments(list(departments), list(protected))
        except CF.ConfigError as e:
            raise LedgerError(str(e))
        self.path = str(path)
        self.unit = unit
        self.currency_label = currency_label
        self.tz = dt.timezone(dt.timedelta(hours=utc_offset_hours))

    @classmethod
    def from_config(cls, cfg, path=None):
        return cls(path or cfg["ledger"]["path"], cfg["departments"], cfg["protected_depts"], cfg["unit"],
                   cfg["utc_offset_hours"], cfg["currency_label"])

    def now(self):
        return dt.datetime.now(self.tz)

    def holdings(self, rows):
        hold = {d: 0 for d in self.departments + (TREASURY,)}
        for r in rows:
            apply(hold, r)
        return hold

    # ---------- 検査 ----------
    def verify(self):
        """問題の一覧を返す。空なら正常。追記の途中を読まないよう、ロックを取ってから読む。"""
        try:
            with locked(self.path):
                return self._verify()
        except LedgerError as e:
            return ["帳簿を確認できない: %s" % e]

    def _verify(self):
        """ロックを取った状態で呼ぶ。"""
        try:
            rows, raw, _ = read(self.path)
        except LedgerError as e:
            return [str(e)]
        probs, prev = [], ZERO
        hold = {d: 0 for d in self.departments + (TREASURY,)}
        budget_refs = set()
        for i, (r, l) in enumerate(zip(rows, raw), 1):
            if r.get("seq") != i or isinstance(r.get("seq"), bool):
                probs.append("帳簿異常: %d行目の seq が %r (連番でない)" % (i, r.get("seq")))
            if r.get("prev_hash") != prev:
                probs.append("帳簿異常: %d行目の prev_hash が直前の行と合わない (書き換えの疑い)" % i)
            why = check_row(r, self.protected)
            if why:
                probs.append("帳簿異常: %d行目 %s" % (i, why))
            else:
                if r["type"] == "grant" and str(r["ref"]).startswith("budget-"):
                    if r["ref"] in budget_refs:
                        probs.append("帳簿異常: %d行目 %s の二重発行" % (i, r["ref"]))
                    budget_refs.add(r["ref"])
                apply(hold, r)
                neg = negatives(hold)
                if neg:
                    probs.append("帳簿異常: %d行目の時点で %s の持ち分がマイナス" % (i, "、".join(neg)))
            prev = sha(l)
            if len(probs) >= 10:
                break
        hp = head_path(self.path)
        if hp.exists():
            try:
                head = hp.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                head = None
            if head != prev:
                probs.append("帳簿異常: 最後の行のハッシュが head 記録と合わない (最終行の書き換え・削除・追加の疑い)")
        elif rows:
            probs.append("帳簿異常: head 記録 (%s) がない。初回なら `seal` を実行" % hp.name)
        return probs

    # ---------- 書き込み ----------
    def _check_known(self, out):
        allowed = self.departments + (TREASURY,)
        if out["dept"] not in allowed:
            raise LedgerError("追記を拒否: 設定 (departments) にない課: %r" % (out["dept"],))
        if out["type"] == "transfer" and out.get("to") not in allowed:
            raise LedgerError("追記を拒否: 設定 (departments) にない課: %r" % (out.get("to"),))

    @staticmethod
    def _check_holding_rules(hold, out):
        """持ち分に関する拒否ルール。合わなければ LedgerError。"""
        t, d, a = out["type"], out["dept"], cents(out["amount"])
        if t == "spend" and hold.get(d, 0) + a < 0:
            raise LedgerError("追記を拒否: %s の持ち分が足りない (持ち分 %.2f)" % (d, hold.get(d, 0) / 100))
        if t == "transfer" and hold.get(d, 0) - a < 0:
            raise LedgerError("追記を拒否: %s の持ち分が足りない (持ち分 %.2f)" % (d, hold.get(d, 0) / 100))
        if t == "adjust" and hold.get(d, 0) + a < 0:
            raise LedgerError("追記を拒否: adjust で持ち分がマイナスになる")

    def append_many(self, new_rows, now=None):
        """検査してから複数行をまとめて追記する。1行でも拒否されたら1行も書かない。"""
        if not new_rows:
            return []
        with locked(self.path):
            probs = self._verify()
            if probs:
                raise LedgerError("追記を拒否: " + " / ".join(probs[:3]))
            rows, raw, ends_nl = read(self.path)
            hold = self.holdings(rows)
            issued_refs = {r.get("ref") for r in rows if r["type"] == "grant"}
            prev = sha(raw[-1]) if raw else ZERO
            ts = (now or self.now()).isoformat(timespec="seconds")
            outs, lines = [], []
            for i, row in enumerate(new_rows):
                if "amount" in row and row["amount"] is None:
                    raise LedgerError("追記を拒否: 金額が空 (None)")
                out = {"seq": len(rows) + i + 1, "ts": ts}
                out.update({k: v for k, v in row.items() if k not in ("seq", "ts", "prev_hash") and v is not None})
                out["amount"] = cents(out.get("amount", 0)) / 100
                out["note"] = str(out.get("note", ""))[:30]
                out["prev_hash"] = prev
                why = check_row(out, self.protected)
                if why:
                    raise LedgerError("追記を拒否: " + why)
                self._check_known(out)
                if out["type"] == "grant" and str(out["ref"]).startswith("budget-") and out["ref"] in issued_refs:
                    raise LedgerError("追記を拒否: %s は発行済み" % out["ref"])
                self._check_holding_rules(hold, out)
                apply(hold, out)
                line = dumps(out)
                prev = sha(line)
                outs.append(out)
                lines.append(line)
            with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                if not ends_nl:
                    f.write("\n")  # 末尾改行のない帳簿でも行が連結しないように
                f.write("\n".join(lines) + "\n")
                f.flush()
                os.fsync(f.fileno())
            with open(head_path(self.path), "w", encoding="utf-8", newline="\n") as f:
                f.write(prev + "\n")
        return outs

    def append(self, row, now=None):
        return self.append_many([row], now)[0]

    def seal(self):
        """head 記録が無い帳簿に head を作る。帳簿の鎖と中身が正しいときだけ。"""
        with locked(self.path):
            hp = head_path(self.path)
            if hp.exists():
                raise LedgerError("head 記録は既にある")
            rows, raw, _ = read(self.path)
            probs = [p for p in self._verify() if "head 記録" not in p]
            if probs:
                raise LedgerError("seal を拒否: " + " / ".join(probs[:3]))
            h = sha(raw[-1]) if raw else ZERO
            with open(hp, "w", encoding="utf-8", newline="\n") as f:
                f.write(h + "\n")
            return h

    # ---------- 発行・消費 ----------
    def grant_month(self, budgets, month, now=None):
        """月初の発行。全部を先に検査してから、1回の書き込みで全行を書く (途中で止まらない)。
        守られた課 (protected_depts) にはそれぞれ protected_min_per_case × protected_cases を先に配り、
        残りを split_depts で等分する。割り切れない端数は treasury に残る。"""
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", str(month)):
            raise LedgerError("月の形式は YYYY-MM")
        months = budgets.get("months") if isinstance(budgets, dict) else None
        b = months.get(month) if isinstance(months, dict) else None
        if not isinstance(b, dict):
            raise LedgerError("budgets.json に %s がない" % month)
        total = cents(_num(b, "total", lo=0.01))
        per_prot = cents(_num(b, "protected_min_per_case")) * _num(b, "protected_cases", integer=True)
        split = b.get("split_depts")
        if not isinstance(split, list) or not split or len(set(split)) != len(split) \
                or any(d not in self.departments or d in self.protected for d in split):
            raise LedgerError("budgets.json の split_depts が不正 (空・重複・設定にない課・守られた課を含む)")
        prot_total = per_prot * len(self.protected)
        each = (total - prot_total) // len(split)
        if each <= 0:
            raise LedgerError("発行総量が小さすぎて、等分する額が 0 以下になる")
        ref = "budget-" + month
        rows = [{"type": "grant", "dept": TREASURY, "amount": total / 100, "ref": ref, "note": "月初発行"}]
        if per_prot:
            rows += [{"type": "transfer", "dept": TREASURY, "to": d, "amount": per_prot / 100, "ref": ref, "note": "守られた課の最低額"}
                     for d in self.protected]
        rows += [{"type": "transfer", "dept": TREASURY, "to": d, "amount": each / 100, "ref": ref, "note": "等分"} for d in split]
        return self.append_many(rows, now)

    def charge(self, dept, model, tokens_in, tokens_out, equiv, ref, note="", amount=None, real_cost=0.0):
        """利用の記録。参考換算の分を課の持ち分から引く (実際のお金は動かない)。足りなければ、引けるだけ引いて不足を記録する。"""
        if dept not in self.departments:
            raise LedgerError("追記を拒否: 設定 (departments) にない課: %r" % (dept,))
        row = {"type": "spend", "dept": dept, "model": model, "tokens_in": tokens_in, "tokens_out": tokens_out,
               "equiv": equiv, "ref": ref, "note": note}
        if real_cost:
            row["real_cost"] = real_cost
        if amount is not None:
            row["amount"] = amount
            return self.append(row)
        with locked(self.path):
            probs = self._verify()
            if probs:
                raise LedgerError("追記を拒否: " + " / ".join(probs[:3]))
            have = self.holdings(read(self.path)[0])[dept]
        want = cents(equiv)
        take = min(want, max(have, 0))
        row["amount"] = -take / 100
        if take < want:
            row["shortfall"] = (want - take) / 100
        try:
            return self.append(row)
        except LedgerError as e:        # 確認から追記までの間に持ち分が動いた場合は、引かずに不足として記録する
            if "持ち分が足りない" not in str(e):
                raise
            row["amount"], row["shortfall"] = 0.0, want / 100
            return self.append(row)

    # ---------- 集計 ----------
    def summary(self, month=None, now=None):
        """集計を dict で返す。帳簿異常があれば problems に入り、数字は返さない。"""
        now = now or self.now()
        month = month or now.strftime("%Y-%m")
        try:
            with locked(self.path):
                probs = self._verify()
                rows, raw = ([], []) if probs else read(self.path)[:2]
        except LedgerError as e:
            probs, rows, raw = ["帳簿を確認できない: %s" % e], [], []
        base = {"month": month, "problems": probs, "unit": self.unit}
        if probs:
            return base
        hold = self.holdings(rows)
        mrows = [r for r in rows if str(r.get("ts", ""))[:7] == month]
        per = {d: {"tokens": 0, "equiv": 0.0, "burn": 0.0, "runs": 0} for d in hold}
        refs = {}
        for r in mrows:
            if r["type"] == "spend":
                p = per[r["dept"]]
                p["tokens"] += int(r.get("tokens_in", 0)) + int(r.get("tokens_out", 0))
                p["equiv"] += float(r.get("equiv") or 0)
                p["burn"] += -float(r["amount"])
                p["short"] = p.get("short", 0.0) + float(r.get("shortfall") or 0)
                p["real"] = p.get("real", 0.0) + float(r.get("real_cost") or 0)
                p["runs"] += 1
                refs[r["ref"]] = refs.get(r["ref"], 0.0) + float(r.get("equiv") or 0)
        base.update({
            "rows": len(rows), "head": sha(raw[-1]) if raw else ZERO,
            "holdings": {d: v / 100 for d, v in hold.items()},
            "issued": sum(float(r["amount"]) for r in rows if r["type"] == "grant" and r.get("ref") == "budget-" + month),
            "adjusted": sum(float(r["amount"]) for r in mrows if r["type"] == "adjust"),
            "burned": sum(p["burn"] for p in per.values()),
            "shortfall": round(sum(p.get("short", 0.0) for p in per.values()), 2),
            "real_cost": round(sum(p.get("real", 0.0) for p in per.values()), 2),
            "equiv": round(sum(p["equiv"] for p in per.values()), 2),
            "transfers_between_depts": sum(1 for r in rows if r["type"] == "transfer" and r["dept"] != TREASURY),
            "per_dept": per, "top_refs": sorted(refs.items(), key=lambda x: -x[1])[:3]})
        return base

    def summary_lines(self, s):
        if s["problems"]:
            return ["- **帳簿異常 (集計を止めた)**: " + " / ".join(s["problems"][:3])]
        if not s["rows"]:
            return ["- 帳簿は空 (未発行)"]
        u, c = self.unit, self.currency_label
        out = ["- 持ち分 (%s): " % u + "、".join("%s %.2f" % (d, v) for d, v in s["holdings"].items())]
        out.append("- %s: 発行 %.2f / 調整 %.2f / 消費 (持ち分から引いた %s) %.2f / 持ち分が足りず引けなかった分 %.2f / 実際に払った%s %.2f" % (
            s["month"], s["issued"], s["adjusted"], u, s["burned"], s["shortfall"], c, s["real_cost"]))
        used = ["%s %d回 %.1f万トークン (消費 %.2f)" % (d, p["runs"], p["tokens"] / 1e4, p["burn"]) for d, p in s["per_dept"].items() if p["runs"]]
        out.append("- 利用実績: " + ("、".join(used) if used else "記録なし"))
        if s["top_refs"]:
            out.append("- 参考換算の多い案件 (equiv の合計): " + "、".join("%s %.2f" % kv for kv in s["top_refs"]))
        out.append("- 課の間の取引 (treasury からの配分を除く): %d件" % s["transfers_between_depts"])
        out.append("- 帳簿の最終ハッシュ: %s (%d行。PCの外の記録に写しておく)" % (s["head"][:16], s["rows"]))
        return out


def load_json(p, name):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LedgerError("%s が見つからない: %s" % (name, Path(p).name))
    except (ValueError, UnicodeDecodeError):
        raise LedgerError("%s が JSON として読めない" % name)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m ee_aidea.ledger")
    ap.add_argument("--config", default="ee-aidea.json")
    ap.add_argument("--ledger"); ap.add_argument("--budgets"); ap.add_argument("--rates")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("verify"); sub.add_parser("seal")
    sm = sub.add_parser("summary"); sm.add_argument("--month", help="YYYY-MM (既定は今月)")
    g = sub.add_parser("grant-month"); g.add_argument("month")
    s = sub.add_parser("spend")
    s.add_argument("--dept", required=True); s.add_argument("--model", required=True)
    s.add_argument("--tokens-in", type=int, default=0); s.add_argument("--tokens-out", type=int, default=0)
    s.add_argument("--amount", type=float, default=None, help="指定しなければ、参考換算の分を持ち分から引く")
    s.add_argument("--real-cost", type=float, default=0.0, help="実際に払った額 (サブスク枠内なら 0)")
    s.add_argument("--ref", required=True); s.add_argument("--note", default="")
    t = sub.add_parser("transfer")
    t.add_argument("--from", dest="src", required=True); t.add_argument("--to", required=True)
    t.add_argument("--amount", type=float, required=True); t.add_argument("--ref", required=True); t.add_argument("--note", default="")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    try:
        cfg = CF.load(a.config)
        L = Ledger.from_config(cfg, a.ledger)
        if a.cmd == "verify":
            p = L.verify()
            print("\n".join(p) if p else "OK: %d行、異常なし" % len(read(L.path)[0]))
            return 1 if p else 0
        if a.cmd == "summary":
            s_ = L.summary(a.month)
            print("\n".join(L.summary_lines(s_)))
            return 1 if s_["problems"] else 0
        if a.cmd == "seal":
            print("head を作成: " + L.seal()[:16])
            return 0
        if a.cmd == "grant-month":
            for r in L.grant_month(load_json(a.budgets or cfg["ledger"]["budgets"], "budgets.json"), a.month):
                print(dumps(r))
            return 0
        if a.cmd == "spend":
            if a.tokens_in < 0 or a.tokens_out < 0:
                raise LedgerError("トークン数は0以上")
            eq = equiv_cost(load_json(a.rates or cfg["ledger"]["rates"], "rates.json"), a.model, a.tokens_in, a.tokens_out)
            if eq is None:
                raise LedgerError("rates.json にないモデル: %s" % a.model)
            print(dumps(L.charge(a.dept, a.model, a.tokens_in, a.tokens_out, eq, a.ref, a.note,
                                 amount=a.amount, real_cost=a.real_cost)))
            return 0
        if a.cmd == "transfer":
            print(dumps(L.append({"type": "transfer", "dept": a.src, "to": a.to, "amount": a.amount,
                                  "ref": a.ref, "note": a.note})))
            return 0
    except (LedgerError, CF.ConfigError) as e:
        print(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
