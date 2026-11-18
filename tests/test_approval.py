"""承認係のテスト (ネットなし。git を使う)。使い方: キットのフォルダで  python -m unittest discover -s tests -v"""
import datetime as dt, json, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from ee_aidea import approval as C
from ee_aidea import approval_cli as A

ME = C.LOCAL
KEY = b"k" * 32
LEVELS = json.loads((HERE / "levels.example.json").read_text(encoding="utf-8"))
TZ = dt.timezone(dt.timedelta(hours=9))
Y = {"summary": "件数 20→30", "rollback": "戻せる", "risk": "表示が崩れる", "reviewer_objection": "なし"}
R = {"summary": "上限 5→3", "rollback": "戻せる", "risk": "-", "reviewer_objection": "なし", "expected_effect": "処理を小さくできる"}
APP20, APP30 = '{\n "page_size": 20\n}\n', '{\n "page_size": 30\n}\n'
LIM5, LIM3 = '{\n "max_items_per_run": 5\n}\n', '{\n "max_items_per_run": 3\n}\n'


class Clock:
    def __init__(self):
        self.t = dt.datetime(2026, 11, 12, 12, 0, tzinfo=TZ)

    def __call__(self):
        return self.t

    def add(self, **kw):
        self.t += dt.timedelta(**kw)


def g(root, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false"] + list(a),
                          cwd=str(root), capture_output=True, timeout=60)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "sandbox"
        self.root.mkdir()
        for rel, text in list(A.SANDBOX.items()) + [("other/x.txt", "x\n")]:
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(text, encoding="utf-8", newline="\n")
        g(self.root, "init"); g(self.root, "add", "-A"); g(self.root, "commit", "-m", "init")
        self.clock = Clock()
        self.cfg = {"deploy_root": str(self.root), "wait_hours_red": 24, "expire_days": 7}
        self.ai = self.newai()
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def newai(self, key=KEY, data="data", **kw):
        return C.Approval(Path(self.tmp.name) / data, self.cfg, kw.pop("levels", LEVELS), key, now_fn=self.clock, **kw)

    def diff(self, rel, text):
        return A.sample_diff(self.root, rel, text)

    def prop(self, rel, text, fields, sent=True):
        p = self.ai.new_proposal(self.diff(rel, text), fields)
        if sent:
            self.ai.mark_sent(p["id"])
        return self.ai.load(p["id"])

    def msg(self, text, author=ME, channel=ME, ts=None, ai=None):
        self.n += 1
        return (ai or self.ai).handle({"id": str(1000 + self.n), "author_id": author, "channel_id": channel, "content": text,
                                       "ts": (ts or self.clock.t).isoformat()})

    say = msg

    def file(self, rel):
        return (self.root / rel).read_text(encoding="utf-8")

    def status(self, p):
        return self.ai.load(p["id"])["status"]

    def yellow(self, sent=True):
        return self.prop("settings/app.json", APP30, Y, sent)

    def red(self):
        return self.prop("policy/limits.json", LIM3, R)


