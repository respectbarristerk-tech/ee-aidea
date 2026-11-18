"""ファイルモード (Discord なし) の往復テスト。設定ファイルから CLI を通して動かす。"""
import contextlib, datetime as dt, io, json, shutil, sys, tempfile, unittest
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from ee_aidea import approval_cli as A
from ee_aidea import file_io as F


class FileMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.cfgp = self.write_cfg({})
        shutil.copy(HERE / "levels.example.json", self.d / "levels.json")
        self.assertEqual(self.cli("setup"), 0)

    def tearDown(self):
        self.tmp.cleanup()

    def write_cfg(self, approval):
        cfg = json.loads((HERE / "ee-aidea.example.json").read_text(encoding="utf-8"))
        cfg["approval"].update(approval)
        (self.d / "ee-aidea.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        return str(self.d / "ee-aidea.json")

    def cli(self, *args):
        self.out = io.StringIO()
        with contextlib.redirect_stdout(self.out):
            return A.main(["--config", self.cfgp] + list(args))

    def ai(self):
        return A.make(A.load_ctx(self.cfgp))

    def props(self):
        return {p["level"]: p for p in self.ai().all()}

    def file(self, rel):
        return (self.d / "sandbox" / rel).read_text(encoding="utf-8")

    def outbox(self):
        return (self.d / "data" / "outbox.log").read_text(encoding="utf-8")

    def test_check_after_setup(self):
        self.assertEqual(self.cli("check"), 0, self.out.getvalue())
        self.assertTrue((self.d / "data" / "seal.key").exists())
        self.assertEqual(self.cli("setup"), 0)                    # 2回目は何もしない (鍵を作り直さない)

    def test_round_trip_samples(self):
        self.assertEqual(self.cli("samples"), 0, self.out.getvalue())
        ps = self.props()
        self.assertEqual(sorted(ps), ["green", "red", "yellow"])
        self.assertIn("[黄]", self.outbox())
        self.assertEqual(self.cli("run", "--once"), 0)
        self.assertEqual(self.props()["green"]["status"], "deployed")          # 緑は自動で反映
        self.assertIn("見本", self.file("notes/memo.txt"))
        self.assertEqual(self.cli("reply", ps["yellow"]["diff_sha256"][:4]), 0)
        self.assertEqual(self.props()["yellow"]["status"], "pending")          # reply だけでは反映しない
        self.assertEqual(self.cli("run", "--once"), 0)
        self.assertEqual(self.props()["yellow"]["status"], "deployed")
        self.assertIn("30", self.file("settings/app.json"))
        self.assertIn("反映済み", self.outbox())
        self.cli("reply", ps["red"]["diff_sha256"][:4]); self.cli("run", "--once")
        self.assertEqual(self.props()["red"]["status"], "approved_waiting")    # 赤は待機
        self.cli("reply", "CANCEL"); self.cli("run", "--once")
        self.assertEqual(self.props()["red"]["status"], "cancelled")
        self.assertIn("5", self.file("policy/limits.json"))
        self.assertEqual(self.cli("status"), 0)
        self.assertIn("cancelled", self.out.getvalue())

    def test_stop_and_resume(self):
        self.cli("samples")
        y = self.props()["yellow"]
        self.cli("reply", "STOP"); self.cli("run", "--once")
        ai = self.ai()
        self.assertTrue(ai.stopped())
        code = ai.state["stop"]["resume_hash"][:4]
        self.assertIn(code, self.outbox())                                     # 再開用の4文字は outbox に出る
        self.cli("reply", y["diff_sha256"][:4]); self.cli("run", "--once")
        self.assertEqual(self.props()["yellow"]["status"], "pending")          # 停止中は承認しない
        self.cli("reply", code); self.cli("run", "--once")
        self.assertFalse(self.ai().stopped())
        self.cli("reply", y["diff_sha256"][:4]); self.cli("run", "--once")
        self.assertEqual(self.props()["yellow"]["status"], "deployed")

    def test_missing_or_broken_key(self):
        key = self.d / "data" / "seal.key"
        good = key.read_text(encoding="utf-8")
        key.unlink()
        self.assertEqual(self.cli("status"), 1)
        self.assertIn("鍵ファイルがない", self.out.getvalue())
        self.assertEqual(self.cli("run", "--once"), 1)
        for bad in ["zz\n", good[:20], ""]:
            key.write_text(bad, encoding="utf-8")
            self.assertEqual(self.cli("status"), 1)
            self.assertIn("鍵", self.out.getvalue())
        self.assertEqual(self.cli("check"), 1)
        key.write_text(good, encoding="utf-8")
        self.assertEqual(self.cli("status"), 0)

    def test_stale_and_broken_inbox(self):
        self.cli("samples")
        y = self.props()["yellow"]
        inbox = self.d / "data" / "inbox.txt"
        old = (dt.datetime.now(dt.timezone(dt.timedelta(hours=9))) - dt.timedelta(minutes=30)).isoformat(timespec="seconds")
        with open(inbox, "a", encoding="utf-8") as f:
            f.write("not json\n")
            f.write(json.dumps({"n": 1, "ts": old, "text": y["diff_sha256"][:4]}) + "\n")
        self.cli("run", "--once")
        self.assertEqual(self.props()["yellow"]["status"], "pending")          # 古い承認は処理しない
        self.assertIn("処理しません", self.outbox())
        self.cli("reply", y["diff_sha256"][:4]); self.cli("run", "--once")
        self.assertEqual(self.props()["yellow"]["status"], "deployed")
        self.assertEqual([r["n"] for r in F.read_all(inbox)], [1, 2])

    def test_reply_validation(self):
        for bad in ["a\nb", "x" * 201, "   "]:
            self.assertEqual(self.cli("reply", bad), 1, repr(bad))
        self.assertFalse((self.d / "data" / "inbox.txt").exists())

    def test_new_and_send(self):
        diff = A.sample_diff(self.d / "sandbox", "notes/memo.txt", "書き換え\n")
        (self.d / "c.diff").write_bytes(diff)
        (self.d / "f.json").write_text(json.dumps({"summary": "メモ", "rollback": "戻せる"}, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.cli("new", "--diff", str(self.d / "c.diff"), "--fields", str(self.d / "f.json")), 0)
        pid = self.ai().all()[0]["id"]
        self.cli("run", "--once")
        self.assertEqual(self.ai().load(pid)["status"], "pending")             # 知らせる前は緑も反映しない
        self.assertEqual(self.cli("send", pid), 0)
        self.cli("run", "--once")
        self.assertEqual(self.ai().load(pid)["status"], "deployed")
        self.assertEqual(self.cli("new", "--diff", str(self.d / "none.diff"), "--fields", str(self.d / "f.json")), 1)

    def test_levels_change_needs_pin(self):
        lv = json.loads((self.d / "levels.json").read_text(encoding="utf-8"))
        self.cli("status")                                                     # 今の内容を記録させる
        lv["rules"].append({"pattern": "extra/*", "level": "green"})
        (self.d / "levels.json").write_text(json.dumps(lv), encoding="utf-8")
        self.assertEqual(self.cli("status"), 1)
        self.assertIn("pin-levels", self.out.getvalue())
        self.assertEqual(self.cli("pin-levels"), 0)
        self.assertEqual(self.cli("status"), 0)

    def test_data_inside_deploy_root(self):
        self.cfgp = self.write_cfg({"deploy_root": "work", "data_dir": "work/data"})
        self.assertEqual(self.cli("setup"), 1)

    def test_other_modes_refused(self):
        self.cfgp = self.write_cfg({"mode": "discord"})
        self.assertEqual(self.cli("status"), 1)
        self.assertIn("ファイルモード", self.out.getvalue())


if __name__ == "__main__":
    unittest.main()
