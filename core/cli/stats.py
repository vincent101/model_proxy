import json
import sys
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
DIMS = ("supply", "route", "strategy")
VAL_FIELDS = ("requests", "ok", "fail", "usage_in", "usage_out")


def zero_bucket():
    return {"requests": 0, "ok": 0, "fail": 0, "sum_ms": 0, "max_ms": 0, "combos": {}}


def zero_group():
    return {k: 0 for k in VAL_FIELDS}


def cst_today_str():
    return datetime.now(CST).strftime("%Y-%m-%d")


def merge_bucket_into(dst, src):
    dst["requests"] += src.get("requests", 0)
    dst["ok"] += src.get("ok", 0)
    dst["fail"] += src.get("fail", 0)
    dst["sum_ms"] += src.get("sum_ms", 0)
    # OPT-10: max_ms 取 max（跨天桶合并）
    src_max = src.get("max_ms", 0)
    if src_max > dst.get("max_ms", 0):
        dst["max_ms"] = src_max
    for key, v in src.get("combos", {}).items():
        combo = dst["combos"].setdefault(key, zero_group())
        for f in VAL_FIELDS:
            combo[f] += v.get(f, 0)


def get_month_bucket(data, month_key):
    """无条件合并 months_archive[月] 与 days 里剩余同月天桶。
    二者互斥不重叠（归档时天桶已从 days 删除，见 core/server.py _archive_if_needed），
    合并后即为完整月度数据，不会重复计算。"""
    merged = zero_bucket()
    archived = data.get("months_archive", {}).get(month_key)
    if archived is not None:
        merge_bucket_into(merged, archived)
    for day_key, day_bucket in data.get("days", {}).items():
        if day_key[:7] == month_key:
            merge_bucket_into(merged, day_bucket)
    return merged


def select_bucket(data, time_sel):
    """返回 (bucket_dict, label)。bucket_dict 含 requests/ok/fail/sum_ms/combos。"""
    if time_sel is None or time_sel == "total":
        return data.get("total", zero_bucket()), "total (全历史)"
    if time_sel == "today":
        day_key = cst_today_str()
        return data.get("days", {}).get(day_key, zero_bucket()), f"{day_key} (today, UTC+8)"
    if time_sel == "month":
        month_key = cst_today_str()[:7]
        bucket = get_month_bucket(data, month_key)
        return bucket, f"{month_key} (month, UTC+8)"
    if len(time_sel) == 10 and time_sel.count("-") == 2:
        return data.get("days", {}).get(time_sel, zero_bucket()), f"{time_sel} (UTC+8)"
    if len(time_sel) == 7 and time_sel.count("-") == 1:
        bucket = get_month_bucket(data, time_sel)
        return bucket, f"{time_sel} (UTC+8)"
    print(f"Error: 无法识别的时间选择器: {time_sel!r}", file=sys.stderr)
    sys.exit(1)


def parse_combo_key(key):
    dims = {}
    for part in key.split("|"):
        k, v = part.split("=", 1)
        dims[k] = v
    return dims


def fmt_k(n):
    if abs(n) >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def main(argv):
    totals_file = argv[0]
    args = argv[1:]

    with open(totals_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 参数解析：只接受时间选择器（today/month/total/YYYY-MM-DD/YYYY-MM），其余参数报错。
    time_sel = None
    if args:
        first = args[0]
        is_time_like = (
            first in ("today", "month", "total")
            or (len(first) == 10 and first.count("-") == 2)
            or (len(first) == 7 and first.count("-") == 1)
        )
        if is_time_like:
            time_sel = first
        else:
            print(f"Error: 无法识别的参数: {first!r}（应为 today/month/total/YYYY-MM-DD/YYYY-MM）", file=sys.stderr)
            sys.exit(1)

    bucket, label = select_bucket(data, time_sel)

    combos = bucket.get("combos", {})

    def aggregate(proj_dim):
        groups = {}
        for key, v in combos.items():
            dims = parse_combo_key(key)
            gkey = dims.get(proj_dim) if proj_dim else "(all)"
            g = groups.setdefault(gkey, zero_group())
            for f in VAL_FIELDS:
                g[f] += v.get(f, 0)
        return groups

    period_requests = bucket.get("requests", 0)
    period_ok = bucket.get("ok", 0)
    period_fail = bucket.get("fail", 0)
    sum_ms = bucket.get("sum_ms", 0)
    avg_ms_str = f"{sum_ms / period_requests:.1f}" if period_requests else "0"
    max_ms_str = str(bucket.get("max_ms", 0))
    all_groups = aggregate(None)
    filtered_total = all_groups.get("(all)", zero_group())
    usage_in, usage_out = (
        filtered_total["usage_in"], filtered_total["usage_out"])

    print(f"period: {label}   requests={period_requests}  ok={period_ok}  fail={period_fail}  "
          f"avg_ms={avg_ms_str}  max_ms={max_ms_str}  "
          f"usage_in={fmt_k(usage_in)} usage_out={fmt_k(usage_out)}")

    def print_groups(proj_dim):
        groups = aggregate(proj_dim)
        if not groups:
            print("  (no data)")
            return
        for gkey, g in sorted(groups.items(), key=lambda kv: -kv[1]["requests"]):
            print(f"  {gkey:32} requests={g['requests']:<6} ok={g['ok']:<6} fail={g['fail']:<4} "
                  f"in={fmt_k(g['usage_in'])} out={fmt_k(g['usage_out'])}")

    # 对 supply/route/strategy 三个维度各做一次投影，各列一段
    for d in DIMS:
        print(f"by {d}:")
        print_groups(d)