class TestApproval(Base):
    def test_yellow_approved(self):
        p = self.yellow()
        self.assertEqual(p["level"], "yellow")
        out = self.say(p["diff_sha256"][:4].upper())      # 大文字でも可
        self.assertIn("反映済み", out[0])
        self.assertEqual(self.file("settings/app.json"), APP30)
        self.assertEqual(self.status(p), "deployed")
        self.assertIn(p["id"], g(self.root, "log", "-1", "--format=%s").stdout.decode())

    def test_other_sender_is_ignored(self):
        p = self.yellow()
        self.assertEqual(self.say(p["diff_sha256"][:4], author="someone"), [])
        self.assertEqual(self.say(p["diff_sha256"][:4], channel="elsewhere"), [])
        self.assertEqual(self.status(p), "pending")
        self.assertEqual(self.file("settings/app.json"), APP20)
        self.assertIn("ignored_other_id", (self.ai.data / "audit.log").read_text(encoding="utf-8"))

    def test_one_char_off_does_nothing(self):
        p = self.yellow()
        c = p["diff_sha256"][:4]
        wrong = c[:3] + ("0" if c[3] != "0" else "1")
        self.assertIn("該当なし", self.say(wrong)[0])
        self.assertEqual(self.status(p), "pending")

    def test_tampered_diff_is_rejected(self):
        p = self.yellow()
        f = self.ai.pdir(p["id"]) / "change.diff"
        f.write_bytes(f.read_bytes().replace(b"30", b"99"))
        self.assertIn("差分が変更されています", self.say(p["diff_sha256"][:4])[0])
        self.assertEqual(self.status(p), "rejected")
        self.assertEqual(self.file("settings/app.json"), APP20)

    def test_unsent_cannot_be_approved(self):
        p = self.yellow(sent=False)
        self.assertIn("該当なし", self.say(p["diff_sha256"][:4])[0])

    def test_same_message_not_twice(self):
        p = self.yellow()
        m = {"id": "5", "author_id": ME, "channel_id": ME, "content": p["diff_sha256"][:4]}
        self.assertTrue(self.ai.handle(m))
        self.assertEqual(self.ai.handle(m), [])

    def test_ambiguous_prefix_needs_id(self):
        p1 = self.yellow()
        p2 = self.prop("other/x.txt", "y\n", Y)
        q = self.ai.load(p2["id"]); q["diff_sha256"] = p1["diff_sha256"][:4] + q["diff_sha256"][4:]; self.ai.store(q)
        self.assertIn("候補が複数", self.say(p1["diff_sha256"][:4])[0])
        self.assertEqual((self.status(p1), self.status(p2)), ("pending", "pending"))
        self.assertIn("反映済み", self.say("%s %s" % (p1["id"], p1["diff_sha256"][:4]))[0])

    def test_expired_proposal(self):
        p = self.yellow()
        self.clock.add(days=7, minutes=1)
        self.assertIn("期限切れ", self.say(p["diff_sha256"][:4])[0])
        p2 = self.prop("other/x.txt", "y\n", Y)
        self.clock.add(days=8)
        self.assertTrue(any("期限切れ" in x for x in self.ai.tick()))
        self.assertEqual(self.status(p2), "expired")

    def test_chat_is_ignored(self):
        self.assertEqual(self.say("こんにちは"), [])
        self.assertEqual(self.say("abcd efgh"), [])

    def test_bad_post_check_undone(self):
        p = self.prop("settings/app.json", '{\n "page_size": 30,\n}\n', Y)   # 壊れたJSON
        self.assertIn("元に戻しました", self.say(p["diff_sha256"][:4])[0])
        self.assertEqual(self.file("settings/app.json"), APP20)
        self.assertEqual(g(self.root, "status", "--porcelain").stdout.strip(), b"")

    def test_diff_no_longer_applies(self):
        p = self.yellow()
        (self.root / "settings/app.json").write_text('{\n "page_size": 55\n}\n', encoding="utf-8", newline="\n")
        g(self.root, "commit", "-am", "manual")
        self.assertIn("何もしていません", self.say(p["diff_sha256"][:4])[0])
        self.assertIn("55", self.file("settings/app.json"))


