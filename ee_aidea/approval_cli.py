"""承認係の起動用 (ファイルモード)。ee-aidea.json のあるフォルダで:
  python -m ee_aidea.approval_cli check               設定・git・練習用フォルダ・鍵ファイルを確認
  python -m ee_aidea.approval_cli setup               練習用フォルダ (deploy_root) と封印用の鍵ファイルを作る
  python -m ee_aidea.approval_cli samples             見本の提案を3件（緑・黄・赤）作って知らせる
  python -m ee_aidea.approval_cli new --diff 変更.diff --fields 項目.json [--send]
  python -m ee_aidea.approval_cli send P-YYYYMMDD-NN  提案を知らせる (画面と outbox)。知らせたものだけ承認・自動反映の対象
  python -m ee_aidea.approval_cli status              提案の一覧
  python -m ee_aidea.approval_cli reply <返事>        人間の返事を inbox に書く (4文字 / CANCEL / STOP)
  python -m ee_aidea.approval_cli run [--once]        常駐: poll_seconds ごとに inbox を読み、照合・反映する
  python -m ee_aidea.approval_cli pin-levels          levels.json を変えたあと、内容を確認してから実行（新しい内容を正として記録）
"""
import argparse, datetime as dt, hashlib, json, subprocess, sys, time
from pathlib import Path

from . import approval as C
from . import config as CF
from . import file_io as F


class Ctx:
    """設定から作る、承認係の実行に必要なものの組。"""

    def __init__(self, cfg):
        ap = cfg["approval"]
        self.cfg = cfg
        self.data = Path(ap["data_dir"])
        self.levels_path = Path(ap["levels"])
        self.inbox, self.outbox_path = ap["file"]["inbox"], ap["file"]["outbox"]
        self.key_path = self.data / "seal.key"
        self.ledger_path = cfg["ledger"]["path"]
        self.acfg = {"deploy_root": ap["deploy_root"], "wait_hours_red": ap["wait_hours_red"],
                     "expire_days": ap["expire_days"], "max_message_age_minutes": ap["max_message_age_minutes"],
                     "poll_seconds": ap["poll_seconds"], "unit": cfg["unit"], "utc_offset_hours": cfg["utc_offset_hours"],
                     "owner_id": C.LOCAL, "channel_id": C.LOCAL}
        self.tz = dt.timezone(dt.timedelta(hours=cfg["utc_offset_hours"]))

    def now(self):
        return dt.datetime.now(self.tz)

    def outbox(self):
        return F.Outbox(self.outbox_path, self.now)


def load_ctx(path):
    return Ctx(CF.load(path))


def read_levels(ctx):
    try:
        raw = ctx.levels_path.read_bytes()
        return raw, json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        raise C.ConfigError("levels.json がない（levels.example.json をコピーして作る）")
    except (ValueError, UnicodeDecodeError):
        raise C.ConfigError("levels.json を JSON として読めない")


def make(ctx, pin=True):
    raw, levels = read_levels(ctx)
    return C.Approval(ctx.data, ctx.acfg, levels, C.load_seal_key(ctx.key_path), now_fn=ctx.now,
                      ledger_path=ctx.ledger_path, levels_sha=hashlib.sha256(raw).hexdigest() if pin else None)


def lock(ctx):
    ctx.data.mkdir(parents=True, exist_ok=True)
    return C.locked(ctx.data / "approval")


def git_ok(root):
    """練習用フォルダそのものに .git があり、履歴があること（親フォルダの git は数えない）。"""
    try:
        return (Path(root) / ".git").exists() and \
            subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, timeout=30).returncode == 0
    except Exception:
        return False


def _inside(child, parent):
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def cmd_check(ctx):
    ok = True

    def line(name, good, note=""):
        nonlocal ok
        ok = ok and good
        print("[%s] %s%s" % ("OK" if good else "NG", name, ("  " + note) if note else ""))
    root = ctx.acfg["deploy_root"]
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=30, check=True)
        line("git が使える", True)
    except Exception:
        line("git が使える", False)
    line("練習用フォルダ (deploy_root) が準備済み", git_ok(root), "まだなら setup を実行")
    line("記録のフォルダ (data_dir) が練習用フォルダの外にある", not _inside(ctx.data, root))
    try:
        C.check_levels(read_levels(ctx)[1])
        line("levels.json（場所→色の一覧）が正しい", True)
    except C.ConfigError as e:
        line("levels.json（場所→色の一覧）が正しい", False, str(e))
    try:
        C.load_seal_key(ctx.key_path)
        line("封印用の鍵ファイルがある", True)
    except C.ConfigError as e:
        line("封印用の鍵ファイルがある", False, str(e))
    print("\n結果: " + ("すべてOK" if ok else "NG があります"))
    return 0 if ok else 1


