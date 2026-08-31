"""Generate the two durable evaluator comparison figures.

The figures are deliberately kept on separate canvases: the official
evaluator has 200 public-set samples, while the natural-language evaluator
has an independent frozen set of 100 samples. The script uses only the
persisted report values and Python's standard library; it never runs an
evaluation or contacts a model service. SVGs are always generated directly;
on macOS, PNG compatibility outputs are rendered with the built-in
``qlmanage`` workspace renderer through a disposable square wrapper so tall
canvases are not clipped.
"""

from __future__ import annotations

from html import escape
from pathlib import Path
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET


OUT = Path(__file__).resolve().parent
WIDTH = 1200
PNG_SIZES = {
    "official_evaluator_comparison_200.svg": 1600,
    "smart_evaluator_comparison_100.svg": 1800,
}
SVG_NS = "http://www.w3.org/2000/svg"
INK = "#17202A"
MUTED = "#60717C"
GRID = "#D9E1E6"
GRID_LIGHT = "#EEF2F4"
OFFICIAL = "#D97724"
DEEPSEEK = "#2563A6"
DEEPSEEK_LIGHT = "#79A9D6"
PANEL = "#FFFFFF"
BACKGROUND = "#F7F9FA"
MONO = "&quot;SF Mono&quot;,&quot;Roboto Mono&quot;,monospace"
FONT = "-apple-system,BlinkMacSystemFont,&quot;Segoe UI&quot;,&quot;PingFang SC&quot;,&quot;Noto Sans CJK SC&quot;,sans-serif"


def text(
    x: float,
    y: float,
    value: object,
    size: int = 14,
    fill: str = INK,
    anchor: str = "start",
    weight: int = 400,
    family: str = FONT,
) -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" '
        f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}" '
        f'font-family="{family}">{escape(str(value))}</text>'
    )


def rect(
    x: float,
    y: float,
    width: float,
    height: float,
    fill: str,
    stroke: str = "none",
    radius: float = 0,
) -> str:
    return (
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" '
        f'height="{height:.1f}" rx="{radius:.1f}" fill="{fill}" '
        f'stroke="{stroke}"/>'
    )


def line(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    stroke: str = GRID,
    width: float = 1,
    dash: str | None = None,
) -> str:
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" '
        f'y2="{y2:.1f}" stroke="{stroke}" stroke-width="{width:.1f}"'
        f"{dash_attr}/>"
    )


def circle(
    x: float,
    y: float,
    radius: float,
    fill: str,
    stroke: str = "#FFFFFF",
    width: float = 2,
) -> str:
    return (
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{width:.1f}"/>'
    )


def start(height: int, title: str, description: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
        f'height="{height}" viewBox="0 0 {WIDTH} {height}" '
        'role="img" aria-labelledby="title desc">',
        f'<title id="title">{escape(title)}</title>',
        f'<desc id="desc">{escape(description)}</desc>',
        rect(0, 0, WIDTH, height, BACKGROUND),
    ]


def axis(
    parts: list[str],
    x0: float,
    x1: float,
    y: float,
    maximum: float,
    ticks: tuple[float, ...],
    formatter: str,
) -> None:
    parts.append(line(x0, y, x1, y, "#8A989F", 1.2))
    for tick in ticks:
        x = x0 + (x1 - x0) * tick / maximum
        parts.extend(
            [
                line(x, y - 5, x, y + 5, "#8A989F"),
                text(x, y + 23, format(tick, formatter), 11, MUTED, "middle", family=MONO),
            ]
        )


def scale(value: float, x0: float, x1: float, maximum: float) -> float:
    return x0 + (x1 - x0) * value / maximum