class TestLevels(Base):
    def test_unlisted_yellow_levels_red(self):
        self.assertEqual(C.classify(["other/x.txt"], LEVELS), "yellow")
        self.assertEqual(C.classify(["levels.json"], LEVELS), "red")
        self.assertEqual(C.classify(["notes/a.txt", "policy/l.json"], LEVELS), "red")
        self.assertEqual(C.classify(["notes/a.txt", "docs/b.md"], LEVELS), "green")

    def test_required_fields(self):
        d = self.diff("policy/limits.json", LIM3)
        with self.assertRaises(C.PacketError):                       # 赤で期待される効果が空
            self.ai.new_proposal(d, dict(R, expected_effect=""))
        dy = self.diff("settings/app.json", APP30)
        with self.assertRaises(C.PacketError):                       # 黄で risk なし
            self.ai.new_proposal(dy, {k: v for k, v in Y.items() if k != "risk"})
        with self.assertRaises(C.PacketError):                       # 黄と赤が混ざれば赤の項目が要る
            self.ai.new_proposal(d + dy, Y)
        p = self.ai.new_proposal(d + dy, R)
        self.assertEqual(p["level"], "red")
        body = self.ai.render(p)[1]
        self.assertIn("期待される効果", body); self.assertIn("処理を小さくできる", body)
        self.assertLess(body.index("最悪の影響"), body.index("期待される効果"))   # required_fields の順に出す

    def test_custom_required(self):
        lv = json.loads(json.dumps(LEVELS)); lv["required_fields"]["green"] = ["summary", "rollback", "ticket"]
        ai = self.newai(data="d2", levels=lv)
        with self.assertRaises(C.PacketError):
            ai.new_proposal(self.diff("notes/memo.txt", "a\n"), {"summary": "メモ", "rollback": "戻せる"})
        p = ai.new_proposal(self.diff("notes/memo.txt", "a\n"), {"summary": "メモ", "rollback": "戻せる", "ticket": "T-1"})
        self.assertIn("ticket", ai.render(p)[1])

    def test_paths_outside_root(self):
        for bad in ["--- a/../x\n+++ b/../x\n", "--- a/.git/config\n+++ b/.git/config\n", "+++ /etc/passwd\n", "+++ b/C:/x\n", "hello\n"]:
            with self.assertRaises(C.PacketError):
                C.diff_files(bad)

    def test_green_auto_only_after_sent(self):
        p = self.prop("notes/memo.txt", "a\nb\n", {"summary": "メモ", "rollback": "戻せる"}, sent=False)
        self.assertEqual(self.ai.tick(), [])
        self.ai.mark_sent(p["id"])
        self.assertIn("緑・自動", self.ai.tick()[0])
        self.assertEqual(self.file("notes/memo.txt"), "a\nb\n")

    def test_level_change_invalidates(self):
        p = self.prop("notes/memo.txt", "a\nb\n", {"summary": "メモ", "rollback": "戻せる"})
        self.ai.levels = dict(LEVELS, rules=[])                      # 緑の決まりが消えた → 黄になるはず
        self.assertIn("色の判定", self.ai.tick()[0])
        self.assertEqual(self.status(p), "rejected")


class TestRedAndStop(Base):
    def test_red_waits_then_deploys(self):
        p = self.red()
        self.assertIn("待機中", self.say(p["diff_sha256"][:4])[0])
        self.assertEqual(self.file("policy/limits.json"), LIM5)
        self.clock.add(hours=23, minutes=59)
        self.assertEqual(self.ai.tick(), [])
        self.assertEqual(self.file("policy/limits.json"), LIM5)
        self.clock.add(minutes=2)
        self.assertIn("待機が終わりました", self.ai.tick()[0])
        self.assertEqual(self.file("policy/limits.json"), LIM3)

    def test_cancel_during_wait(self):
        p = self.red()
        self.say(p["diff_sha256"][:4])
        self.assertIn("破棄", self.say("cancel")[0])
        self.clock.add(hours=25)
        self.assertEqual(self.ai.tick(), [])
        self.assertEqual(self.file("policy/limits.json"), LIM5)
        self.assertIn("対象の提案がありません", self.say("CANCEL")[0])

    def test_second_approval(self):
        p = self.red()
        self.say(p["diff_sha256"][:4])
        self.assertIn("該当なし", self.say(p["diff_sha256"][:4])[0])
        self.assertEqual(self.file("policy/limits.json"), LIM5)

    def test_stop_and_resume_code(self):
        p = self.yellow()
        r = self.red(); self.say(r["diff_sha256"][:4])
        out = self.say("stop")[0]
        self.assertIn("ほかのプログラムは止まりません", out)
        self.assertTrue(self.ai.stop_flag.exists())
        code = self.ai.state["stop"]["resume_hash"][:4]
        self.assertIn("すでに停止中", self.say("STOP")[0])            # STOP では再開しない
        self.assertTrue(self.ai.stop_flag.exists())
        self.assertIn("停止中", self.say(p["diff_sha256"][:4])[0])    # 停止中は承認を受け付けない
        self.clock.add(hours=25)
        self.assertEqual(self.ai.tick(), [])                          # 待機が終わっても反映しない
        self.assertEqual(self.say(code, author="someone"), [])        # 他人の再開コードは無視
        self.assertTrue(self.ai.stop_flag.exists())
        out = self.say(code)[0]
        self.assertIn("再開しました", out)
        self.assertIn(r["id"], out)                                   # 停止をまたいだ赤は承認前に戻る
        self.assertFalse(self.ai.stop_flag.exists())
        self.assertEqual(self.ai.tick(), [])
        self.assertEqual(self.status(r), "pending")
        self.assertEqual(self.file("policy/limits.json"), LIM5)

    def test_state_survives_restart(self):
        p = self.red(); self.say(p["diff_sha256"][:4]); self.say("STOP")
        ai2 = self.newai()
        self.assertTrue(ai2.stopped())
        self.assertEqual(ai2.load(p["id"])["status"], "approved_waiting")
        self.assertEqual(ai2.handle({"id": "1001", "author_id": ME, "channel_id": ME, "content": "x"}), [])  # 処理済み