SANDBOX = {"notes/memo.txt": "練習用のメモ\n",
           "settings/app.json": '{\n "page_size": 20\n}\n',
           "policy/limits.json": '{\n "max_items_per_run": 5\n}\n'}
SAMPLES = [
    ("notes/memo.txt", "練習用のメモ\n見本: 表示だけの変更（緑）\n",
     {"summary": "見本（緑）: メモに1行足す", "rollback": "戻せる。前版に戻す"}),
    ("settings/app.json", '{\n "page_size": 30\n}\n',
     {"summary": "見本（黄）: 1ページの件数 20→30", "rollback": "戻せる。前版に戻す",
      "risk": "見本のため影響なし（練習用フォルダのみ）", "reviewer_objection": "見本のため未実施"}),
    ("policy/limits.json", '{\n "max_items_per_run": 3\n}\n',
     {"summary": "見本（赤）: 1回に処理する件数の上限 5→3", "rollback": "戻せる。前版に戻す",
      "risk": "見本のため影響なし（練習用フォルダのみ）", "reviewer_objection": "見本のため未実施",
      "expected_effect": "見本: 1回の処理が小さくなり、途中で止めやすくなる"}),
]


def cmd_setup(ctx):
    root = Path(ctx.acfg["deploy_root"])
    if _inside(ctx.data, root):
        print("記録のフォルダ (data_dir) を練習用フォルダ (deploy_root) の中に置かないでください。")
        return 1
    if C.make_seal_key(ctx.key_path):
        print("封印用の鍵ファイルを作りました: %s（他人に渡さない。消すと今までの記録は無効になる）" % ctx.key_path.name)
    if git_ok(root):
        print("練習用フォルダは準備済みです: %s" % root.name)
        return 0
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in SANDBOX.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8", newline="\n")

    def g(*a):
        return subprocess.run(["git", "-c", "user.name=ee-aidea", "-c", "user.email=ee-aidea@localhost"] + list(a),
                              cwd=str(root), capture_output=True, timeout=60)
    steps = [g("init"), g("config", "core.autocrlf", "false"), g("add", "-A"), g("commit", "-m", "sandbox init")]
    if any(s.returncode for s in steps):
        print("練習用フォルダの準備に失敗しました（git）。")
        return 1
    print("練習用フォルダを作りました: %s" % root.name)
    return 0


def sample_diff(root, rel, new_text):
    """練習用フォルダの中で実際に書き換えて差分を取り、元に戻す。"""
    fp = Path(root) / rel
    old = fp.read_bytes()
    try:
        fp.write_text(new_text, encoding="utf-8", newline="\n")
        r = subprocess.run(["git", "-c", "core.autocrlf=false", "diff", "--", rel], cwd=str(root), capture_output=True, timeout=60)
        return r.stdout
    finally:
        fp.write_bytes(old)


def send(ai, out, pid):
    p = ai.load(pid)
    title, text = ai.render(p)
    ai.mark_sent(pid)
    if out.post(text, title=title):
        return True
    q = ai.load(pid); q["sent"] = False; ai.store(q)      # 出せなかったので「未送信」に戻す
    print("知らせを出せませんでした（%s）: %s" % (out.last_error, pid))
    return False


def cmd_samples(ctx):
    if not git_ok(ctx.acfg["deploy_root"]):
        print("先に setup を実行してください。")
        return 1
    out, rc = ctx.outbox(), 0
    for rel, new_text, fields in SAMPLES:
        with lock(ctx):
            ai = make(ctx)                  # 状態は必ずロックの中で読む（常駐側の記録を古い内容で上書きしないため）
            diff = sample_diff(ctx.acfg["deploy_root"], rel, new_text)
            if not diff.strip():
                print("見本の差分を作れません（既に反映済みの可能性）: %s" % rel)
                rc = 1
                continue
            try:
                p = ai.new_proposal(diff, fields)
            except C.PacketError as e:
                print("作成できません: %s" % e)
                rc = 1
                continue
            rc = rc if send(ai, out, p["id"]) else 1
    return rc


def cmd_reply(ctx, text):
    n, ts = F.append_inbox(ctx.inbox, text, ctx.now())
    print("返事を記録しました (#%d)。承認係 (run) が次の回に処理します。" % n)
    return 0