def dumbbell(
    parts: list[str],
    label: str,
    before: float,
    after: float,
    y: float,
    x0: float,
    x1: float,
    maximum: float,
    digits: int = 3,
    delta_digits: int | None = None,
    inverse: bool = False,
) -> None:
    before_x = scale(before, x0, x1, maximum)
    after_x = scale(after, x0, x1, maximum)
    left, right = sorted((before_x, after_x))
    delta = after - before
    delta_digits = digits if delta_digits is None else delta_digits
    delta_label = f"{delta:+.{delta_digits}f}"
    delta_fill = DEEPSEEK if (delta < 0 if inverse else delta > 0) else "#B5473C"
    parts.extend(
        [
            text(x0 - 24, y + 6, label, 15, INK, "end", 650),
            line(left, y, right, y, "#AAB6BD", 4),
            circle(before_x, y, 8, OFFICIAL),
            circle(after_x, y, 8, DEEPSEEK),
            text(before_x, y - 15, f"{before:.{digits}f}", 12, INK, "middle", 650, MONO),
            text(after_x, y + 27, f"{after:.{digits}f}", 12, INK, "middle", 650, MONO),
            text(x1 + 18, y + 6, delta_label, 13, delta_fill, family=MONO, weight=700),
        ]
    )


def quality_panel(
    parts: list[str],
    quality: list[tuple[str, float, float]],
    mttc: tuple[float, float],
) -> None:
    panel_x, panel_y, panel_w, panel_h = 42, 154, 1116, 430
    parts.extend(
        [
            rect(panel_x, panel_y, panel_w, panel_h, PANEL, GRID, 16),
            text(66, 190, "总体质量", 21, INK, weight=700),
            text(66, 215, "同一 evaluator 内的成对变化；分数越高越好，MTTC 越低越好", 13, MUTED),
        ]
    )
    axis(parts, 255, 1010, 266, 1, (0, 0.25, 0.5, 0.75, 1), "g")
    for index, (label, before, after) in enumerate(quality):
        dumbbell(parts, label, before, after, 309 + index * 52, 255, 1010, 1)

    separator_y = 309 + (len(quality) - 1) * 52 + 31
    parts.extend(
        [
            line(70, separator_y, 1130, separator_y, GRID, 1, "4 5"),
            text(70, separator_y + 23, "MTTC · turns", 13, MUTED, weight=700),
        ]
    )
    mttc_y = separator_y + 57
    dumbbell(
        parts,
        "首命中轮数",
        mttc[0],
        mttc[1],
        mttc_y,
        255,
        1010,
        11,
        digits=3,
        delta_digits=3,
        inverse=True,
    )
    axis(parts, 255, 1010, mttc_y + 30, 11, (0, 3, 6, 9, 11), "g")


def scenario_cell(
    parts: list[str],
    x0: float,
    x1: float,
    y: float,
    before: float,
    after: float,
    maximum: float,
    digits: int,
) -> None:
    before_x = scale(before, x0, x1, maximum)
    after_x = scale(after, x0, x1, maximum)
    parts.extend(
        [
            line(x0, y, x1, y, GRID, 1),
            line(min(before_x, after_x), y, max(before_x, after_x), y, "#AAB6BD", 3),
            circle(before_x, y - 3, 6, OFFICIAL),
            circle(after_x, y + 3, 6, DEEPSEEK),
            text(x0, y + 23, f"O {before:.{digits}f}", 10, MUTED, family=MONO),
            text(x1, y + 23, f"D {after:.{digits}f}", 10, MUTED, "end", family=MONO),
        ]
    )


def scenarios_panel(
    parts: list[str],
    scenarios: list[tuple[str, int, tuple[tuple[float, float], ...]]],
) -> int:
    panel_y = 602
    panel_h = 400 if len(scenarios) <= 4 else 616
    parts.extend(
        [
            rect(42, panel_y, 1116, panel_h, PANEL, GRID, 16),
            text(66, panel_y + 36, "场景小多图", 21, INK, weight=700),
            text(66, panel_y + 61, "每个单元格是 Official → DeepSeek；不同指标使用独立坐标", 13, MUTED),
        ]
    )
    columns = ((230, 485), (545, 800), (860, 1115))
    headers = ("Hit@10", "Exact Top-1" if len(scenarios) > 4 else "MRR", "MTTC · 越低越好")
    for (x0, x1), header in zip(columns, headers):
        parts.extend(
            [
                text((x0 + x1) / 2, panel_y + 91, header, 13, INK, "middle", 700),
                line(x0, panel_y + 101, x1, panel_y + 101, GRID),
            ]
        )

    for index, (name, count, values) in enumerate(scenarios):
        y = panel_y + 136 + index * 72
        parts.extend(
            [
                text(62, y - 2, name, 13, INK, weight=650),
                text(62, y + 18, f"n={count}", 11, MUTED),
                line(62, y + 34, 1130, y + 34, GRID_LIGHT),
            ]
        )
        for column, ((before, after), maximum) in enumerate(zip(values, (1, 1, 11))):
            digits = 2 if column == 2 else 3
            scenario_cell(parts, columns[column][0], columns[column][1], y, before, after, maximum, digits)
    return panel_y + panel_h