GITHDR = "diff --git a/%s b/%s\nindex 1111111..2222222 100644\n--- a/%s\n+++ b/%s\n"


class TestFormatAndTamper(Base):
    """差分の形・記録の書き換え・壊れた記録への対応。"""

    def test_non_git_format_rejected(self):
        bad = [
            "--- notes/policy/limits.json\n+++ notes/policy/limits.json\n@@ -1 +1 @@\n-a\n+b\n",          # a/ b/ なし
            "--- x/policy/limits.json\n+++ x/policy/limits.json\n@@ -1 +1 @@\n-a\n+b\n",
            "diff --git a/notes/memo.txt b/policy/limits.json\n--- a/notes/memo.txt\n+++ b/policy/limits.json\n@@ -1 +1 @@\n-a\n+b\n",
            "diff --git a/notes/memo.txt b/notes/memo.txt\nsimilarity index 100%\nrename from policy/limits.json\nrename to notes/l.json\n",
            "diff --git a/keys/k b/keys/k\nnew file mode 100644\nindex 0000000..e69de29\n",                 # 空の新規ファイル
            "diff --git a/notes/b.bin b/notes/b.bin\nnew file mode 100644\nindex 0000000..1111111\nGIT binary patch\nliteral 1\n",
            "diff --git a/notes/ln b/notes/ln\nnew file mode 120000\nindex 0000000..1111111\n--- /dev/null\n+++ b/notes/ln\n@@ -0,0 +1 @@\n+../policy\n",
            "diff --git a/notes/memo.txt b/notes/memo.txt\nold mode 100644\nnew mode 100755\n",
            'diff --git "a/policy/\\346\\227\\245.json" "b/policy/\\346\\227\\245.json"\n',
            GITHDR % (("notes/a b.txt",) * 4) + "@@ -1 +1 @@\n-a\n+b\n",
            GITHDR % (("notes/memo.txt",) * 4) + "@@ -1 +1 @@\n-a\n+b\n\r",
        ]
        for d in bad:
            with self.assertRaises(C.PacketError, msg=d[:50]):
                C.diff_files(d)

    def test_hunk_count_hides_nothing(self):
        hidden = (GITHDR % (("notes/memo.txt",) * 4) + "@@ -1 +1 @@\n-練習用のメモ\n+x\n"
                  "--- a/policy/limits.json\n+++ b/policy/limits.json\n@@ -2 +2 @@\n- \"max_items_per_run\": 5\n+ \"max_items_per_run\": 99\n")
        with self.assertRaises(C.PacketError):
            C.diff_files(hidden)
        with self.assertRaises(C.PacketError):
            self.ai.new_proposal(hidden.encode(), {"summary": "メモの変更", "rollback": "戻せる"})
        self.assertEqual(self.file("policy/limits.json"), LIM5)

    def test_added_file_deployed(self):
        add = "diff --git a/policy/new.json b/policy/new.json\nnew file mode 100644\nindex 0000000..1111111\n--- /dev/null\n+++ b/policy/new.json\n@@ -0,0 +1 @@\n+{}\n"
        self.assertEqual(C.classify(C.diff_files(add), LEVELS), "red")
        p = self.ai.new_proposal(add.encode(), R); self.ai.mark_sent(p["id"])
        self.say(p["diff_sha256"][:4]); self.clock.add(hours=24, minutes=1)
        self.assertIn("反映済み", self.ai.tick()[0])
        self.assertTrue((self.root / "policy/new.json").exists())

    def test_case_insensitive_levels(self):
        self.assertEqual(C.level_of("POLICY/x.json", LEVELS), "red")
        self.assertEqual(C.level_of("Notes/Policy/x", LEVELS), "green")   # notes 配下は緑のまま（中身は別フォルダ）

    def test_tampered_packet(self):
        for edit in [lambda q: q.update(status="approved_waiting", wait_until="2020-01-01T00:00:00+09:00"),
                     lambda q: q.update(status="deployed"), lambda q: q.update(sent=True, level="green"),
                     lambda q: q.update(expires_at="2099-01-01T00:00:00+09:00")]:
            p = self.red()
            f = self.ai.pdir(p["id"]) / "packet.json"
            q = json.loads(f.read_text(encoding="utf-8")); edit(q)
            f.write_text(json.dumps(q), encoding="utf-8")
            notes = self.ai.tick()
            self.assertTrue(any("無効" in x for x in notes), notes)
            self.assertIn("該当なし", self.say(p["diff_sha256"][:4])[0])
            self.assertEqual(self.file("policy/limits.json"), LIM5)
            f.unlink()

    def test_swapped_diff_and_hash(self):
        p = self.yellow()
        other = self.diff("settings/app.json", '{\n "page_size": 55190\n}\n')
        (self.ai.pdir(p["id"]) / "change.diff").write_bytes(other)
        f = self.ai.pdir(p["id"]) / "packet.json"
        q = json.loads(f.read_text(encoding="utf-8"))
        q["diff_sha256"] = p["diff_sha256"][:4] + C.sha256_bytes(other)[4:]
        f.write_text(json.dumps(q), encoding="utf-8")
        self.assertIn("該当なし", self.say(p["diff_sha256"][:4])[0])
        self.assertEqual(self.file("settings/app.json"), APP20)

    def test_wrong_key_cannot_forge(self):
        p = self.yellow(sent=False)
        self.say("hello")                                             # 状態を一度保存させる
        other = self.newai(key=b"x" * 32)                              # 鍵を知らない者
        self.assertTrue(other.stopped())                              # 状態の封印が合わず停止側へ
        with self.assertRaises(C.PacketError):
            other.load(p["id"])

    def test_broken_packet(self):
        p = self.yellow()
        bad = self.ai.data / "proposals" / "P-20261112-77"; bad.mkdir()
        (bad / "packet.json").write_text("{bad", encoding="utf-8")
        self.assertTrue(any("P-20261112-77" in x for x in self.ai.tick()))
        self.assertEqual(self.ai.tick(), [])                           # 知らせるのは1回だけ
        self.assertIn("反映済み", self.say(p["diff_sha256"][:4])[0])
        self.assertIn("STOP を受け付けました", self.say("STOP")[0])

    def test_flag_without_state(self):
        self.say("STOP")
        self.ai.state_path.unlink()
        ai2 = self.newai()
        self.assertTrue(ai2.stopped())
        out = self.msg("STOP", ai=ai2)[0]
        self.assertIn("STOP を受け付けました", out)
        code = ai2.state["stop"]["resume_hash"][:4]
        self.assertIn("再開しました", self.msg(code, ai=ai2)[0])

    def test_state_without_flag(self):
        p = self.yellow()
        self.say("STOP"); self.ai.stop_flag.unlink()
        self.assertIn("停止中", self.say(p["diff_sha256"][:4])[0])
        self.ai.tick(); self.assertTrue(self.ai.stop_flag.exists())

    def test_tampered_state_forces_stop(self):
        self.say("hello")
        st = json.loads(self.ai.state_path.read_text(encoding="utf-8")); st["processed"] = None
        self.ai.state_path.write_text(json.dumps(st), encoding="utf-8")
        ai2 = self.newai()
        self.assertTrue(ai2.stopped())
        self.assertTrue(any("state.json" in x for x in ai2.tick()))

    def test_bad_levels_refuse_to_start(self):
        rf = LEVELS["required_fields"]
        for lv in [{"rules": [{"pattern": "x/*", "level": "blue"}]}, {"rules": "x"}, dict(LEVELS, rules=[]),
                   {k: v for k, v in LEVELS.items() if k != "required_fields"},
                   dict(LEVELS, required_fields=dict(rf, green=["rollback"])),             # summary がない
                   dict(LEVELS, required_fields=dict(rf, red=["summary", "rollback", "Bad Name"])),
                   dict(LEVELS, required_fields={"green": rf["green"]}),
                   dict(LEVELS, field_labels=[])]:
            with self.assertRaises(C.ConfigError, msg=repr(lv)[:80]):
                self.newai(levels=lv)
        with self.assertRaises(C.ConfigError):
            self.newai(key=b"")
        ok = self.newai(levels_sha="aaa")
        self.assertEqual(ok.state["levels_sha"], "aaa")
        with self.assertRaises(C.ConfigError):                         # levels.json が変わったら、確認まで動かない
            self.newai(levels_sha="bbb")

    def test_blank_fields_rejected(self):
        d = self.diff("policy/limits.json", LIM3)
        for v in [" ", "\n", "なし", "null", "0", 0, False, None, "-"]:
            with self.assertRaises(C.PacketError, msg=repr(v)):
                self.ai.new_proposal(d, dict(R, expected_effect=v))
        with self.assertRaises(C.PacketError):
            self.ai.new_proposal(d, dict(R, reviewer_objection=" "))
        self.ai.new_proposal(d, dict(R, reviewer_objection="なし"))    # 反対意見の「なし」は明記として可

    def test_red_approved_too_late(self):
        p = self.red()
        self.clock.add(days=6, hours=1)
        self.assertIn("承認できません", self.say(p["diff_sha256"][:4])[0])
        self.assertEqual(self.status(p), "pending")

    def test_parent_git_repo_not_used(self):
        sub = self.root / "inner"; sub.mkdir()
        self.cfg = dict(self.cfg, deploy_root=str(sub))
        ai = self.newai(data="d2")
        with self.assertRaises(C.PacketError):
            ai.new_proposal(self.diff("settings/app.json", APP30), Y)

    def test_handle_error_replies(self):
        p = self.yellow()
        self.ai.deploy = lambda p: (_ for _ in ()).throw(FileNotFoundError("x"))
        self.assertIn("エラー", self.say(p["diff_sha256"][:4])[0])
        self.assertEqual(self.status(p), "pending")

    def test_replay_of_cancelled_red(self):
        p = self.red()
        self.say(p["diff_sha256"][:4])
        f = self.ai.pdir(p["id"]) / "packet.json"
        saved = f.read_bytes()                       # 待機中の有効な記録を取っておく
        self.say("CANCEL")
        f.write_bytes(saved)                         # 破棄のあとで戻す
        self.clock.add(hours=25)
        notes = self.ai.tick()
        self.assertTrue(any("無効" in x for x in notes), notes)
        self.assertEqual(self.file("policy/limits.json"), LIM5)

    def test_replay_of_deployed_yellow(self):
        p = self.yellow()
        f = self.ai.pdir(p["id"]) / "packet.json"
        saved = f.read_bytes()
        self.say(p["diff_sha256"][:4])
        f.write_bytes(saved)
        self.assertIn("該当なし", self.say(p["diff_sha256"][:4])[0])

    def test_missing_state(self):
        p = self.yellow()
        self.say("STOP")
        self.ai.state_path.unlink(); self.ai.stop_flag.unlink()
        ai2 = self.newai()
        self.assertTrue(ai2.stopped())
        self.assertTrue(any("state.json" in x for x in ai2.tick()))
        self.assertIn("停止中", self.msg(p["diff_sha256"][:4], ai=ai2)[0])

    def test_stale_replies_ignored(self):
        self.ai.cfg["max_message_age_minutes"] = 15
        p = self.yellow()
        out = self.say(p["diff_sha256"][:4], ts=self.clock.t - dt.timedelta(minutes=16))
        self.assertIn("処理しません", out[0])
        self.assertEqual(self.status(p), "pending")
        out = self.ai.handle({"id": "x1", "author_id": ME, "channel_id": ME, "content": p["diff_sha256"][:4], "ts": "bad"})
        self.assertIn("処理しません", out[0])                                   # 時刻を読めない返事も処理しない
        q = self.red()
        self.assertIn("破棄しました", self.say("CANCEL", ts=self.clock.t - dt.timedelta(days=1))[0])   # CANCEL は古くても通す
        self.assertEqual(self.status(q), "cancelled")
        self.assertIn("反映済み", self.say(p["diff_sha256"][:4], ts=self.clock.t - dt.timedelta(minutes=1))[0])
        self.assertIn("STOP を受け付けました", self.say("STOP", ts=self.clock.t - dt.timedelta(days=1))[0])

    def test_gitignored_paths_rejected(self):
        (self.root / ".gitignore").write_text("notes/private*\n", encoding="utf-8", newline="\n")
        g(self.root, "add", "-A"); g(self.root, "commit", "-m", "ignore")
        add = "diff --git a/notes/private.txt b/notes/private.txt\nnew file mode 100644\nindex 0000000..1111111\n--- /dev/null\n+++ b/notes/private.txt\n@@ -0,0 +1 @@\n+x\n"
        with self.assertRaises(C.PacketError):
            self.ai.new_proposal(add.encode(), {"summary": "メモの追加", "rollback": "戻せる"})

    def test_reserved_names_rejected(self):
        for name in ["notes/CON", "notes/con.txt", "notes/LPT1.json", "aux/x.txt"]:
            with self.assertRaises(C.PacketError, msg=name):
                C.check_path(name)
        for lv in [{"rules": []}, {"rules": [], "must_be_red": []}]:
            with self.assertRaises(C.ConfigError):
                C.check_levels(lv)


