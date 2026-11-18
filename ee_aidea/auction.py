"""入札 (auction)。**記録だけ (shadow)**。落札の結果に従って作業を起動する機能はない。試験的で、効果は未検証。

LLM は判断しない。決まった計算だけ。
- 競るもの: 次の回にサブエージェントを起動する枠 (枠の数は呼ぶ側が決めて渡す。上限は設定 max_slots)
- 点数 = 期待できる価値 × 信用度 ÷ 上限 (max_bid)。高い順に枠の数だけ落札。同点は案件ID・課の名前の順
- 信用度 = 答え合わせの済んだ落札の「実績の合計 ÷ 申告の合計」(0〜1)。答え合わせが1件もない課は default_trust
- 入札できない場合: 根拠が空、期待値か上限が正の数でない (max_num 超も不可)、上限が min_max_bid 未満、
  課の持ち分が上限に足りない、設定にない課、同じ課から2件目
- 入札は帳簿に書かない (持ち分は動かない)。帳簿は持ち分の確認に読むだけ。

使い方 (ee-aidea.json のあるフォルダで):
  python -m ee_aidea.auction decide --round 20300101_0900 --slots 1 --bids bids/20300101_0900.json
  python -m ee_aidea.auction actual --round 20300101_0900 --ran research:R-1 [--ran qa:R-2]   実際に動かしたものを記録
  python -m ee_aidea.auction settle --round 20300101_0900 --dept research --realized 1200 --source "記録の出どころ"
  python -m ee_aidea.auction report                                                            落札と実際の一致を集計
入札ファイルの形: [{"dept":"research","ref":"R-1","task":"競合調査","max_bid":120,"expected_value":3000,"basis":"類似品の価格から見積もり"}]（basis は5文字以上）
"""
import argparse, datetime as dt, json, math, re, sys
from pathlib import Path

from . import config as CF
from . import ledger as L

ROUND = re.compile(r"^\d{8}_\d{4}$")
RAN = re.compile(r"^[a-z][a-z0-9_-]*:\S+$")


class AuctionError(Exception):
    pass


class Rules:
    """入札の決まり。設定 (ee-aidea.json の auction 節と departments) から作る。"""

    def __init__(self, bidders, max_slots=2, min_max_bid=10, default_trust=0.5, max_num=1e9):
        self.bidders = tuple(bidders)
        self.max_slots, self.min_max_bid = max_slots, min_max_bid
        self.default_trust, self.max_num = default_trust, max_num

    @classmethod
    def from_config(cls, cfg):
        a = cfg["auction"]
        return cls(cfg["departments"], a["max_slots"], a["min_max_bid"], a["default_trust"], a["max_num"])

    def num(self, v):
        """有限で、大きすぎない数か (10**400 のような桁外れの値で計算が落ちないように)。"""
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return False
        try:
            f = float(v)
        except OverflowError:
            return False
        return math.isfinite(f) and abs(f) <= self.max_num


def read_log(path, rules):
    p = Path(path)
    if not p.exists():
        return []
    data = p.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise AuctionError("入札の記録の %d行目を読めない (UTF-8 として壊れている)" % (data[:e.start].count(b"\n") + 1))
    out = []
    # 改行は \n だけで区切る (splitlines は U+2028 等でも区切り、記録が読めなくなるため)
    for i, l in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        if l.strip():
            try:
                r = json.loads(l)
            except ValueError:
                raise AuctionError("入札の記録の %d行目を読めない" % i)
            ev = r.get("event") if isinstance(r, dict) else None
            ok = isinstance(r, dict) and isinstance(r.get("round"), str) and (
                (ev == "decide" and isinstance(r.get("winners"), list) and isinstance(r.get("bids"), list)
                 and all(isinstance(b, dict) and (not b.get("won") or rules.num(b.get("expected_value"))) for b in r["bids"]))
                or (ev == "actual" and isinstance(r.get("ran"), list))
                or (ev == "settle" and isinstance(r.get("dept"), str) and rules.num(r.get("realized_value"))))
            if not ok:
                raise AuctionError("入札の記録の %d行目の形が不正" % i)
            out.append(r)
    return out