def latency_pair(
    parts: list[str],
    label: str,
    before: float,
    after: float,
    y: float,
    formatter: str,
) -> None:
    x0, x1 = 190, 515
    maximum = max(before, after) * 1.12 or 1
    before_x = scale(before, x0, x1, maximum)
    after_x = scale(after, x0, x1, maximum)
    parts.extend(
        [
            text(160, y + 5, label, 12, INK, "end", 600),
            line(x0, y, x1, y, GRID),
            line(min(before_x, after_x), y, max(before_x, after_x), y, "#AAB6BD", 3),
            circle(before_x, y, 7, OFFICIAL),
            circle(after_x, y, 7, DEEPSEEK),
            text(before_x, y - 14, format(before, formatter), 10, INK, "middle", family=MONO),
            text(after_x, y + 24, format(after, formatter), 10, INK, "middle", family=MONO),
        ]
    )


def resources_panel(
    parts: list[str],
    panel_y: int,
    wall: tuple[float, float],
    per_question: tuple[float, float],
    per_turn: tuple[float, float],
    total_tokens: tuple[int, int],
    token_parts: tuple[tuple[int, ...], tuple[int, ...]],
    costs: tuple[float, float],
    fallback_note: str,
) -> None:
    parts.extend(
        [
            rect(42, panel_y, 1116, 360, PANEL, GRID, 16),
            text(66, panel_y + 36, "资源消耗", 21, INK, weight=700),
            text(66, panel_y + 61, "秒、Token、美元分轴展示；成本为峰值 cache 假设区间", 13, MUTED),
            text(70, panel_y + 96, "延迟", 15, INK, weight=700),
        ]
    )
    latency_pair(parts, "每题", per_question[0], per_question[1], panel_y + 132, ".2f")
    latency_pair(parts, "每轮", per_turn[0], per_turn[1], panel_y + 180, ".2f")
    parts.append(text(70, panel_y + 238, f"总 wall time：{wall[0]:,.2f}s → {wall[1]:,.2f}s", 11, MUTED))

    parts.extend(
        [
            text(595, panel_y + 96, "Reported tokens", 15, INK, weight=700),
            text(675, panel_y + 135, "Official", 11, INK, "end"),
            rect(690, panel_y + 119, 410, 18, GRID_LIGHT, radius=4),
            text(698, panel_y + 133, f"{total_tokens[0]:,}", 10, MUTED, family=MONO),
            text(675, panel_y + 179, "DeepSeek", 11, INK, "end"),
            rect(690, panel_y + 163, 410, 18, GRID_LIGHT, radius=4),
        ]
    )
    token_max = max(total_tokens) * 1.02 or 1
    token_width = 410 * total_tokens[1] / token_max
    cursor = 690.0
    colors = (DEEPSEEK, DEEPSEEK_LIGHT, "#A8BBCB")
    for value, color in zip(token_parts[1], colors):
        width = token_width * value / total_tokens[1] if total_tokens[1] else 0
        if width:
            parts.append(rect(cursor, panel_y + 163, width, 18, color, radius=2))
            cursor += width
    parts.extend(
        [
            text(1100, panel_y + 199, f"{total_tokens[1]:,}", 10, INK, "end", family=MONO),
            rect(690, panel_y + 218, 12, 12, DEEPSEEK, radius=2),
            text(710, panel_y + 229, "prompt", 10, MUTED),
            rect(775, panel_y + 218, 12, 12, DEEPSEEK_LIGHT, radius=2),
            text(795, panel_y + 229, "completion", 10, MUTED),
            text(595, panel_y + 256, fallback_note, 10, MUTED),
            text(70, panel_y + 285, "峰值成本区间", 15, INK, weight=700),
            text(160, panel_y + 307, "DeepSeek", 12, INK, "end", 600),
        ]
    )
    cost_x0, cost_x1 = 240, 1080
    cost_max = max(costs) * 1.05 or 1
    start_x = scale(costs[0], cost_x0, cost_x1, cost_max)
    end_x = scale(costs[1], cost_x0, cost_x1, cost_max)
    parts.extend(
        [
            line(cost_x0, panel_y + 302, cost_x1, panel_y + 302, GRID),
            line(start_x, panel_y + 302, end_x, panel_y + 302, DEEPSEEK, 6),
            circle(start_x, panel_y + 302, 7, DEEPSEEK),
            circle(end_x, panel_y + 302, 7, DEEPSEEK),
            text(start_x, panel_y + 287, f"${costs[0]:.3f} · 全 hit", 10, INK, "middle", family=MONO),
            text(end_x, panel_y + 287, f"${costs[1]:.3f} · 全 miss", 10, INK, "middle", family=MONO),
            text(70, panel_y + 341, "Official 离线：$0", 11, MUTED),
        ]
    )