class TestRollback(Base):
    """巻き戻し対策 (通し番号)。常駐中の巻き戻しと、古い記録の差し戻し。"""

    def red_waiting(self):
        p = self.red()
        self.say(p["diff_sha256"][:4])
        return p

    def snapshot(self):
        dst = Path(self.tmp.name) / ("snap%d" % self.n); self.n += 1
        shutil.copytree(self.ai.data, dst)
        return dst

    def restore(self, snap):
        shutil.rmtree(self.ai.data); shutil.copytree(snap, self.ai.data)

    def test_rollback_while_running(self):
        self.red_waiting()
        snap = self.snapshot()
        self.say("CANCEL")
        self.restore(snap)                                     # 動いている最中に戻された
        self.ai.reload()
        self.assertTrue(self.ai.stopped())
        self.clock.add(hours=25)
        self.assertTrue(any("巻き戻し" in x for x in self.ai.tick()))
        self.assertEqual(self.file("policy/limits.json"), LIM5)

    def test_state_and_flag_deleted(self):
        self.red_waiting(); self.say("STOP")
        self.ai.state_path.unlink(); self.ai.stop_flag.unlink()
        self.ai.reload()
        self.assertTrue(self.ai.stopped())
        self.clock.add(hours=25); self.ai.tick()
        self.assertEqual(self.file("policy/limits.json"), LIM5)

    def test_old_packet_version_invalid(self):
        p = self.red_waiting()
        f = self.ai.pdir(p["id"]) / "packet.json"
        old = f.read_bytes()                                   # 1回目の待機中の記録
        self.say("STOP")
        self.say(self.ai.state["stop"]["resume_hash"][:4])     # 再開 → 赤は承認前に戻る
        self.assertEqual(self.status(p), "pending")
        f.write_bytes(old)                                     # 古い「待機中」に戻す
        self.clock.add(hours=25)
        self.assertTrue(any("無効" in x for x in self.ai.tick()))
        self.assertEqual(self.file("policy/limits.json"), LIM5)

    def test_reload_sees_other_process(self):
        other = self.newai()
        p = other.new_proposal(self.diff("settings/app.json", APP30), Y); other.mark_sent(p["id"])
        self.ai.reload()
        self.assertFalse(self.ai.stopped())
        self.assertIn("反映済み", self.say(p["diff_sha256"][:4])[0])


if __name__ == "__main__":
    unittest.main()
