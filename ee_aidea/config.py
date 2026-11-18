"""設定ファイル ee-aidea.json の読み込み。形が違えば ConfigError で止める (丸めて続けない)。

相対パスは、設定ファイルのあるフォルダを基準に解決する。
"""
import json, math, re
from pathlib import Path

NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
RESERVED = ("treasury",)      # 発行元。キット側の予約名で、課の名前には使えない

DEFAULTS = {
    "unit": "pt",
    "currency_label": "円",
    "utc_offset_hours": 9,
    "departments": ["research", "engineering", "qa"],
    "protected_depts": [],
    "labels": {},
    "ledger": {"path": "data/ledger.jsonl", "rates": "rates.json", "budgets": "budgets.json"},
    "auction": {"log": "data/auction_log.jsonl", "max_slots": 2, "min_max_bid": 10, "default_trust": 0.5, "max_num": 1e9},
    "approval": {"mode": "file", "deploy_root": "sandbox", "levels": "levels.json", "data_dir": "data",
                 "wait_hours_red": 24, "expire_days": 7, "max_message_age_minutes": 15, "poll_seconds": 30,
                 "file": {"inbox": "data/inbox.txt", "outbox": "data/outbox.log"}},
}


class ConfigError(Exception):
    pass


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def check_departments(depts, protected):
    if not isinstance(depts, list) or not depts:
        raise ConfigError("departments は課の名前の一覧 (1つ以上)")
    for d in depts:
        if not isinstance(d, str) or not NAME.match(d):
            raise ConfigError("課の名前は英小文字で始まり、英小文字・数字・_- だけ (32文字まで): %r" % (d,))
        if d in RESERVED:
            raise ConfigError("%s は予約名のため課の名前に使えない" % d)
    if len(set(depts)) != len(depts):
        raise ConfigError("departments に同じ名前が2回ある")
    if not isinstance(protected, list) or any(p not in depts for p in protected):
        raise ConfigError("protected_depts は departments にある課だけ")
    return tuple(depts), tuple(protected)


def validate(cfg):
    check_departments(cfg.get("departments"), cfg.get("protected_depts"))
    if not isinstance(cfg.get("unit"), str) or not cfg["unit"].strip() or len(cfg["unit"]) > 10:
        raise ConfigError("unit は10文字までの単位名")
    if not isinstance(cfg.get("currency_label"), str):
        raise ConfigError("currency_label は文字列")
    off = cfg.get("utc_offset_hours")
    if not _is_num(off) or not -12 <= off <= 14:
        raise ConfigError("utc_offset_hours は -12〜14 の数")
    if not isinstance(cfg.get("labels"), dict):
        raise ConfigError("labels は {課: 表示名} の形")
    a = cfg["auction"]
    if isinstance(a.get("max_slots"), bool) or not isinstance(a.get("max_slots"), int) or not 0 <= a["max_slots"] <= 20:
        raise ConfigError("auction.max_slots は 0〜20 の整数")
    for k in ("min_max_bid", "max_num"):
        if not _is_num(a.get(k)) or a[k] <= 0:
            raise ConfigError("auction.%s は正の数" % k)
    if not _is_num(a.get("default_trust")) or not 0 <= a["default_trust"] <= 1:
        raise ConfigError("auction.default_trust は 0〜1")
    ap = cfg["approval"]
    if ap.get("mode") != "file":
        raise ConfigError("approval.mode は \"file\" だけ (この版にはファイルモードしかない)")
    for k in ("wait_hours_red", "expire_days", "max_message_age_minutes", "poll_seconds"):
        if not _is_num(ap.get(k)) or ap[k] < 0:
            raise ConfigError("approval.%s は0以上の数" % k)
    return cfg


def load(path):
    """ee-aidea.json を読み、既定値を補い、相対パスを絶対パスにした dict を返す。"""
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError("設定ファイルがない: %s (ee-aidea.example.json をコピーして作る)" % p.name)
    except (ValueError, UnicodeDecodeError):
        raise ConfigError("設定ファイルを JSON として読めない: %s" % p.name)
    if not isinstance(raw, dict):
        raise ConfigError("設定ファイルの形が不正 ({...} の形)")
    cfg = validate(_merge(DEFAULTS, raw))
    base = p.resolve().parent
    cfg["_base"] = str(base)

    def res(v):
        q = Path(v)
        return str(q if q.is_absolute() else base / q)
    for k in ("path", "rates", "budgets"):
        cfg["ledger"][k] = res(cfg["ledger"][k])
    cfg["auction"]["log"] = res(cfg["auction"]["log"])
    ap = cfg["approval"]
    for k in ("deploy_root", "levels", "data_dir"):
        ap[k] = res(ap[k])
    for k in ("inbox", "outbox"):
        ap["file"][k] = res(ap["file"][k])
    return cfg