def figure(
    filename: str,
    title: str,
    badge: str,
    subtitle: str,
    quality: list[tuple[str, float, float]],
    mttc: tuple[float, float],
    scenarios: list[tuple[str, int, tuple[tuple[float, float], ...]]],
    wall: tuple[float, float],
    per_question: tuple[float, float],
    per_turn: tuple[float, float],
    total_tokens: tuple[int, int],
    token_parts: tuple[tuple[int, ...], tuple[int, ...]],
    costs: tuple[float, float],
    fallback_note: str,
    footer: str,
) -> None:
    height = 1540 if len(scenarios) <= 4 else 1750
    parts = start(height, title, subtitle)
    parts.extend(
        [
            rect(42, 28, 250, 34, INK, radius=17),
            text(167, 51, badge, 14, "#FFFFFF", "middle", 700),
            text(42, 101, title, 34, INK, weight=750),
            text(42, 132, subtitle, 15, MUTED),
            circle(884, 51, 8, OFFICIAL, OFFICIAL),
            text(902, 57, "Official 离线", 14, INK, weight=600),
            circle(1040, 51, 8, DEEPSEEK, DEEPSEEK),
            text(1058, 57, "DeepSeek", 14, INK, weight=600),
        ]
    )
    quality_panel(parts, quality, mttc)
    scenario_end = scenarios_panel(parts, scenarios)
    resources_panel(parts, scenario_end + 18, wall, per_question, per_turn, total_tokens, token_parts, costs, fallback_note)
    parts.extend(
        [
            text(42, height - 38, footer, 12, MUTED),
            text(1158, height - 38, "2026-08-31", 12, MUTED, "end", family=MONO),
            "</svg>",
        ]
    )
    (OUT / filename).write_text("\n".join(parts) + "\n", encoding="utf-8")