def cmd_run(ctx, once, sleep=time.sleep):
    out = ctx.outbox()
    with lock(ctx):
        ai = make(ctx)
    queue = []                              # まだ outbox に出せていない文。出せるまで毎回やり直す

    def say(text):
        queue.append(text)
        del queue[:-50]

    def flush():
        while queue:
            if not out.post(queue[0]):
                ai.audit("post_failed", detail=out.last_error)
                return False
            queue.pop(0)
        return True

    if not once:
        say("承認係が起動しました。返事を %s秒ごとに確認します。止めるときはこの画面で Ctrl+C。" % ctx.acfg["poll_seconds"])
    while True:
        caught_up = False                   # この回、返事を最後まで読めたか
        try:
            with lock(ctx):
                ai.reload()                 # 別の処理が書いた状態を読み直す。番号が逆戻りしていたら巻き戻しとして止める
                flush()
                msgs = F.read_new(ctx.inbox, ai.state.get("last_message_id"))
                if msgs is None:
                    print("返事のファイルを読めません。次の回を待ちます。")
                else:
                    caught_up = True
                for m in msgs or []:
                    for reply in ai.handle(m):
                        say(reply)
                    ai.state["last_message_id"] = m["id"]
                    ai.save_state()
                ai.save_state()
                flush()
        except KeyboardInterrupt:
            raise
        except Exception as e:
            caught_up = False
            ai.audit("loop_error", where="messages", kind=type(e).__name__)
            print("エラー（%s）。何もせず次の回を待ちます。" % type(e).__name__)
        try:
            with lock(ctx):
                ai.reload()                 # ロックを取り直したので、必ず状態を読み直す
                # 反映は、(1) 返事を最後まで読めた (2) 出せていない知らせが残っていない、の両方のときだけ
                if caught_up and not queue:
                    for note in ai.tick():
                        say(note)
                    flush()
                else:
                    print("返事を確認できていないか、知らせを出せていないため、この回は反映を行いません。")
        except KeyboardInterrupt:
            raise
        except Exception as e:  # 何が起きても落とさず、何もしないで次の回へ
            ai.audit("loop_error", where="tick", kind=type(e).__name__)
            print("エラー（%s）。何もせず次の回を待ちます。" % type(e).__name__)
        if once:
            return 0
        sleep(max(5, int(ctx.acfg["poll_seconds"])))


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="python -m ee_aidea.approval_cli")
    ap.add_argument("--config", default="ee-aidea.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check"); sub.add_parser("setup"); sub.add_parser("samples"); sub.add_parser("status")
    n = sub.add_parser("new"); n.add_argument("--diff", required=True); n.add_argument("--fields", required=True)
    n.add_argument("--send", action="store_true")
    s = sub.add_parser("send"); s.add_argument("id")
    rp = sub.add_parser("reply"); rp.add_argument("text", nargs="+")
    r = sub.add_parser("run"); r.add_argument("--once", action="store_true")
    sub.add_parser("pin-levels")
    a = ap.parse_args(argv)
    try:
        ctx = load_ctx(a.config)
        if a.cmd == "check":
            return cmd_check(ctx)
        if a.cmd == "setup":
            return cmd_setup(ctx)
        if a.cmd == "samples":
            return cmd_samples(ctx)
        if a.cmd == "reply":
            return cmd_reply(ctx, " ".join(a.text))
        if a.cmd == "status":
            with lock(ctx):
                ai = make(ctx)
            for p in ai.all():
                print("%s [%s] %s 先頭4文字=%s %s" % (p["id"], C.JP[p["level"]], p["status"], p["diff_sha256"][:4], p["summary"][:40]))
            if ai.stopped():
                print("（停止中）")
            return 0
        if a.cmd == "new":
            with lock(ctx):
                ai = make(ctx)
                try:
                    fields = json.loads(Path(a.fields).read_text(encoding="utf-8"))
                    diff = Path(a.diff).read_bytes()
                except FileNotFoundError as e:
                    raise C.PacketError("ファイルがない: %s" % Path(e.filename or "").name)
                except (ValueError, UnicodeDecodeError):
                    raise C.PacketError("項目のファイルを JSON として読めない")
                p = ai.new_proposal(diff, fields)
                print("作成: %s [%s] 先頭4文字=%s" % (p["id"], C.JP[p["level"]], p["diff_sha256"][:4]))
                return 0 if (not a.send or send(ai, ctx.outbox(), p["id"])) else 1
        if a.cmd == "send":
            with lock(ctx):
                ai = make(ctx)
                return 0 if send(ai, ctx.outbox(), a.id) else 1
        if a.cmd == "pin-levels":
            with lock(ctx):
                ai = make(ctx, pin=False)
                ai.state["levels_sha"] = hashlib.sha256(read_levels(ctx)[0]).hexdigest()
                ai.save_state()
                ai.audit("levels_pinned")
            print("levels.json の今の内容を、正しいものとして記録しました。")
            return 0
        if a.cmd == "run":
            return cmd_run(ctx, a.once)
    except (C.PacketError, C.ConfigError, CF.ConfigError) as e:
        print(str(e))
        return 1
    except KeyboardInterrupt:
        print("\n止めました。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
