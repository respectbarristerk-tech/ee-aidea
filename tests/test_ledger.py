"""帳簿のテスト (ネットなし)。使い方: キットのフォルダで  python -m unittest discover -s tests -v"""
import contextlib, hashlib, io, json, multiprocessing as mp, sys, tempfile, unittest
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from ee_aidea import ledger as Y

DEPTS = ["research", "engineering", "qa", "writer"]
PROT = ["qa"]
B = {"months": {"2026-11": {"total": 2100, "protected_min_per_case": 150, "protected_cases": 3,
                            "split_depts": ["research", "engineering", "writer"], "basis": "テスト"}}}
R = {"fx_rate": 150, "models": {"model-a": {"in_usd_per_mtok": 2.0, "out_usd_per_mtok": 10.0}}}
CFG = {"departments": DEPTS, "protected_depts": PROT, "ledger": {"rates": "r.json", "budgets": "b.json"}}


def run(args):
    with contextlib.redirect_stdout(io.StringIO()):
        return Y.main(args)


def book(path, depts=DEPTS, prot=PROT):
    return Y.Ledger(path, depts, prot)


def _race(path, barrier, q):
    sys.path.insert(0, str(HERE))
    from ee_aidea import ledger as Y2
    barrier.wait()
    try:
        Y2.Ledger(path, DEPTS, PROT).append({"type": "transfer", "dept": "research", "to": "engineering", "amount": 300, "ref": "race"})
        q.put("ok")
    except Y2.LedgerError:
        q.put("rejected")