def add_log(path, row):
    """呼ぶ側が L.locked(記録のパス) を取った状態で呼ぶ (cmd_* を参照)。"""
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def trust(log, rules):
    """課ごとの信用度。答え合わせ (settle) の済んだ落札だけで計算する。"""
    claimed, realized = {}, {}
    decided = {r["round"]: r for r in log if r.get("event") == "decide"}
    for r in log:
        if r.get("event") != "settle" or r.get("round") not in decided:
            continue
        for b in decided[r["round"]]["bids"]:
            if b.get("dept") == r.get("dept") and b.get("won"):
                claimed[b["dept"]] = claimed.get(b["dept"], 0.0) + b["expected_value"]
                realized[b["dept"]] = realized.get(b["dept"], 0.0) + max(0.0, float(r["realized_value"]))
    return {d: (min(1.0, realized[d] / claimed[d]) if claimed.get(d, 0) > 0 else rules.default_trust) for d in rules.bidders}


def decide(bids, holdings, tr, slots, rules):
    """入札の一覧に点数と可否を付け、落札を決める。(結果の一覧, 落札した入札の一覧)"""
    if isinstance(slots, bool) or not isinstance(slots, int) or not 0 <= slots <= rules.max_slots:
        raise AuctionError("枠の数は 0〜%d" % rules.max_slots)
    if not isinstance(bids, list):
        raise AuctionError("入札ファイルは一覧 ([...]) の形")
    out, seen = [], set()
    for b in bids:
        if not isinstance(b, dict):
            out.append({"dept": None, "eligible": False, "reason": "入札の形が不正", "score": 0.0, "won": False})
            continue
        r = {"dept": b.get("dept"), "ref": str(b.get("ref") or ""), "task": str(b.get("task") or "")[:60],
             "max_bid": b.get("max_bid"), "expected_value": b.get("expected_value"), "basis": str(b.get("basis") or "")[:200],
             "eligible": True, "reason": "", "score": 0.0, "won": False}

        def no(why):
            r["eligible"], r["reason"] = False, why
        if r["dept"] not in rules.bidders:
            no("設定にない課")
        elif r["dept"] in seen:
            no("同じ課から2件目")
        elif not r["ref"].strip():
            no("案件IDがない")
        elif len(re.sub(r"\s", "", r["basis"])) < 5:
            no("根拠がない")
        elif not rules.num(r["max_bid"]) or r["max_bid"] <= 0 or not rules.num(r["expected_value"]) or r["expected_value"] <= 0:
            no("期待値か上限が正の数でない")
        elif r["max_bid"] < rules.min_max_bid:
            no("上限が小さすぎる (下限 %s)" % rules.min_max_bid)
        elif holdings.get(r["dept"], 0) < r["max_bid"]:
            no("持ち分が上限に足りない (持ち分 %.2f)" % holdings.get(r["dept"], 0))
        if r["dept"] in rules.bidders:
            seen.add(r["dept"])
        if r["eligible"]:
            r["trust"] = tr.get(r["dept"], rules.default_trust)
            r["score"] = round(r["expected_value"] * r["trust"] / r["max_bid"], 6)
        out.append(r)
    ranked = sorted([r for r in out if r["eligible"]], key=lambda r: (-r["score"], r["ref"], r["dept"]))
    for r in ranked[:slots]:
        r["won"] = True
    return out, ranked[:slots]


def _now(cfg):
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=cfg["utc_offset_hours"]))).isoformat(timespec="seconds")


