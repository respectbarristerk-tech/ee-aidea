"""入札のテスト (ネットなし)。使い方: キットのフォルダで  python -m unittest discover -s tests -v"""
import contextlib, io, json, os, subprocess, sys, tempfile, threading, time, unittest
from unittest import mock
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from ee_aidea import ledger as Y
from ee_aidea import auction as A

DEPTS = ["research", "engineering", "qa", "writer", "ops"]
B = {"months": {"2026-11": {"total": 2100, "protected_min_per_case": 150, "protected_cases": 3,
                            "split_depts": ["research", "engineering", "writer", "ops"]}}}
BAL = {"research": 287.5, "engineering": 137.5, "qa": 600.0, "writer": 287.5, "ops": 287.5, "treasury": 400.0}
RU = A.Rules(DEPTS)
TR = {d: 0.5 for d in DEPTS}


def bid(dept, ref, mx, exp, basis="似た作業2件の記録から"):
    return {"dept": dept, "ref": ref, "task": "t", "max_bid": mx, "expected_value": exp, "basis": basis}


def setup_dir(t):
    """設定・帳簿・入札ファイルを作る。(設定のパス, 帳簿のパス, 記録のパス, 入札ファイルのパス)"""
    d = Path(t)
    cfg = {"departments": DEPTS, "protected_depts": ["qa"], "ledger": {"path": "ledger.jsonl"}, "auction": {"log": "log.jsonl"}}
    (d / "ee-aidea.json").write_text(json.dumps(cfg), encoding="utf-8")
    led = str(d / "ledger.jsonl")
    Y.Ledger(led, DEPTS, ["qa"]).grant_month(B, "2026-11")
    return str(d / "ee-aidea.json"), led, str(d / "log.jsonl"), str(d / "b.json")


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class T(unittest.TestCase):
    def test_score_order_and_slots(self):
        out, win = A.decide([bid("research", "R-11", 100, 3000), bid("engineering", "R-7", 50, 2000), bid("ops", "R-12", 100, 100)], BAL, TR, 1, RU)
        self.assertEqual([(w["dept"], w["ref"]) for w in win], [("engineering", "R-7")])          # 2000*0.5/50=20 > 15 > 0.5
        self.assertEqual(len(A.decide([bid("research", "R-11", 100, 3000), bid("engineering", "R-7", 50, 2000)], BAL, TR, 2, RU)[1]), 2)
        self.assertEqual(A.decide([bid("research", "R-11", 100, 3000)], BAL, TR, 0, RU)[1], [])
        with self.assertRaises(A.AuctionError):
            A.decide([], BAL, TR, 3, RU)                                                          # 既定の上限は2
        self.assertEqual(len(A.decide([bid("research", "R-1", 100, 3000)], BAL, TR, 3, A.Rules(DEPTS, max_slots=3))[1]), 1)

    def test_ineligible_bids(self):
        cases = [bid("research", "R-1", 100, 3000, basis=" "), bid("research", "R-1", 0, 3000), bid("research", "R-1", 100, -5),
                 bid("research", "R-1", float("nan"), 10), bid("engineering", "R-1", 200, 3000),        # 持ち分 137.5 < 200
                 bid("treasury", "R-1", 10, 10), bid("ghost", "R-1", 10, 10), bid("research", "", 10, 10), "x", bid("research", "R-1", True, 10)]
        for c in cases:
            out, win = A.decide([c], BAL, TR, 1, RU)
            self.assertEqual(win, [], repr(c)); self.assertFalse(out[0]["eligible"])
        out, win = A.decide([bid("research", "R-1", 10, 10), bid("research", "R-2", 10, 99999)], BAL, TR, 2, RU)
        self.assertEqual(len(win), 1)                                                                 # 同じ課から2件目は不可

    def test_bidders_come_from_config(self):
        ru = A.Rules(["alpha", "beta"])
        out, win = A.decide([bid("research", "R-1", 10, 100), bid("alpha", "R-2", 10, 100)], {"research": 99, "alpha": 99}, {}, 1, ru)
        self.assertFalse(out[0]["eligible"]); self.assertEqual(win[0]["dept"], "alpha")
        self.assertFalse(A.decide([bid("alpha", "R-1", 9, 100)], {"alpha": 99}, {}, 1, A.Rules(["alpha"], min_max_bid=10))[0][0]["eligible"])
        self.assertTrue(A.decide([bid("alpha", "R-1", 9, 100)], {"alpha": 99}, {}, 1, A.Rules(["alpha"], min_max_bid=5))[0][0]["eligible"])

    def test_tie_break_is_deterministic(self):
        bids = [bid("research", "R-20", 100, 1000), bid("ops", "R-10", 100, 1000)]
        self.assertEqual(A.decide(bids, BAL, TR, 1, RU)[1][0]["ref"], "R-10")
        self.assertEqual(A.decide(bids[::-1], BAL, TR, 1, RU)[1][0]["ref"], "R-10")

    def test_trust_lowers_overclaimers(self):
        log = [{"event": "decide", "round": "20261101_0617", "bids": [dict(bid("research", "R-1", 100, 10000), won=True)]},
               {"event": "settle", "round": "20261101_0617", "dept": "research", "realized_value": 1000}]
        tr = A.trust(log, RU)
        self.assertAlmostEqual(tr["research"], 0.1); self.assertEqual(tr["ops"], 0.5)
        win = A.decide([bid("research", "R-11", 100, 3000), bid("ops", "R-12", 100, 1000)], BAL, tr, 1, RU)[1]
        self.assertEqual(win[0]["dept"], "ops")                                                   # 3000*0.1 < 1000*0.5
        self.assertEqual(A.trust([], A.Rules(DEPTS, default_trust=0.3))["ops"], 0.3)

    def test_cli_flow_and_report(self):
        with tempfile.TemporaryDirectory() as t:
            cfg, led, log, bf = setup_dir(t)
            Path(bf).write_text(json.dumps([bid("research", "R-11", 100, 3000), bid("ops", "R-12", 100, 100)]), encoding="utf-8")
            base = ["--config", cfg]
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                self.assertEqual(A.main(base + ["decide", "--round", "20261113_1417", "--slots", "1", "--bids", bf]), 0)
                self.assertIn("記録だけ", buf.getvalue())
                self.assertEqual(A.main(base + ["decide", "--round", "20261113_1417", "--slots", "1", "--bids", bf]), 1)   # 二重決定
                self.assertEqual(A.main(base + ["decide", "--round", "bad", "--slots", "1", "--bids", bf]), 1)
                self.assertEqual(A.main(base + ["decide", "--round", "20261113_1817", "--slots", "1", "--bids", str(Path(t) / "none")]), 1)
                self.assertEqual(A.main(base + ["actual", "--round", "20261113_1417", "--ran", "qa:R-7"]), 0)
                self.assertEqual(A.main(base + ["actual", "--round", "20261113_1417", "--ran", "QA R-7"]), 1)
                self.assertEqual(A.main(base + ["settle", "--round", "20261113_1417", "--dept", "ops", "--realized", "5", "--source", "記録"]), 1)
                self.assertEqual(A.main(base + ["settle", "--round", "20261113_1417", "--dept", "research", "--realized", "600", "--source", "記録の出どころ"]), 0)
                self.assertEqual(A.main(base + ["settle", "--round", "20261113_1417", "--dept", "research", "--realized", "600", "--source", "記録の出どころ"]), 1)
            rep = "\n".join(A.report(A.read_log(log, RU), RU))
            self.assertIn("決定 1回", rep); self.assertIn("一致 0回", rep); self.assertIn("research 0.20", rep)
            self.assertEqual(len(Y.read(led)[0]), 6)                                                  # 入札は帳簿を動かさない

    def test_broken_ledger_stops(self):
        with tempfile.TemporaryDirectory() as t:
            cfg, led, log, bf = setup_dir(t)
            Path(led).write_text("broken\n", encoding="utf-8")
            Path(bf).write_text("[]", encoding="utf-8")
            with quiet():
                self.assertEqual(A.main(["--config", cfg, "decide", "--round", "20261113_1417", "--slots", "1", "--bids", bf]), 1)

    def test_huge_and_tiny_numbers(self):
        for c in [bid("research", "R-1", 100, 10 ** 400), bid("research", "R-1", 10 ** 400, 100), bid("research", "R-1", 100, 2e9),
                  bid("research", "R-1", 1e-300, 1e300), bid("research", "R-1", 9.99, 1000)]:
            out, win = A.decide([c, bid("qa", "B-1", 10, 100)], BAL, TR, 1, RU)
            self.assertFalse(out[0]["eligible"], repr(c)); self.assertEqual(win[0]["dept"], "qa")

    def test_malformed_log_rows(self):
        with tempfile.TemporaryDirectory() as t:
            cfg, led, log, bf = setup_dir(t)
            lg = Path(log)
            for bad in ['5', '[1]', '{"event":"decide","round":"20261113_1200"}', '{"event":"zzz","round":"x"}',
                        '{"event":"settle","round":"20261113_1000","dept":"research","realized_value":"x"}']:
                lg.write_text(bad + "\n", encoding="utf-8")
                with self.assertRaises(A.AuctionError):
                    A.read_log(lg, RU)
                with quiet():
                    self.assertEqual(A.main(["--config", cfg, "report"]), 1)

    def test_parallel_decide_threads(self):
        """同じ回を8並列で decide しても decide 行は1行だけ (判定と追記が同じロックの中)。"""
        with tempfile.TemporaryDirectory() as t:
            cfg, led, log, bf = setup_dir(t)
            Path(bf).write_text(json.dumps([bid("research", "R-11", 100, 3000)]), encoding="utf-8")
            real = Y.Ledger.summary

            def slow(self, *a, **k):
                r = real(self, *a, **k); time.sleep(0.2); return r
            gate, codes = threading.Barrier(8), []

            def run():
                gate.wait()
                codes.append(A.main(["--config", cfg, "decide", "--round", "20261116_0900", "--slots", "1", "--bids", bf]))
            with mock.patch.object(Y.Ledger, "summary", slow), quiet():
                ths = [threading.Thread(target=run) for _ in range(8)]
                for th in ths: th.start()
                for th in ths: th.join(60)
            rows = [r for r in A.read_log(log, RU) if r["event"] == "decide" and r["round"] == "20261116_0900"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(sorted(codes), [0] + [1] * 7)

    def test_parallel_decide_processes(self):
        """別々のプロセス8本で同じ回を decide しても decide 行は1行だけ。"""
        with tempfile.TemporaryDirectory() as t:
            cfg, led, log, bf = setup_dir(t)
            Path(bf).write_text(json.dumps([bid("research", "R-11", 100, 3000)]), encoding="utf-8")
            env = dict(os.environ, PYTHONPATH=str(HERE), PYTHONDONTWRITEBYTECODE="1")
            ps = [subprocess.Popen([sys.executable, "-m", "ee_aidea.auction", "--config", cfg, "decide", "--round", "20261116_0915",
                                    "--slots", "1", "--bids", bf], cwd=t, env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(8)]
            codes = sorted(p.wait(60) for p in ps)
            self.assertEqual(len([r for r in A.read_log(log, RU) if r["event"] == "decide"]), 1)
            self.assertEqual(codes, [0] + [1] * 7)

    def test_broken_utf8_log(self):
        with tempfile.TemporaryDirectory() as t:
            cfg, led, log, bf = setup_dir(t)
            lg = Path(log)
            good = '{"event":"actual","round":"20261113_1417","ran":[]}\n'.encode("utf-8")
            lg.write_bytes(good + b'{"event":"actual","round":"\xff\xfe","ran":[]}\n' + good)
            with self.assertRaises(A.AuctionError) as cm:
                A.read_log(lg, RU)
            self.assertIn("2行目", str(cm.exception))
            Path(bf).write_text("[]", encoding="utf-8")
            for args in (["report"], ["decide", "--round", "20261116_0930", "--slots", "1", "--bids", bf],
                         ["actual", "--round", "20261113_1417"], ["settle", "--round", "20261113_1417", "--dept", "qa", "--realized", "1", "--source", "abc"]):
                with contextlib.redirect_stdout(io.StringIO()) as buf:
                    self.assertEqual(A.main(["--config", cfg] + args), 1)
                self.assertIn("2行目", buf.getvalue())
            self.assertEqual(lg.read_bytes().count(b"\n"), 3)                                         # 何も追記しない

    def test_unicode_separators_in_task(self):
        # task に U+2028 / U+0085 があっても、記録が行で割れて読めなくならない
        with tempfile.TemporaryDirectory() as t:
            cfg, led, log, bf = setup_dir(t)
            b = bid("research", "R-11", 100, 3000); b["task"] = "競合 調査\u0085まとめ "
            Path(bf).write_text(json.dumps([b], ensure_ascii=False), encoding="utf-8")
            base = ["--config", cfg]
            with quiet():
                self.assertEqual(A.main(base + ["decide", "--round", "20261116_1817", "--slots", "1", "--bids", bf]), 0)
            self.assertEqual(len(A.read_log(log, RU)), 1)
            with quiet():
                self.assertEqual(A.main(base + ["actual", "--round", "20261116_1817", "--ran", "research:R-11"]), 0)
                self.assertEqual(A.main(base + ["report"]), 0)
            self.assertEqual(len(A.read_log(log, RU)), 2)


if __name__ == "__main__":
    unittest.main()
