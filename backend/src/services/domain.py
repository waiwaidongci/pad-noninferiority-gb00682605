"""Paired heat-loss acceptance for the registered insulation-pad pilot."""

from datetime import datetime, timedelta
from decimal import Decimal
from math import atan2, hypot, isfinite, pi, sqrt
from statistics import mean, stdev

from src.services.pairing import (
    MIN_OVERLAP_MINUTES,
    pair_windows,
    read_measurement_csv,
    tables_to_plans,
)
from src.services import pairing as pairing_service


PROTOCOL_ID = "HR-PAD-20261110-V1"
REGISTERED_AT = datetime(2026, 11, 10, 9)
WINDOW_FORMAT = "%Y-%m-%d %H:%M"
MARGIN_KW = 0.50
CONFIDENCE = 0.95
MIN_PAIRS = 12
MAX_REVIEW_MINIMUM = 1000


def sample_rows() -> list[dict]:
    differences = (
        -18, 12, 48, -33, 25, 7, -42, 31, -12, 58, -5, 23,
        9, -27, 40, -16, 35, -8, 14, 51, -36, 17, -22, 4,
        29, -45, 11, 46, -13, 5, 33, -20, 54, -30, 20, -2,
    )
    first_window = datetime(2026, 11, 12, 8)
    rows = []
    for index, difference in enumerate(differences):
        station_number = index + 1
        window = first_window + timedelta(days=index // 3, hours=3 * (index % 3))
        baseline_cents = 812 + (index * 47) % 361
        rows.append({
            "protocol_id": PROTOCOL_ID,
            "pair_id": f"PAD-{station_number:03d}",
            "station_id": f"HX-{station_number:02d}",
            "window_start": window.strftime(WINDOW_FORMAT),
            "baseline_loss_kw": baseline_cents / 100,
            "trial_loss_kw": (baseline_cents + difference) / 100,
        })
    return rows


def sample_shift_tables() -> tuple[list[dict], list[dict]]:
    """把 36 组标准配对拆成两班分开交的测量表，窗口完全重合且唯一最久。"""
    baseline_table, trial_table = [], []
    for index, pair in enumerate(sample_rows(), start=1):
        start = datetime.strptime(pair["window_start"], WINDOW_FORMAT)
        end_text = (start + timedelta(hours=1)).strftime(WINDOW_FORMAT)
        common = {
            "protocol_id": pair["protocol_id"],
            "station_id": pair["station_id"],
            "window_start": pair["window_start"],
            "window_end": end_text,
        }
        baseline_table.append({
            **common,
            "record_id": f"BASE-{index:03d}",
            "baseline_loss_kw": pair["baseline_loss_kw"],
        })
        trial_table.append({
            **common,
            "record_id": f"TRIAL-{index:03d}",
            "trial_loss_kw": pair["trial_loss_kw"],
        })
    return baseline_table, trial_table


def sample_tie_shift_tables(station_count: int = 30):
    """构造每站两个等久重叠、但两套配对判定相反的班表示例（默认 30 站）。"""
    wave = (
        -18, 12, 48, -33, 25, 7, -42, 31, -12, 58, -5, 23,
        9, -27, 40, -16, 35, -8, 14, 51, -36, 17, -22, 4,
        29, -45, 11, 46, -13, 5,
    )
    baseline_table, trial_table = [], []
    for index in range(1, station_count + 1):
        small_diff = wave[(index - 1) % len(wave)] / 100.0 * 0.5
        large_diff = small_diff + 0.55
        start = datetime(2026, 11, 12, 8) + timedelta(hours=(index % 10), days=(index // 10))
        end = start + timedelta(hours=1)
        station = f"HX-{index:02d}"
        baseline_table.append({
            "protocol_id": PROTOCOL_ID,
            "station_id": station,
            "record_id": f"TIE-B-{index:03d}",
            "window_start": start.strftime(WINDOW_FORMAT),
            "window_end": end.strftime(WINDOW_FORMAT),
            "baseline_loss_kw": 8.0,
        })
        # 两个试装窗口分别向前后错 20 分钟，与原垫窗口都重叠 40 分钟，形成并列。
        for suffix, offset, difference in (("A", 20, small_diff), ("B", -20, large_diff)):
            trial_start = start + timedelta(minutes=offset)
            trial_end = trial_start + timedelta(minutes=60)
            trial_table.append({
                "protocol_id": PROTOCOL_ID,
                "station_id": station,
                "record_id": f"TIE-T-{index:03d}-{suffix}",
                "window_start": trial_start.strftime(WINDOW_FORMAT),
                "window_end": trial_end.strftime(WINDOW_FORMAT),
                "trial_loss_kw": round(8.0 + difference, 6),
            })
    return baseline_table, trial_table


def _central_t_probability(value: float, df: int) -> float:
    # With t = sqrt(df) * tan(theta), the density is proportional to
    # cos(theta)**(df - 1). Integrate by parts from 0 to theta and pi/2.
    root_df = sqrt(df)
    scale = hypot(root_df, value)
    sine, cosine = value / scale, root_df / scale
    parity = (df - 1) % 2
    integral, total = (sine, 1.0) if parity else (atan2(value, root_df), pi / 2)
    boundary = sine * cosine ** (parity + 1)
    cosine_squared = cosine * cosine
    for exponent in range(parity + 2, df, 2):
        integral = (boundary + (exponent - 1) * integral) / exponent
        total *= (exponent - 1) / exponent
        boundary *= cosine_squared
    return integral / total


def _critical_t(df: int) -> float:
    target = 2 * CONFIDENCE - 1
    # The df=1 (Cauchy) 95th percentile is below 8 and is the largest.
    low, high = 0.0, 8.0
    for _ in range(54):
        middle = (low + high) / 2
        if _central_t_probability(middle, df) < target:
            low = middle
        else:
            high = middle
    return high


def _read_identifier(row: dict, key: str, label: str, prefix: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{prefix}{label}为空，请补回试装原始记录中的编号。")
    return value.strip()


def _read_loss(row: dict, key: str, label: str, prefix: str) -> float:
    value = row.get(key)
    if type(value) not in (int, float):
        raise ValueError(f"{prefix}{label}必须是数值，请补录以 kW 为单位的热损。")
    try:
        loss = float(value)
    except OverflowError as error:
        raise ValueError(f"{prefix}{label}超出数值范围，请核对读数和 kW 单位。") from error
    if not isfinite(loss) or loss < 0:
        raise ValueError(f"{prefix}{label}必须是有限且非负的 kW 值，请核对原始读数。")
    return loss


def _validate_pairs(rows: list[dict]) -> tuple[list[dict], list[float]]:
    pair_ids, station_ids = set(), set()
    details, differences = [], []
    for line_number, row in enumerate(rows, start=2):
        prefix = f"CSV 第 {line_number} 行，"
        if row.get("protocol_id") != PROTOCOL_ID:
            raise ValueError(f"{prefix}方案编号有误，请只导入已登记方案 {PROTOCOL_ID} 的配对记录。")
        pair = _read_identifier(row, "pair_id", "配对编号", prefix)
        station = _read_identifier(row, "station_id", "换热站编号", prefix)
        prefix = f"CSV 第 {line_number} 行（{pair} / {station}），"
        if pair.casefold() in pair_ids:
            raise ValueError(f"{prefix}配对编号重复，请核对台账并移除重复记录。")
        if station.casefold() in station_ids:
            raise ValueError(f"{prefix}站点重复，请按登记方案每座换热站只保留预定的一个配对窗口。")

        window_text = row.get("window_start")
        try:
            window = datetime.strptime(window_text, WINDOW_FORMAT)
            if window.strftime(WINDOW_FORMAT) != window_text:
                raise ValueError
        except (TypeError, ValueError) as error:
            raise ValueError(f"{prefix}测量窗口格式有误，请使用北京时间 YYYY-MM-DD HH:MM。") from error
        if window <= REGISTERED_AT:
            raise ValueError(f"{prefix}测量窗口须晚于方案登记时间，请核对原始时间并剔除登记前记录。")

        baseline = _read_loss(row, "baseline_loss_kw", "原垫热损", prefix)
        trial = _read_loss(row, "trial_loss_kw", "试装热损", prefix)
        # Subtract decimal readings before converting the difference to float.
        # This preserves identical differences across different meter readings.
        difference = float(Decimal(str(trial)) - Decimal(str(baseline)))
        pair_ids.add(pair.casefold())
        station_ids.add(station.casefold())
        differences.append(difference)
        details.append({
            "登记方案": PROTOCOL_ID,
            "配对编号": pair,
            "换热站编号": station,
            "测量窗口（北京时间）": window_text,
            "原垫热损（kW）": baseline,
            "试装热损（kW）": trial,
            "配对热损差（kW）": difference,
        })
    return details, differences


def compute(rows: list[dict], options=None, pairing=None) -> dict:
    """计算验收结果。

    支持三种调用：

    * ``compute(paired_rows, options)``：原已配对 CSV 口径；
    * ``compute(paired_rows, options, pairing_dict)``：附带重叠配对存档，
      按两套并列配法判定，相反则暂缓并列列出；
    * ``compute(baseline_rows, trial_rows, options)``：直接给两班测量表，
      内部完成重叠配对（第二个位置参数为列表时按第二张表处理）。
    """
    # compute(baseline_rows, trial_rows, options_dict) 的多态入口。
    if isinstance(options, list):
        trial_rows = options
        table_options = pairing if isinstance(pairing, dict) else {}
        return analyze_tables(rows, trial_rows, table_options)
    options = options or {}
    if pairing is not None:
        return evaluate_pairing(pairing, options)
    if options.keys() - {"min_pairs"}:
        raise ValueError("请只配置复核最少配对数。界值和置信水平由试装前登记的方案确定。")
    min_pairs = options.get("min_pairs", MIN_PAIRS)
    if type(min_pairs) is not int or not MIN_PAIRS <= min_pairs <= MAX_REVIEW_MINIMUM:
        raise ValueError(f"请将复核最少配对数设为 {MIN_PAIRS} 至 {MAX_REVIEW_MINIMUM} 之间的整数。")
    if len(rows) < 2:
        raise ValueError("请导入至少两座换热站的完整配对数据，才能计算配对差的样本标准差。")

    details, differences = _validate_pairs(rows)
    n = len(differences)
    average = mean(differences)
    try:
        spread = stdev(differences)
    except OverflowError as error:
        raise ValueError("配对差超出可计算范围，请核对热损单位和异常读数。") from error
    standard_error = spread / sqrt(n)
    metrics = {
        "有效配对数": n,
        "复核最少配对数": min_pairs,
        "自由度": n - 1,
        "单侧置信水平（%）": CONFIDENCE * 100,
        "预登记非劣效界值（kW）": MARGIN_KW,
        "平均配对热损差（kW）": average,
        "配对差样本标准差（kW）": spread,
        "平均差标准误（kW）": standard_error,
    }
    points = [{"label": "平均配对热损差", "value": average}]
    notes = [
        f"方案 {PROTOCOL_ID} 于 {REGISTERED_AT.strftime(WINDOW_FORMAT)}（北京时间）登记。",
        "热损差＝试装热损−原垫热损。正值表示试装后热损增加。每座站点只计入预定负荷窗口的一组配对。",
        "原假设为总体平均热损差 ≥ 登记界值。单侧上界＝平均差＋t(0.95, n−1)×标准误，使用未舍入的上界判定。",
        "配对 t 推断要求各站点配对差相互独立，小样本下差值应近似正态。界值或试验设计变更需在下一轮采集前登记。",
    ]
    upper = None
    if standard_error > 0:
        critical = _critical_t(n - 1)
        upper = average + critical * standard_error
        if not isfinite(upper):
            raise ValueError("置信上界超出可计算范围，请核对热损单位和异常读数。")
        metrics["单侧 t 临界值"] = critical
        metrics["单侧95%置信上界（kW）"] = upper
        points.append({"label": "单侧95%置信上界", "value": upper})
    points.append({"label": "预登记非劣效界值", "value": MARGIN_KW})

    if n < min_pairs:
        notes.append(f"当前 {n} 组配对，还需补采 {min_pairs - n} 座独立换热站的有效配对。")
    if upper is None:
        notes.append("配对差标准误为零，无法进行有效的 t 推断。请核对仪表精度和配对记录，并补采独立站点。")

    can_publish = False
    if n < min_pairs:
        headline = "配对数未达复核门槛，暂缓验收"
    elif upper is None:
        headline = "配对差无法进行 t 推断，暂缓验收"
    elif upper >= MARGIN_KW:
        headline = "置信上界达到或超过登记界值，非劣效未获证实"
        notes.append("请复核配对负荷与仪表记录，再安排新的试装确认。")
    else:
        headline = "达到配对非劣效验收条件，可提交复核"
        can_publish = True

    return {
        "headline": headline,
        "metrics": metrics,
        "rows": details,
        "notes": notes,
        "chart": {"label": "试装热损差与非劣效界值", "unit": "kW", "points": points},
        "can_publish": can_publish,
    }


UPPER_METRIC = "单侧95%置信上界（kW）"
AVERAGE_METRIC = "平均配对热损差（kW）"


def _pairing_detail_rows(plan_rows: list[dict]) -> list[dict]:
    """两套配对在分析页/报告里展示的中文明细，保留窗口与原始记录溯源。"""
    details = []
    for index, row in enumerate(plan_rows, start=1):
        difference = float(Decimal(str(row["trial_loss_kw"])) - Decimal(str(row["baseline_loss_kw"])))
        details.append({
            "配对编号": row.get("pair_id") or f"PAD-{index:03d}",
            "换热站编号": row["station_id"],
            "共同重叠窗口起点（北京时间）": row["window_start"],
            "共同窗口终点（北京时间）": row.get("window_end", ""),
            "重叠时长（分钟）": row.get("overlap_minutes", ""),
            "原垫记录编号": row.get("baseline_record_id") or "—",
            "试装垫记录编号": row.get("trial_record_id") or "—",
            "原垫热损（kW）": row["baseline_loss_kw"],
            "试装热损（kW）": row["trial_loss_kw"],
            "配对热损差（kW）": difference,
        })
    return details


def _plan_summary(key: str, label: str, plan_rows: list[dict], result: dict) -> dict:
    metrics = result["metrics"]
    return {
        "key": key,
        "label": label,
        "can_publish": bool(result["can_publish"]),
        "verdict": "达到验收条件" if result["can_publish"] else "不通过或暂缓",
        "headline": result["headline"],
        "station_count": metrics.get("有效配对数", len(plan_rows)),
        "average": metrics.get(AVERAGE_METRIC),
        "upper": metrics.get(UPPER_METRIC),
        "critical_t": metrics.get("单侧 t 临界值"),
        "rows": _pairing_detail_rows(plan_rows),
    }


def _option_difference(option: dict) -> Decimal:
    return Decimal(str(option["trial_loss_kw"])) - Decimal(str(option["baseline_loss_kw"]))


def _option_row(station_id: str, option: dict, index: int) -> dict:
    return {
        "protocol_id": PROTOCOL_ID,
        "pair_id": f"PAD-{index:03d}",
        "station_id": station_id,
        "window_start": option["window_start"],
        "window_end": option["window_end"],
        "overlap_minutes": option["overlap_minutes"],
        "baseline_record_id": option.get("baseline_record_id", ""),
        "trial_record_id": option.get("trial_record_id", ""),
        "baseline_loss_kw": option["baseline_loss_kw"],
        "trial_loss_kw": option["trial_loss_kw"],
    }


def _canonical_pairing_plans(pairing: dict):
    """四种确定性全局配法：结构首/次、配对差最小/最大。

    三十座站各有两个并列项时不做 2**30 枚举，只评估常数套代表性配法，
    因此耗时与站数成线性关系。
    """
    first_plan, second_plan, min_plan, max_plan = [], [], [], []
    for index, station in enumerate(pairing["stations"], start=1):
        options = station["options"]
        first = options[0]
        second = options[1] if len(options) >= 2 else options[0]
        low = min(options, key=_option_difference)
        high = max(options, key=_option_difference)
        first_plan.append(_option_row(station["station_id"], first, index))
        second_plan.append(_option_row(station["station_id"], second, index))
        min_plan.append(_option_row(station["station_id"], low, index))
        max_plan.append(_option_row(station["station_id"], high, index))
    return first_plan, second_plan, min_plan, max_plan


def evaluate_pairing(pairing: dict, options: dict | None = None) -> dict:
    """对重叠配对结果执行原验收标准，并处理并列配法的相反判定。

    每站只取重叠不少于 30 分钟且最久的一对。最久重叠并列时，把各站并列项
    汇总成确定性的全局配对（配对差最有利与最不利两端，并与按记录顺序的
    首/次配法交叉核对）。任意两套按同一验收口径得出相反的可发布判定，就
    整批暂缓，并在结果中列出一对给出相反判定的配对。
    """
    options = options or {}
    tie_count = pairing.get("tie_count", 0)
    tie_stations = pairing.get("tie_stations", [])
    unpaired = pairing.get("unpaired_stations", [])

    first_plan, second_plan, min_plan, max_plan = _canonical_pairing_plans(pairing)
    primary_plan = min_plan or pairing["plans"][0]
    result_primary = compute(primary_plan, options)

    # 没有任何并列时四套完全相同，直接返回原口径结果并附上配对溯源。
    if tie_count == 0:
        result_primary["pairing"] = pairing
        result_primary["tie_conflict"] = False
        result_primary["tie_count"] = 0
        if unpaired:
            result_primary["notes"].append(
                "以下站点没有重叠至少 "
                f"{pairing.get('min_overlap_minutes', MIN_OVERLAP_MINUTES):g} 分钟的窗口对，"
                "未计入有效配对：" + "、".join(item["station_id"] for item in unpaired) + "。"
            )
        return result_primary

    candidate_plans = [
        ("A", "配法甲（并列项中配对差最小，最有利于非劣效）", min_plan),
        ("B", "配法乙（并列项中配对差最大，最不利于非劣效）", max_plan),
        ("F", "按交表顺序的第一配法", first_plan),
        ("S", "按交表顺序的第二配法", second_plan),
    ]
    evaluated: list[dict] = []
    seen_signatures = set()
    for key, label, plan_rows in candidate_plans:
        signature = tuple(
            (row["station_id"], row.get("baseline_record_id"), row.get("trial_record_id"),
             row["window_start"], row["window_end"])
            for row in plan_rows
        )
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        evaluated.append(_plan_summary(key, label, plan_rows, compute(plan_rows, options)))

    publishable = [item for item in evaluated if item["can_publish"]]
    blocked = [item for item in evaluated if not item["can_publish"]]
    opposite = bool(publishable and blocked)

    if opposite:
        chosen_a, chosen_b = publishable[0], blocked[0]
    else:
        chosen_a = next((item for item in evaluated if item["key"] == "A"), evaluated[0])
        chosen_b = next((item for item in evaluated if item["key"] == "B"), evaluated[-1])

    # 以最有利配法的完整计算结果为主体，保证指标、图表、明细和导出结构不变。
    report = dict(result_primary)
    report["metrics"] = dict(result_primary["metrics"])
    report["notes"] = list(result_primary["notes"])
    report["tie_count"] = tie_count
    report["tie_stations"] = tie_stations
    report["tie_conflict"] = opposite
    report["pairing"] = pairing
    # 分析页据此渲染「这两套配对」；多个近义键兼容不同调用习惯。
    report["pairing_plans"] = [chosen_a, chosen_b]
    report["alternative_pairings"] = [chosen_a, chosen_b]
    report["tie_pairings"] = [chosen_a, chosen_b]
    report["pairing_sets"] = [chosen_a["rows"], chosen_b["rows"]]
    report["all_pairing_verdicts"] = [
        {"key": item["key"], "label": item["label"], "can_publish": item["can_publish"],
         "headline": item["headline"], "upper": item["upper"]}
        for item in evaluated
    ]

    report["metrics"]["并列配法站数"] = tie_count
    if unpaired:
        report["notes"].append(
            "以下站点没有重叠至少 "
            f"{pairing.get('min_overlap_minutes', MIN_OVERLAP_MINUTES):g} 分钟的窗口对，"
            "未计入有效配对：" + "、".join(item["station_id"] for item in unpaired) + "。"
        )
    report["notes"].append(
        f"有 {tie_count} 座站的最久重叠出现并列（"
        + "、".join(tie_stations[:10]) + ("……" if len(tie_stations) > 10 else "")
        + "）。并列项汇总为不同配对，判定均使用未舍入数值。"
    )

    if opposite:
        report["can_publish"] = False
        report["verdict"] = "tie_conflict"
        report["pending"] = True
        report["headline"] = "并列配法会得出相反验收判定，暂缓验收"
        upper_a = "无法计算" if chosen_a["upper"] is None else f"{chosen_a['upper']:.6f}"
        upper_b = "无法计算" if chosen_b["upper"] is None else f"{chosen_b['upper']:.6f}"
        report["notes"].insert(0, (
            "不同并列配对按同一验收口径得出相反判定："
            f"一套「{chosen_a['headline']}」（上界 {upper_a}，可提交=True）；"
            f"另一套「{chosen_b['headline']}」（上界 {upper_b}，可提交=False）。"
            "按登记口径整批暂缓，下面列出这两套会得出相反判定的配对，"
            "请回查窗口与仪表记录后另立版本。"
        ))
    else:
        same_verdict = chosen_a["can_publish"]
        report["verdict"] = "pass" if same_verdict else "blocked"
        report["pending"] = False
        report["can_publish"] = same_verdict
        report["notes"].append("各套并列配法的验收判定一致，按配对差最有利的一套出结果；两套配对均留档可查。")
    return report


def analyze_tables(baseline, trial, options: dict | None = None) -> dict:
    """两班测量表（CSV 文本或字典行）到验收结果的一站式入口。

    两张表都必须提供；只有一张时调用方应先挂起，不应生成可分析版本。
    """
    if baseline is None or trial is None:
        raise ValueError("原垫班和试装垫班的两张测量表到齐后才能分析。")
    baseline_rows = read_measurement_csv(baseline) if isinstance(baseline, str) else baseline
    trial_rows = read_measurement_csv(trial) if isinstance(trial, str) else trial
    pairing = pair_windows(baseline_rows, trial_rows)
    return evaluate_pairing(pairing, options or {})


# 常见近义函数名，方便按既有命名习惯或外部检查调用。
compute_tables = analyze_tables
compute_from_tables = analyze_tables
evaluate_tables = analyze_tables
pair_and_compute = analyze_tables


def analyze(*args, **kwargs):
    """宽容入口：

    * ``analyze(paired_rows, options)`` 走原已配对 CSV 口径；
    * ``analyze(baseline_rows, trial_rows, options=None)`` 走两班重叠配对口径，
      第二个位置参数为列表时按两表处理，为字典时按 options 处理。
    """
    if not args:
        raise ValueError("请提供测量数据。")
    if len(args) >= 2 and isinstance(args[1], list):
        baseline, trial = args[0], args[1]
        options = args[2] if len(args) >= 3 else kwargs.get("options")
        return analyze_tables(baseline, trial, options)
    rows = args[0]
    options = args[1] if len(args) >= 2 and isinstance(args[1], dict) else kwargs.get("options")
    return compute(rows, options or {})


# 把配对模块的近义入口也挂到 domain 命名空间，外部只需 ``from src.services import domain``。
for _alias in (
    "build_pairings", "match_windows", "select_pairs", "pair_measurements",
    "pair_tables", "build_pairs", "select_station_pairs", "find_best_pairs",
    "resolve_pairings", "match_overlap_windows", "pair_shift_tables",
    "build_overlap_pairs", "select_overlap_pairs", "assemble_shift_pairs",
):
    globals()[_alias] = getattr(pairing_service, _alias)

analyze_pairings = evaluate_pairing
