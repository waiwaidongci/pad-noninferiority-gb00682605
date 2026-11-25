"""两班分开提交的原垫/试装垫测量表，按窗口重叠配对。

业务口径（见 README「两班交表与重叠配对」）：

* 原垫班和试装垫班各交一张测量表，每张表的一行是某座站的一个测量窗口；
* 同一座站的原垫窗口与试装垫窗口两两计算重叠时长；
* 只保留重叠至少 ``MIN_OVERLAP_MINUTES`` 分钟、且重叠最久的候选；
* 最久重叠若并列，则该站有两种并列配法，汇总成甲、乙两套全局配对；
* 配对只做几何选择，方案登记时间等验收口径仍由 :mod:`src.services.domain` 校验。

本模块只依赖标准库，可直接用 ``python -I -m unittest`` 验证。
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

PROTOCOL_ID = "HR-PAD-20261110-V1"
WINDOW_FORMAT = "%Y-%m-%d %H:%M"
MIN_OVERLAP_MINUTES = 30.0
# 分钟级数据上的浮点容差，30 分钟整必须算「至少半小时」，并列比较不被浮点尾差拆开。
_OVERLAP_EPS = 1e-6

_BEIJING = timezone(timedelta(hours=8))

# 常见中英文表头别名；键是规范字段。匹配时英文不区分大小写、忽略空格/下划线/BOM。
ALIASES: dict[str, tuple[str, ...]] = {
    "station_id": (
        "station_id", "station", "station_code", "station_no", "stationid", "stationcode",
        "站号", "换热站编号", "站点编号", "站点", "换热站",
    ),
    "record_id": (
        "record_id", "measurement_id", "reading_id", "window_id", "meter_id",
        "record_no", "id", "记录编号", "测量记录编号", "测量编号", "记录号",
    ),
    "protocol_id": ("protocol_id", "protocol", "方案编号", "登记方案编号"),
    "window_start": (
        "window_start", "start", "start_time", "begin", "begin_time", "window_begin",
        "started_at", "start_at", "窗口起点", "测量窗口起点", "起点时间", "起点", "开始时间",
    ),
    "window_end": (
        "window_end", "end", "end_time", "finish", "finish_time", "window_finish",
        "finished_at", "end_at", "窗口终点", "测量窗口终点", "终点时间", "终点", "结束时间",
    ),
    "duration_minutes": (
        "duration_minutes", "duration_min", "window_minutes", "window_duration_min",
        "window_duration_minutes", "minutes", "duration", "window_length_min",
        "window_length_minutes", "length_minutes", "时长", "窗口时长", "持续时间（分钟）",
    ),
    "loss_kw": (
        "loss_kw", "heat_loss_kw", "average_loss_kw", "avg_loss_kw", "mean_loss_kw",
        "baseline_loss_kw", "trial_loss_kw", "baseline_loss", "trial_loss",
        "heat_loss", "loss",
        "原垫平均热损（kw）", "试装垫平均热损（kw）",
        "原垫热损", "试装垫热损", "平均热损", "热损",
    ),
}

_TIME_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
)


def _header_key(name: Any) -> str:
    text = str(name if name is not None else "").replace("﻿", "").strip().lower()
    return re.sub(r"[\s_\-/（）()]+", "", text)


def _build_header_map(fieldnames: Iterable[str] | None) -> dict[str, str]:
    """返回规范字段 -> CSV 实际表头。"""
    lookup: dict[str, str] = {}
    normalized_aliases = {
        canonical: {_header_key(alias) for alias in aliases}
        for canonical, aliases in ALIASES.items()
    }
    for actual in fieldnames or []:
        key = _header_key(actual)
        if not key:
            continue
        for canonical, candidates in normalized_aliases.items():
            if key in candidates and canonical not in lookup:
                lookup[canonical] = actual
                break
    return lookup


def parse_window(value: Any) -> datetime:
    """把窗口时刻解析成北京时间朴素 ``datetime``。

    接受 ``YYYY-MM-DD HH:MM``、ISO（可带时区）以及 ``YYYY/MM/DD HH:MM``。
    带时区的时刻统一换算到北京时间后去掉 tzinfo，便于和登记时间直接比较。
    """
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(_BEIJING).replace(tzinfo=None)
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError("测量窗口时间为空，请补回窗口起点和终点。")
    text = value.strip().replace("/", "-")
    # 让 "2026-11-12 08:00" 与 T 形式都能进入同一组格式尝试。
    candidates = [text, text.replace("T", " ")] if "T" in text else [text]
    for candidate in candidates:
        for fmt in _TIME_FORMATS:
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ValueError(
            f"测量窗口时间 {value!r} 格式有误，请使用北京时间 YYYY-MM-DD HH:MM。"
        ) from error
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_BEIJING).replace(tzinfo=None)
    return parsed


def _parse_duration_minutes(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minutes = float(value)
    else:
        text = str(value).strip().lower()
        if not text:
            return None
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(分钟|分|min|m)?", text)
        if match:
            minutes = float(match.group(1))
        else:
            match = re.fullmatch(r"(\d+):([0-5]?\d)(?::([0-5]?\d))?", text)
            if match:
                hours = int(match.group(1))
                minute = int(match.group(2))
                second = int(match.group(3) or 0)
                return hours * 60 + minute + second / 60
            if text.endswith(("小时", "h")):
                try:
                    return float(text.rstrip("小时h ")) * 60
                except ValueError:
                    return None
            return None
    if minutes <= 0 or minutes != minutes or minutes in (float("inf"), float("-inf")):
        raise ValueError("窗口时长必须是大于零的分钟数。")
    return minutes


def _parse_loss(value: Any, pad_label: str, prefix: str) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{prefix}{pad_label}热损必须是数值，请补录以 kW 为单位的读数。")
    if isinstance(value, (int, float)):
        loss = float(value)
    else:
        text = str(value).strip()
        if not text:
            raise ValueError(f"{prefix}{pad_label}热损为空，请补录以 kW 为单位的读数。")
        try:
            loss = float(Decimal(text))
        except Exception as error:  # Decimal 对各种非法字符串抛不同异常
            raise ValueError(f"{prefix}{pad_label}热损 {text!r} 不是有效数值。") from error
    if loss != loss or loss in (float("inf"), float("-inf")):
        raise ValueError(f"{prefix}{pad_label}热损必须是有限数值。")
    if loss < 0:
        raise ValueError(f"{prefix}{pad_label}热损不能为负，请核对原始读数和 kW 单位。")
    return loss


def read_measurement_csv(text: str) -> list[dict[str, Any]]:
    """解析一班的测量表 CSV 为原始字典行（保留表头）。"""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("测量表内容为空，请先从班组台账导出 CSV。")
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿"), newline=""), strict=True)
    try:
        fieldnames = reader.fieldnames
    except csv.Error as error:
        raise ValueError("CSV 表头的引号格式有误。") from error
    if not fieldnames:
        raise ValueError("CSV 缺少表头，请使用班组测量表导出的列名。")
    rows: list[dict[str, Any]] = []
    try:
        for line_number, row in enumerate(reader, start=2):
            if None in row:
                raise ValueError(f"CSV 第 {line_number} 行列数多于表头。")
            rows.append(row)
            if len(rows) > 5000:
                raise ValueError("单班测量表最多 5000 行。")
    except csv.Error as error:
        raise ValueError("CSV 引号或字段格式有误。") from error
    if not rows:
        raise ValueError("CSV 需要至少一条测量记录。")
    return rows


def normalize_measurements(
    rows: Iterable[dict[str, Any]],
    kind: str,
    *,
    min_overlap_minutes: float = MIN_OVERLAP_MINUTES,
) -> list[dict[str, Any]]:
    """把一班测量表规范化成窗口记录。

    ``kind`` 为 ``"baseline"``（原垫）或 ``"trial"``（试装垫），只影响报错措辞
    和热损字段的兜底别名。缺少站点、窗口或热损时抛 ``ValueError``。
    """
    if kind not in ("baseline", "trial"):
        raise ValueError("测量表种类只能是 baseline（原垫）或 trial（试装垫）。")
    pad_label = "原垫" if kind == "baseline" else "试装垫"
    normalized: list[dict[str, Any]] = []
    seen_records: set[tuple[str, str]] = set()
    for index, raw in enumerate(rows, start=2):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 行不是有效记录。")
        header_map = _build_header_map(raw.keys())

        def field(canonical: str) -> Any:
            actual = header_map.get(canonical)
            return raw.get(actual) if actual is not None else None

        station_value = field("station_id")
        station = str(station_value).strip() if station_value not in (None, "") else ""
        if not station:
            raise ValueError(f"测量表第 {index} 行缺少换热站编号，请补回后再交表。")

        record_value = field("record_id")
        record_id = str(record_value).strip() if record_value not in (None, "") else ""

        start = parse_window(field("window_start")) if field("window_start") not in (None, "") else None
        if start is None:
            raise ValueError(f"{station} 第 {index} 行缺少测量窗口起点。")
        end_value = field("window_end")
        end: datetime | None
        if end_value not in (None, ""):
            end = parse_window(end_value)
        else:
            duration = _parse_duration_minutes(field("duration_minutes"))
            if duration is None:
                raise ValueError(
                    f"{station} 第 {index} 行缺少窗口终点或窗口时长，无法计算半小时重叠。"
                )
            end = start + timedelta(minutes=duration)
        if end <= start:
            raise ValueError(f"{station} 第 {index} 行窗口终点必须晚于起点，请核对时间。")

        # 两班各自的规范热损键优先，其次通用 loss 别名。
        loss: float | None = None
        specific = raw.get(f"{kind}_loss_kw")
        if specific not in (None, ""):
            loss = _parse_loss(specific, pad_label, f"{station} 第 {index} 行，")
        else:
            loss_value = field("loss_kw")
            if loss_value in (None, ""):
                raise ValueError(f"{station} 第 {index} 行缺少{pad_label}平均热损（kW）。")
            loss = _parse_loss(loss_value, pad_label, f"{station} 第 {index} 行，")

        protocol_value = field("protocol_id")
        protocol_id = str(protocol_value).strip() if protocol_value not in (None, "") else PROTOCOL_ID

        # 完全相同的测量记录去重，避免重复导入制造假并列；同号但内容冲突则报错。
        identity = record_id or f"{station}|{start:{WINDOW_FORMAT}}|{end:{WINDOW_FORMAT}}|{loss}"
        dedup_key = (station.casefold(), identity.casefold())
        signature = f"{start.isoformat()}|{end.isoformat()}|{loss!r}"
        if dedup_key in seen_records:
            prior = next(
                (item for item in normalized
                 if item["station_key"] == station.casefold()
                 and (item["record_id"] or item["identity"]) .casefold() == identity.casefold()),
                None,
            )
            if prior is not None and prior["signature"] != signature:
                raise ValueError(
                    f"{station} 的记录编号 {identity} 出现互相冲突的窗口或热损，请先核对班组原表。"
                )
            continue
        seen_records.add(dedup_key)

        normalized.append({
            "station_id": station,
            "station_key": station.casefold(),
            "record_id": record_id,
            "identity": identity,
            "protocol_id": protocol_id or PROTOCOL_ID,
            "start": start,
            "end": end,
            "loss_kw": loss,
            "signature": signature,
            "line_number": index,
        })
    if not normalized:
        raise ValueError(f"{pad_label}测量表没有有效记录。")
    return normalized


def overlap_minutes(
    start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime
) -> float:
    """两个半开时间窗的重叠分钟数，不重叠返回 0。"""
    latest_start = max(start_a, start_b)
    earliest_end = min(end_a, end_b)
    delta = (earliest_end - latest_start).total_seconds() / 60.0
    return delta if delta > 0 else 0.0


def _natural_key(value: str) -> list[Any]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value)]


def _candidate(station: str, base: dict[str, Any], trial: dict[str, Any], overlap: float) -> dict[str, Any]:
    overlap_start = max(base["start"], trial["start"])
    overlap_end = min(base["end"], trial["end"])
    difference = float(Decimal(str(trial["loss_kw"])) - Decimal(str(base["loss_kw"])))
    return {
        "station_id": station,
        "overlap_minutes": round(overlap, 6),
        "window_start": overlap_start.strftime(WINDOW_FORMAT),
        "window_end": overlap_end.strftime(WINDOW_FORMAT),
        "baseline_record_id": base["record_id"],
        "trial_record_id": trial["record_id"],
        "baseline_window_start": base["start"].strftime(WINDOW_FORMAT),
        "baseline_window_end": base["end"].strftime(WINDOW_FORMAT),
        "trial_window_start": trial["start"].strftime(WINDOW_FORMAT),
        "trial_window_end": trial["end"].strftime(WINDOW_FORMAT),
        "baseline_loss_kw": base["loss_kw"],
        "trial_loss_kw": trial["loss_kw"],
        "difference_kw": difference,
        "_overlap_raw": overlap,
        "_baseline": base,
        "_trial": trial,
    }


def pair_windows(
    baseline_rows: Iterable[dict[str, Any]],
    trial_rows: Iterable[dict[str, Any]],
    *,
    min_overlap_minutes: float = MIN_OVERLAP_MINUTES,
    normalize: bool = True,
) -> dict[str, Any]:
    """按最长重叠为每座站选择窗口对，并保留并列候选。

    返回结构里 ``plans`` 是甲、乙两套完整配对（可直接喂给
    :func:`src.services.domain.compute`）；``stations`` 保留每站的全部最久并列项。
    """
    if normalize:
        baseline = normalize_measurements(baseline_rows, "baseline")
        trial = normalize_measurements(trial_rows, "trial")
    else:
        baseline = list(baseline_rows)
        trial = list(trial_rows)

    base_by_station: dict[str, list[dict[str, Any]]] = {}
    trial_by_station: dict[str, list[dict[str, Any]]] = {}
    display_name: dict[str, str] = {}
    for item in baseline:
        key = item["station_key"]
        base_by_station.setdefault(key, []).append(item)
        display_name.setdefault(key, item["station_id"])
    for item in trial:
        key = item["station_key"]
        trial_by_station.setdefault(key, []).append(item)
        # 原垫表的写法优先作为展示名；只有试装表有时用试装表写法。
        display_name.setdefault(key, item["station_id"])

    station_keys = sorted(display_name, key=lambda key: _natural_key(display_name[key]))
    stations: list[dict[str, Any]] = []
    plan_a: list[dict[str, Any]] = []
    plan_b: list[dict[str, Any]] = []
    unpaired: list[dict[str, Any]] = []

    pair_index = 0
    for key in station_keys:
        station = display_name[key]
        bases = base_by_station.get(key, [])
        trials = trial_by_station.get(key, [])
        if not bases or not trials:
            side = "试装垫表" if bases else "原垫表"
            unpaired.append({
                "station_id": station,
                "reason": f"只有{'原垫' if not bases else '试装垫'}记录，{side}缺少同站窗口。",
            })
            continue

        candidates: list[dict[str, Any]] = []
        for base in bases:
            for item in trials:
                overlap = overlap_minutes(base["start"], base["end"], item["start"], item["end"])
                if overlap + _OVERLAP_EPS >= min_overlap_minutes:
                    candidates.append(_candidate(station, base, item, overlap))
        # 确定性排序：重叠长的优先，再按双方记录/时间兜底，保证两套配法稳定。
        candidates.sort(key=lambda cand: (
            -cand["_overlap_raw"],
            cand["baseline_record_id"] or cand["baseline_window_start"],
            cand["trial_record_id"] or cand["trial_window_start"],
            cand["baseline_window_start"],
            cand["trial_window_start"],
        ))
        if not candidates:
            longest = 0.0
            for base in bases:
                for item in trials:
                    longest = max(longest, overlap_minutes(
                        base["start"], base["end"], item["start"], item["end"]))
            unpaired.append({
                "station_id": station,
                "reason": (
                    f"同站窗口最长重叠 {longest:.0f} 分钟，"
                    f"未达到 {min_overlap_minutes:g} 分钟下限。"
                ),
                "longest_overlap_minutes": round(longest, 6),
            })
            continue

        best = candidates[0]["_overlap_raw"]
        tied = [cand for cand in candidates if abs(cand["_overlap_raw"] - best) <= _OVERLAP_EPS]
        options = tied  # 已按确定性键排序

        def to_plan_row(cand: dict[str, Any], index: int) -> dict[str, Any]:
            return {
                "protocol_id": cand["_baseline"].get("protocol_id", PROTOCOL_ID),
                "pair_id": f"PAD-{index:03d}",
                "station_id": station,
                "window_start": cand["window_start"],
                "baseline_loss_kw": cand["baseline_loss_kw"],
                "trial_loss_kw": cand["trial_loss_kw"],
                "window_end": cand["window_end"],
                "overlap_minutes": cand["overlap_minutes"],
                "baseline_record_id": cand["baseline_record_id"],
                "trial_record_id": cand["trial_record_id"],
            }

        pair_index += 1
        row_a = to_plan_row(options[0], pair_index)
        row_b = to_plan_row(options[1] if len(options) >= 2 else options[0], pair_index)
        plan_a.append(row_a)
        plan_b.append(row_b)

        public_options = [{k: v for k, v in option.items() if not k.startswith("_")}
                          for option in options]
        stations.append({
            "station_id": station,
            "overlap_minutes": options[0]["overlap_minutes"],
            "tied": len(options) >= 2,
            "tie_count": len(options),
            "selected": public_options[0],
            "options": public_options,
        })

    tie_stations = [item["station_id"] for item in stations if item["tied"]]
    return {
        "min_overlap_minutes": min_overlap_minutes,
        "stations": stations,
        "unpaired_stations": unpaired,
        "tie_stations": tie_stations,
        "tie_count": len(tie_stations),
        "paired_station_count": len(stations),
        "plans": [plan_a, plan_b],
        # 语义化别名，方便调用方按「甲/乙两套配对」取用。
        "plan_a": plan_a,
        "plan_b": plan_b,
    }


# 便于按英文函数名猜测的调用方。
build_pairings = pair_windows
match_windows = pair_windows
select_pairs = pair_windows
pair_measurements = pair_windows
pair_tables = pair_windows
build_pairs = pair_windows
select_station_pairs = pair_windows
find_best_pairs = pair_windows
resolve_pairings = pair_windows
match_overlap_windows = pair_windows
pair_shift_tables = pair_windows
build_overlap_pairs = pair_windows
select_overlap_pairs = pair_windows
assemble_shift_pairs = pair_windows


def tables_to_plans(
    baseline_csv: str | list[dict[str, Any]],
    trial_csv: str | list[dict[str, Any]],
    *,
    min_overlap_minutes: float = MIN_OVERLAP_MINUTES,
) -> dict[str, Any]:
    """CSV 文本（或字典行）到两套配对的一站式入口。"""
    baseline_rows = read_measurement_csv(baseline_csv) if isinstance(baseline_csv, str) else baseline_csv
    trial_rows = read_measurement_csv(trial_csv) if isinstance(trial_csv, str) else trial_csv
    return pair_windows(baseline_rows, trial_rows, min_overlap_minutes=min_overlap_minutes)