def cmd_decide(a, cfg, rules, book):
    with L.locked(a.log):    # 「済みか」の判定と追記を同じロックの中で行う
        if not ROUND.match(a.round):
            raise AuctionError("回の名前は YYYYMMDD_HHMM")
        log = read_log(a.log, rules)
        if any(r.get("event") == "decide" and r.get("round") == a.round for r in log):
            raise AuctionError("この回は決定済み")
        try:
            bids = json.loads(Path(a.bids).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise AuctionError("入札ファイルがない: %s" % Path(a.bids).name)
        except (ValueError, UnicodeDecodeError):
            raise AuctionError("入札ファイルを JSON として読めない")
        s = book.summary()
        if s["problems"]:
            raise AuctionError("帳簿に異常があるため入札を止めた: " + s["problems"][0])
        out, winners = decide(bids, s["holdings"], trust(log, rules), a.slots, rules)
        add_log(a.log, {"event": "decide", "round": a.round, "mode": "shadow", "slots": a.slots,
                        "ts": _now(cfg), "bids": out,
                        "winners": ["%s:%s" % (w["dept"], w["ref"]) for w in winners]})
    print("入札 %s (記録だけ。実際の作業はこの結果に従わない) 枠 %d" % (a.round, a.slots))
    for r in sorted(out, key=lambda r: -r["score"]):
        print("  %s %-11s %-6s 点数 %-10s 期待 %s / 上限 %s %s %s" % (
            "落札" if r["won"] else ("不可" if not r["eligible"] else "　　"), r["dept"], r.get("ref", ""),
            r["score"], r.get("expected_value"), r.get("max_bid"), cfg["unit"], r["reason"]))
    return 0


def cmd_actual(a, cfg, rules):
    with L.locked(a.log):
        log = read_log(a.log, rules)
        if not any(r.get("event") == "decide" and r.get("round") == a.round for r in log):
            raise AuctionError("この回の決定がない")
        ran = sorted(a.ran or [])
        if any(not RAN.match(x) for x in ran):
            raise AuctionError("--ran は 課:案件ID の形")
        add_log(a.log, {"event": "actual", "round": a.round, "ran": ran, "ts": _now(cfg)})
    print("記録しました: 実際に動かしたもの %s" % (ran or "なし"))
    return 0


def cmd_settle(a, cfg, rules):
    with L.locked(a.log):
        log = read_log(a.log, rules)
        d = [r for r in log if r.get("event") == "decide" and r.get("round") == a.round]
        if not d or not any(b.get("dept") == a.dept and b.get("won") for b in d[0]["bids"]):
            raise AuctionError("この回に、その課の落札がない")
        if any(r.get("event") == "settle" and r.get("round") == a.round and r.get("dept") == a.dept for r in log):
            raise AuctionError("答え合わせ済み")
        if not rules.num(a.realized) or a.realized < 0 or len(a.source.strip()) < 3:
            raise AuctionError("実績は0以上の数、出どころ (--source) は必須")
        add_log(a.log, {"event": "settle", "round": a.round, "dept": a.dept, "realized_value": a.realized,
                        "source": a.source[:100], "ts": _now(cfg)})
    print("答え合わせを記録しました。")
    return 0


def report(log, rules):
    dec = [r for r in log if r.get("event") == "decide"]
    act = {r["round"]: r for r in log if r.get("event") == "actual"}
    both = [r for r in dec if r["round"] in act]
    same = sum(1 for r in both if sorted(r["winners"]) == sorted(act[r["round"]]["ran"]))
    lines = ["- 入札 (記録だけ): 決定 %d回 / 実際の記録あり %d回 / 落札と実際が一致 %d回" % (len(dec), len(both), same)]
    tr = trust(log, rules)
    if any(r.get("event") == "settle" for r in log):
        lines.append("- 信用度: " + "、".join("%s %.2f" % (d, tr[d]) for d in rules.bidders))
    for r in both[-3:]:
        lines.append("  - %s 落札 %s / 実際 %s" % (r["round"], r["winners"] or "なし", act[r["round"]]["ran"] or "なし"))
    return lines


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="python -m ee_aidea.auction")
    ap.add_argument("--config", default="ee-aidea.json")
    ap.add_argument("--ledger"); ap.add_argument("--log")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decide"); d.add_argument("--round", required=True); d.add_argument("--slots", type=int, required=True)
    d.add_argument("--bids", required=True)
    c = sub.add_parser("actual"); c.add_argument("--round", required=True); c.add_argument("--ran", action="append")
    s = sub.add_parser("settle"); s.add_argument("--round", required=True); s.add_argument("--dept", required=True)
    s.add_argument("--realized", type=float, required=True); s.add_argument("--source", required=True)
    sub.add_parser("report")
    a = ap.parse_args(argv)
    try:
        cfg = CF.load(a.config)
        rules = Rules.from_config(cfg)
        a.log = a.log or cfg["auction"]["log"]
        Path(a.log).parent.mkdir(parents=True, exist_ok=True)
        if a.cmd == "decide":
            return cmd_decide(a, cfg, rules, L.Ledger.from_config(cfg, a.ledger))
        if a.cmd == "actual":
            return cmd_actual(a, cfg, rules)
        if a.cmd == "settle":
            return cmd_settle(a, cfg, rules)
        print("\n".join(report(read_log(a.log, rules), rules)))
        return 0
    except (AuctionError, L.LedgerError, CF.ConfigError) as e:
        print(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