def forge(path, mutate):
    """帳簿の行を書き換え、鎖と head を正しく計算し直して書き戻す (ファイルを書ける者による改ざんの再現)。"""
    rows = [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    mutate(rows)
    prev, out = Y.ZERO, []
    for i, r in enumerate(rows, 1):
        r["seq"], r["prev_hash"] = i, prev
        line = Y.dumps(r)
        prev = hashlib.sha256(line.encode()).hexdigest()
        out.append(line)
    Path(path).write_text("\n".join(out) + "\n", encoding="utf-8")
    Y.head_path(path).write_text(prev + "\n", encoding="utf-8")


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = str(Path(self.tmp.name) / "ledger.jsonl")
        self.L = book(self.p)

    def tearDown(self):
        self.tmp.cleanup()

    def n(self):
        return len(Y.read(self.p)[0])

    def hold(self):
        return self.L.holdings(Y.read(self.p)[0])

    def test_grant_month_and_holdings(self):
        self.L.grant_month(B, "2026-11")
        h = self.hold()
        self.assertEqual(sum(h.values()), 210000)
        self.assertEqual((h["qa"], h["treasury"], h["research"]), (45000, 0, 55000))
        self.assertEqual(self.L.verify(), [])
        with self.assertRaises(Y.LedgerError):
            self.L.grant_month(B, "2026-11")

    def test_tamper_middle_line(self):
        self.L.grant_month(B, "2026-11")
        s = Path(self.p).read_text(encoding="utf-8").replace('"amount":450.0', '"amount":950.0')
        Path(self.p).write_text(s, encoding="utf-8")
        self.assertTrue(any("prev_hash" in x for x in self.L.verify()))
        with self.assertRaises(Y.LedgerError):
            self.L.append({"type": "transfer", "dept": "research", "to": "engineering", "amount": 1, "ref": "R-1"})

    def test_deleted_line_is_detected(self):
        self.L.grant_month(B, "2026-11")
        lines = Path(self.p).read_text(encoding="utf-8").split("\n")
        Path(self.p).write_text("\n".join(lines[:2] + lines[3:]), encoding="utf-8")
        self.assertTrue(self.L.verify())

    def test_guards(self):
        self.L.grant_month(B, "2026-11")
        for row in [{"type": "transfer", "dept": "qa", "to": "research", "amount": 10, "ref": "x"},
                    {"type": "spend", "dept": "research", "amount": -9999, "ref": "x"},
                    {"type": "transfer", "dept": "research", "to": "engineering", "amount": 9999, "ref": "x"},
                    {"type": "reward", "dept": "qa", "amount": 10, "ref": "x"},          # この版に無い種類
                    {"type": "bid", "dept": "qa", "amount": 0, "ref": "x"},              # 入札は帳簿に書かない
                    {"type": "adjust", "dept": "qa", "amount": -10, "ref": "x"}]:
            with self.assertRaises(Y.LedgerError, msg=repr(row)):
                self.L.append(row)
        self.assertEqual(self.n(), 5)

    def test_spend_burn_and_equiv(self):
        self.L.grant_month(B, "2026-11")
        eq = Y.equiv_cost(R, "model-a", 100000, 10000)
        self.assertAlmostEqual(eq, (0.1 * 2 + 0.01 * 10) * 150)
        self.L.append({"type": "spend", "dept": "qa", "amount": 0, "model": "model-a", "tokens_in": 100000,
                       "tokens_out": 10000, "equiv": eq, "ref": "R-1"})
        self.L.append({"type": "spend", "dept": "research", "amount": -12.4, "model": "model-a", "ref": "R-2"})
        self.assertEqual(sum(self.hold().values()), 210000 - 1240)
        s = self.L.summary("2026-11")
        self.assertAlmostEqual(s["burned"], 12.4)
        self.assertEqual(s["per_dept"]["qa"]["tokens"], 110000)

    def test_transfer_between_depts(self):
        self.L.grant_month(B, "2026-11")
        self.L.append({"type": "transfer", "dept": "research", "to": "engineering", "amount": 50, "ref": "R-2"})
        h = self.hold()
        self.assertEqual((h["research"], h["engineering"], h["qa"], h["treasury"]), (50000, 60000, 45000, 0))
        self.assertEqual(self.L.summary("2026-11")["transfers_between_depts"], 1)
        self.assertEqual(self.L.verify(), [])

    def test_empty_ledger(self):
        self.assertEqual(self.L.verify(), [])
        self.assertIn("空", self.L.summary_lines(self.L.summary())[0])

    def test_no_trailing_newline(self):
        self.L.grant_month(B, "2026-11")
        s = Path(self.p).read_text(encoding="utf-8").rstrip("\n")
        Path(self.p).write_text(s, encoding="utf-8")
        self.assertEqual(self.L.verify(), [])
        self.L.append({"type": "spend", "dept": "research", "amount": 0, "ref": "R-1"})
        self.assertEqual(self.L.verify(), [])
        self.assertEqual(self.n(), 6)

    def test_concurrent_transfers(self):
        self.L.grant_month(B, "2026-11")
        ctx = mp.get_context("fork") if sys.platform != "win32" else mp
        barrier, q = ctx.Barrier(4), ctx.Queue()
        ps = [ctx.Process(target=_race, args=(self.p, barrier, q)) for _ in range(4)]
        for p in ps: p.start()
        for p in ps: p.join(30)
        res = sorted(q.get() for _ in ps)
        self.assertEqual(res.count("ok"), 1)
        self.assertEqual(self.L.verify(), [])
        self.assertGreaterEqual(self.hold()["research"], 0)

    def test_last_line_tamper_detected(self):
        self.L.grant_month(B, "2026-11")
        orig = Path(self.p).read_text(encoding="utf-8")
        last_prev = Y.read(self.p)[0][-1]["prev_hash"]
        Path(self.p).write_text(orig.replace('"amount":550.0,"ref":"budget-2026-11","note":"等分","prev_hash":"' + last_prev,
                                             '"amount":100.0,"ref":"budget-2026-11","note":"等分","prev_hash":"' + last_prev), encoding="utf-8")
        self.assertNotEqual(Path(self.p).read_text(encoding="utf-8"), orig)
        self.assertTrue(any("head" in x for x in self.L.verify()))
        lines = orig.splitlines()
        Path(self.p).write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        self.assertTrue(any("head" in x for x in self.L.verify()))
        fake = {"seq": 6, "ts": "2026-11-12T00:00:00+09:00", "type": "transfer", "dept": "research", "to": "writer",
                "amount": 1.0, "ref": "x", "note": "", "prev_hash": hashlib.sha256(lines[-1].encode()).hexdigest()}
        Path(self.p).write_text(orig + Y.dumps(fake) + "\n", encoding="utf-8")
        self.assertTrue(any("head" in x for x in self.L.verify()))

    def test_missing_head_and_seal(self):
        self.L.grant_month(B, "2026-11")
        Y.head_path(self.p).rename(Path(self.tmp.name) / "moved")
        self.assertTrue(any("seal" in x for x in self.L.verify()))
        self.L.seal()
        self.assertEqual(self.L.verify(), [])
        with self.assertRaises(Y.LedgerError):
            self.L.seal()

    def test_forged_bad_content(self):
        for bad in [{"type": "spend", "dept": "research", "amount": 500.0},
                    {"type": "grant", "dept": "research", "amount": -100.0},
                    {"type": "bid", "dept": "research", "amount": 0.0},
                    {"type": "transfer", "dept": "qa", "to": "research", "amount": 100.0},
                    {"type": "transfer", "dept": "research", "to": "qa", "amount": -100.0},
                    {"type": "spend", "dept": "Bad Name", "amount": 0.0}]:
            for f in (Path(self.p), Y.head_path(self.p)):
                if f.exists():
                    f.unlink()
            self.L.grant_month(B, "2026-11")
            forge(self.p, lambda rows: rows.append(dict(bad, ts="2026-11-12T00:00:00+09:00", ref="x", note="")))
            self.assertTrue(self.L.verify(), bad)

    def test_bad_amounts_not_written(self):
        self.L.grant_month(B, "2026-11")
        for a in [float("nan"), float("inf"), "abc", "", None, [1], 1e308, True, 0.005, -12.345]:
            with self.assertRaises(Y.LedgerError, msg=repr(a)):
                self.L.append({"type": "spend", "dept": "research", "amount": a, "ref": "x"})
        with self.assertRaises(Y.LedgerError):
            self.L.append({"type": "spend", "dept": "research", "amount": 0, "tokens_out": "zz", "ref": "x"})
        with self.assertRaises(Y.LedgerError):
            self.L.append({"type": "spend", "dept": "research", "amount": 0, "tokens_in": -5, "ref": "x"})
        with self.assertRaises(Y.LedgerError):
            self.L.append({"type": "spend", "dept": "research", "amount": 0, "ref": ""})
        self.assertEqual(self.n(), 5)
        self.assertEqual(self.L.verify(), [])

    def test_broken_files_are_problems(self):
        for content in [b"[1,2]\n", b"null\n", b"\xff\xfe\n", "﻿{}\n".encode("utf-8")]:
            Path(self.p).write_bytes(content)
            self.assertTrue(self.L.verify())
            self.assertTrue(self.L.summary()["problems"])

    def write_cfg(self):
        d = Path(self.tmp.name)
        (d / "ee-aidea.json").write_text(json.dumps(dict(CFG, ledger=dict(CFG["ledger"], path="ledger.jsonl"))), encoding="utf-8")
        return str(d / "ee-aidea.json")

    def test_cli_errors_are_clean(self):
        d = Path(self.tmp.name)
        cfg = self.write_cfg()
        self.assertEqual(run(["--config", cfg, "grant-month", "2026-11"]), 1)                 # budgets.json がない
        self.assertEqual(run(["--config", str(d / "none.json"), "verify"]), 1)               # 設定がない
        (d / "b.json").write_text(json.dumps(B), encoding="utf-8")
        (d / "r.json").write_text(json.dumps(R), encoding="utf-8")
        self.assertEqual(run(["--config", cfg, "grant-month", "2026-11"]), 0)
        self.assertEqual(run(["--config", cfg, "spend", "--dept", "qa", "--model", "model-a", "--amount", "nan", "--ref", "x"]), 1)
        self.assertEqual(run(["--config", cfg, "spend", "--dept", "qa", "--model", "unknown", "--ref", "x"]), 1)
        self.assertEqual(run(["--config", cfg, "spend", "--dept", "qa", "--model", "model-a", "--tokens-in", "-5", "--ref", "x"]), 1)
        self.assertEqual(run(["--config", cfg, "verify"]), 0)
        self.assertEqual(run(["--config", cfg, "summary"]), 0)
        self.assertEqual(run(["--config", cfg, "summary", "--month", "2026-11"]), 0)
        Path(self.p).write_text("broken\n", encoding="utf-8")
        self.assertEqual(run(["--config", cfg, "summary"]), 1)

    def test_grant_month_is_atomic(self):
        m = B["months"]["2026-11"]
        bad = [dict(m, split_depts=["research", "ghost"]),
               dict(m, split_depts=["research", "research"]),
               dict(m, split_depts=["qa"]),                      # 守られた課は等分の対象にしない
               dict(m, split_depts=["treasury"]),
               dict(m, split_depts=[]),
               dict(m, protected_cases=1.5),
               dict(m, protected_min_per_case=-1),
               dict(m, total="abc"),
               dict(m, total=10),                                # 守られた課の最低額だけで尽きる
               {k: v for k, v in m.items() if k != "protected_cases"}]
        for b in bad:
            with self.assertRaises(Y.LedgerError, msg=repr(b)):
                self.L.grant_month({"months": {"2026-12": b}}, "2026-12")
            self.assertEqual(self.n(), 0)
        with self.assertRaises(Y.LedgerError):
            self.L.grant_month(B, "2026-13")

    def test_duplicate_budget_grant(self):
        self.L.grant_month(B, "2026-11")
        with self.assertRaises(Y.LedgerError):
            self.L.append({"type": "grant", "dept": "treasury", "amount": 2100, "ref": "budget-2026-11"})

    def test_bad_rates_and_budgets(self):
        for bad in [[], {"models": []}, {"fx_rate": 150, "models": {"model-a": {"in_usd_per_mtok": "2", "out_usd_per_mtok": 10}}},
                    {"fx_rate": 150, "models": {"model-a": {"in_usd_per_mtok": 2}}},
                    {"fx_rate": -150, "models": {"model-a": {"in_usd_per_mtok": 2, "out_usd_per_mtok": 10}}}]:
            with self.assertRaises(Y.LedgerError, msg=repr(bad)):
                Y.equiv_cost(bad, "model-a", 1000, 10)
        with self.assertRaises(Y.LedgerError):
            self.L.grant_month({"months": []}, "2026-11")
        nodir = book(str(Path(self.tmp.name) / "nodir" / "ledger.jsonl"))
        self.assertTrue(nodir.verify())
        with self.assertRaises(Y.LedgerError):
            nodir.append({"type": "spend", "dept": "qa", "amount": 0, "ref": "x"})

    def test_verify_during_appends(self):
        import threading
        self.L.grant_month(B, "2026-11")
        bad, stop = [], threading.Event()

        def watcher():
            while not stop.is_set():
                pr = self.L.verify()
                if pr: bad.append(pr)
                if self.L.summary("2026-11")["problems"]: bad.append("summary")
        th = threading.Thread(target=watcher); th.start()
        try:
            for i in range(60):
                self.L.append({"type": "spend", "dept": "research", "amount": 0, "ref": "R-%d" % i})
        finally:
            stop.set(); th.join()
        self.assertEqual(bad, [])
        self.assertEqual(self.n(), 65)

    def test_crlf_and_duplicate_grant(self):
        self.L.grant_month(B, "2026-11")
        raw = Path(self.p).read_bytes()
        Path(self.p).write_bytes(raw.replace(b"\n", b"\r\n"))
        self.assertTrue(any("CRLF" in x for x in self.L.verify()))
        Path(self.p).write_bytes(raw)
        forge(self.p, lambda rows: rows.append({"ts": "2026-11-12T00:00:00+09:00", "type": "grant", "dept": "treasury",
                                                "amount": 2100.0, "ref": "budget-2026-11", "note": ""}))
        self.assertTrue(any("二重発行" in x for x in self.L.verify()))

    def test_charge_and_shortfall(self):
        self.L.grant_month(B, "2026-11")
        r = self.L.charge("research", "model-a", 100000, 0, 30.0, "R-1", "調査")
        self.assertEqual(r["amount"], -30.0)
        self.assertEqual(self.hold()["research"], 55000 - 3000)
        r = self.L.charge("research", "model-a", 900000, 0, 600.0, "R-1", "大きな調査")     # 持ち分 520 しかない
        self.assertEqual((r["amount"], r["shortfall"]), (-520.0, 80.0))
        self.assertEqual(self.hold()["research"], 0)
        r = self.L.charge("research", "model-a", 1000, 0, 5.0, "R-1")
        self.assertEqual((r["amount"], r["shortfall"]), (0.0, 5.0))
        s = self.L.summary("2026-11")
        self.assertAlmostEqual(s["burned"], 550.0); self.assertAlmostEqual(s["shortfall"], 85.0)
        self.assertEqual(self.L.verify(), [])
        self.assertEqual(sum(self.hold().values()), 210000 - 55000)       # 引いた分だけ総量が減る

    def test_cli_spend_charges(self):
        d = Path(self.tmp.name)
        cfg = self.write_cfg()
        (d / "b.json").write_text(json.dumps(B), encoding="utf-8"); (d / "r.json").write_text(json.dumps(R), encoding="utf-8")
        run(["--config", cfg, "grant-month", "2026-11"])
        self.assertEqual(run(["--config", cfg, "spend", "--dept", "qa", "--model", "model-a",
                                 "--tokens-in", "100000", "--ref", "R-7"]), 0)
        self.assertEqual(self.hold()["qa"], 45000 - 3000)                  # 0.1M×$2×150 = 30

    # --- 課を設定から読む ---
    def test_unknown_dept_rejected(self):
        self.L.grant_month(B, "2026-11")
        for row in [{"type": "spend", "dept": "ghost", "amount": 0, "ref": "x"},
                    {"type": "transfer", "dept": "research", "to": "ghost", "amount": 1, "ref": "x"},
                    {"type": "adjust", "dept": "ghost", "amount": 1, "ref": "x"}]:
            with self.assertRaises(Y.LedgerError, msg=repr(row)):
                self.L.append(row)
        with self.assertRaises(Y.LedgerError):
            self.L.charge("ghost", "model-a", 1, 1, 1.0, "x")
        self.assertEqual(self.n(), 5)

    def test_custom_protected(self):
        L2 = book(self.p, prot=["qa", "research"])
        L2.grant_month({"months": {"2026-11": dict(B["months"]["2026-11"], split_depts=["engineering", "writer"])}}, "2026-11")
        h = L2.holdings(Y.read(self.p)[0])
        self.assertEqual((h["qa"], h["research"]), (45000, 45000))
        for row in [{"type": "transfer", "dept": "research", "to": "engineering", "amount": 1, "ref": "x"},
                    {"type": "adjust", "dept": "research", "amount": -1, "ref": "x"},
                    {"type": "transfer", "dept": "qa", "to": "engineering", "amount": 1, "ref": "x"}]:
            with self.assertRaises(Y.LedgerError, msg=repr(row)):
                L2.append(row)
        L2.append({"type": "transfer", "dept": "engineering", "to": "research", "amount": 1, "ref": "x"})
        L2.append({"type": "spend", "dept": "research", "amount": -1, "ref": "x"})       # 使うのは自由
        self.assertEqual(L2.verify(), [])

    def test_removed_dept_rows_readable(self):
        self.L.grant_month(B, "2026-11")
        L2 = book(self.p, depts=["research", "engineering", "qa"])          # writer を設定から外した
        self.assertEqual(L2.verify(), [])
        s = L2.summary("2026-11")
        self.assertEqual(s["holdings"]["writer"], 550.0)                     # 過去の行は読める
        with self.assertRaises(Y.LedgerError):
            L2.append({"type": "spend", "dept": "writer", "amount": 0, "ref": "x"})   # 追記はできない
        L2.append({"type": "transfer", "dept": "research", "to": "engineering", "amount": 1, "ref": "x"})

    def test_bad_department_settings(self):
        for depts, prot in [([], []), (["treasury"], []), (["Research"], []), (["a b"], []), (["x", "x"], []),
                            (["research"], ["qa"]), ("research", [])]:
            with self.assertRaises(Y.LedgerError, msg=repr((depts, prot))):
                Y.Ledger(self.p, depts, prot)


if __name__ == "__main__":
    unittest.main()