def render_png(svg_name: str, size: int) -> None:
    """Render one tall SVG to a square PNG using the workspace renderer.

    Quick Look's direct SVG thumbnail path clips tall canvases to a square.
    The temporary wrapper gives it a square outer canvas and places the
    original SVG contents in a centered nested ``svg`` with its original
    ``viewBox``. The wrapper is disposable; only the canonical PNG is kept.
    """
    renderer = shutil.which("qlmanage")
    if renderer is None:
        raise RuntimeError(
            "Cannot render evaluator PNGs: macOS Quick Look renderer "
            "'qlmanage' was not found on PATH."
        )

    source = OUT / svg_name
    destination = source.with_suffix(".png")
    ET.register_namespace("", SVG_NS)
    root = ET.parse(source).getroot()
    view_box = root.get("viewBox")
    if not view_box:
        raise ValueError(f"{source} does not declare an SVG viewBox")
    view_box_values = view_box.replace(",", " ").split()
    if len(view_box_values) != 4:
        raise ValueError(f"{source} has an invalid SVG viewBox: {view_box!r}")
    try:
        view_width = float(view_box_values[2])
        view_height = float(view_box_values[3])
    except ValueError as exc:
        raise ValueError(f"{source} has a non-numeric SVG viewBox: {view_box!r}") from exc
    if view_width <= 0 or view_height <= 0:
        raise ValueError(f"{source} has a non-positive SVG viewBox: {view_box!r}")

    inner = "".join(ET.tostring(child, encoding="unicode") for child in root)
    wrapper = (
        f'<svg xmlns="{SVG_NS}" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}" role="img">'
        f'<svg x="0" y="0" width="{size}" height="{size}" '
        f'viewBox="{escape(view_box)}" preserveAspectRatio="xMidYMid meet">'
        f"{inner}</svg></svg>"
    )

    with tempfile.TemporaryDirectory(prefix="evaluator-chart-render-") as temp_dir:
        temp = Path(temp_dir)
        wrapper_path = temp / source.name
        wrapper_path.write_text(wrapper + "\n", encoding="utf-8")
        result = subprocess.run(
            [renderer, "-t", "-s", str(size), "-o", str(temp), str(wrapper_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or "no renderer output"
            raise RuntimeError(f"qlmanage failed for {source.name}: {detail}")

        rendered = temp / f"{wrapper_path.name}.png"
        if not rendered.is_file():
            candidates = sorted(temp.glob("*.png"))
            if len(candidates) == 1:
                rendered = candidates[0]
        if not rendered.is_file():
            raise RuntimeError(f"qlmanage did not produce a PNG for {source.name}")
        shutil.copyfile(rendered, destination)


OFFICIAL_SCENARIOS = [
    ("Buying", 80, ((1, .9375), (.8127, .5718), (2.55, 3.69))),
    ("Browsing", 80, ((1, .9), (.7768, .5616), (2.75, 5.56))),
    ("Intent Override", 30, ((1, .8667), (.8214, .6676), (4.73, 6.93))),
    ("Boundary", 10, ((1, 1), (.92, .5058), (3.5, 4.6))),
]

SMART_SCENARIOS = [
    ("Budget Rating", 14, ((.4286, .7143), (.0714, .2857), (8.5, 4.29))),
    ("Clarification Required", 14, ((.7143, .7143), (.1429, .4286), (6.79, 4.43))),
    ("Direct Search", 15, ((.8, 1), (.6, .9333), (3.73, 1))),
    ("Intent Override", 14, ((.4286, .8571), (.2143, .7143), (7.5, 3.29))),
    ("Multi Constraint", 15, ((.8667, .8), (.6, .5333), (5.8, 3.53))),
    ("Negative Constraint", 14, ((.6429, .7857), (.2143, .3571), (7.36, 3.36))),
    ("Profile Hidden", 14, ((.1429, .5), (0, .2857), (10.21, 6.36))),
]


def main() -> None:
    figure(
        "official_evaluator_comparison_200.svg",
        "官方 evaluator · 200 题",
        "OFFICIAL EVALUATOR · N=200",
        "public_set.jsonl · 4 类官方协议场景 · exact parent_asin",
        [("Hit@10", 1, .915), ("Exact Top-1", .695, .45), ("MRR", .805024, .578792), ("技术分", .901407, .751738)],
        (3.005, 4.970),
        OFFICIAL_SCENARIOS,
        (223.62, 1188.66),
        (1.1181, 5.9433),
        (.3721, 1.2166),
        (0, 1941893),
        ((0,), (1941893,)),
        (.086, .894),
        "官方结果只保存合并 usage；无 backend/cache 明细",
        "只在官方 evaluator 内比较两种配置；不与智能自然语言 benchmark 横向排名。",
    )
    figure(
        "smart_evaluator_comparison_100.svg",
        "智能自然语言 evaluator v2 · 冻结 100 题",
        "NATURAL-LANGUAGE V2 · N=100",
        "7 类 target-exact 自然语言场景 · simulator_version 2",
        [("Hit@10", .58, .77), ("Exact Top-1", .27, .51), ("MRR", .365913, .605179)],
        (7.08, 3.72),
        SMART_SCENARIOS,
        (447.01, 677.93),
        (4.4701, 6.7793),
        (.8887, 1.9043),
        (0, 924200),
        ((0,), (904254, 17793, 2153)),
        (.036, .421),
        "总量含 2,153 本地 fallback token；成本仅按 DeepSeek 归属 token",
        "4 次 DeepSeek validator 失败用量未计入；只在本图内比较两种配置。",
    )
    for svg_name, size in PNG_SIZES.items():
        render_png(svg_name, size)


if __name__ == "__main__":
    main()
