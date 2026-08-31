"""Generate the two figures used by the final Devpost write-up.

The figures intentionally keep the two evaluation protocols on separate
canvases.  Numbers are copied from the persisted baseline/report records; no
evaluation is performed by this script.
"""
from __future__ import annotations

from html import escape
from pathlib import Path


OUT = Path(__file__).resolve().parent
INK = "#17324D"
MUTED = "#60717C"
GRID = "#D7E0E6"
BASE = "#98A6B3"
BLUE = "#1479C9"
BLUE_LIGHT = "#DCEEFF"
GREEN = "#198754"
RED = "#C44747"
BG = "#FFFFFF"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Noto Sans CJK SC',sans-serif"
MONO = "'SF Mono','Roboto Mono',monospace"


def esc(value: object) -> str:
    return escape(str(value))


def text(x: float, y: float, value: object, size: int = 14, fill: str = INK,
         anchor: str = "start", weight: int = 400, family: str = FONT) -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{family}" '
            f'font-size="{size}" fill="{fill}" text-anchor="{anchor}" '
            f'font-weight="{weight}">{esc(value)}</text>')


def rect(x: float, y: float, width: float, height: float, fill: str,
         stroke: str = "none", radius: int = 0) -> str:
    return (f'<rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" '
            f'height="{height:.1f}" rx="{radius}" fill="{fill}" stroke="{stroke}"/>')


def line(x1: float, y1: float, x2: float, y2: float, stroke: str = GRID,
         width: float = 1, dash: str | None = None) -> str:
    extra = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" '
            f'y2="{y2:.1f}" stroke="{stroke}" stroke-width="{width}"{extra}/>')


def circle(x: float, y: float, radius: float, fill: str, stroke: str = BG,
           width: float = 2) -> str:
    return (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{width}"/>')


def canvas(width: int, height: int, title: str, description: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img">',
        f'<title>{esc(title)}</title>',
        f'<desc>{esc(description)}</desc>',
        rect(0, 0, width, height, BG),
    ]


def axis(p: list[str], x0: float, x1: float, y: float, maximum: float,
         ticks: list[float], formatter: str = ".2f") -> None:
    p.append(line(x0, y, x1, y, MUTED, 1))
    for tick in ticks:
        x = x0 + (x1 - x0) * tick / maximum
        p.extend([line(x, y - 4, x, y + 4, MUTED, 1),
                  text(x, y + 20, format(tick, formatter), 11, MUTED, "middle", family=MONO)])


def paired_bars(p: list[str], rows: list[tuple[str, float, float]], x0: float,
                x1: float, y0: float, step: float, maximum: float,
                ticks: list[float], value_format: str = ".3f") -> None:
    axis(p, x0, x1, y0 - 25, maximum, ticks)
    for i, (label, before, after) in enumerate(rows):
        y = y0 + i * step
        p.append(text(x0 - 18, y + 14, label, 14, INK, "end", 600))
        p.extend([rect(x0, y, (x1 - x0) * before / maximum, 11, BASE, radius=2),
                  rect(x0, y + 16, (x1 - x0) * after / maximum, 11, BLUE, radius=2),
                  text(x0 + (x1 - x0) * before / maximum + 8, y + 10,
                       format(before, value_format), 11, MUTED, family=MONO),
                  text(x0 + (x1 - x0) * after / maximum + 8, y + 26,
                       format(after, value_format), 11, BLUE, family=MONO)])


def write(name: str, parts: list[str]) -> None:
    parts.append("</svg>")
    (OUT / name).write_text("\n".join(parts) + "\n", encoding="utf-8")


def official() -> None:
    p = canvas(1120, 700, "官方 evaluator：确定性架构的高分结果",
               "Weak starter baseline compared with the final offline deterministic agent on 200 official samples.")
    p.extend([
        text(60, 58, "官方 evaluator：确定性架构已经足够高分", 25, INK, weight=700),
        text(60, 84, "同一公开集、同一评分协议；只展示从弱基线到最终离线 Agent 的提升", 13, MUTED),
        rect(875, 45, 12, 10, BASE, radius=2), text(895, 55, "Weak starter", 12, MUTED),
        rect(1010, 45, 12, 10, BLUE, radius=2), text(1030, 55, "Final offline", 12, BLUE),
        text(60, 132, "质量指标", 17, INK, weight=700),
        text(60, 155, "数值越高越好；MTTC 单独使用 turns 坐标，数值越低越好。", 12, MUTED),
    ])
    paired_bars(p, [
        ("Hit@10", .125, 1.000),
        ("MRR", .068034, .805024),
        ("Technical score", .106710, .901407),
    ], 285, 930, 195, 58, 1, [0, .25, .5, .75, 1], ".3f")
    p.extend([
        text(60, 395, "命中速度", 17, INK, weight=700),
        text(60, 418, "平均首次命中轮数（MTTC）从 9.81 降至 3.005。", 12, MUTED),
    ])
    paired_bars(p, [("MTTC (turns)", 9.81, 3.005)], 285, 930, 455, 58, 11, [0, 3, 6, 9, 11], ".3f")
    p.extend([
        text(60, 565, "四类官方场景的 Hit@10", 17, INK, weight=700),
        text(60, 588, "Buying、Browsing、Intent Override、Boundary 均为 100%。", 12, MUTED),
    ])
    scenarios = [("Buying", 80), ("Browsing", 80), ("Intent Override", 30), ("Boundary", 10)]
    x0, x1 = 285, 930
    for i, (name, count) in enumerate(scenarios):
        y = 615 + i * 18
        p.extend([text(x0 - 18, y + 10, f"{name} (n={count})", 11, INK, "end"),
                  rect(x0, y, x1 - x0, 10, BLUE_LIGHT, radius=2),
                  rect(x0, y, x1 - x0, 10, BLUE, radius=2),
                  text(x1 + 12, y + 10, "1.000", 11, BLUE, family=MONO)])
    p.append(text(60, 688, "结论：官方协议下无需模型或网络，结构化状态、召回、排序与提交策略即可稳定完成评分。", 11, MUTED))
    write("official_score_progression.svg", p)


def natural() -> None:
    p = canvas(1120, 940, "自然语言 evaluator：DeepSeek 的提升",
               "Official deterministic agent compared with the DeepSeek translation layer on 100 frozen natural-language samples.")
    p.extend([
        text(60, 58, "自然语言 evaluator：DeepSeek 显著改善交互理解", 25, INK, weight=700),
        text(60, 84, "独立冻结集 N=100 · 7 类自然语言场景 · 仅在本 evaluator 内比较", 13, MUTED),
        rect(885, 45, 12, 10, BASE, radius=2), text(905, 55, "Official", 12, MUTED),
        rect(1010, 45, 12, 10, BLUE, radius=2), text(1030, 55, "+ DeepSeek", 12, BLUE),
        text(60, 132, "总体质量", 17, INK, weight=700),
        text(60, 155, "DeepSeek 只负责自然语言翻译；商品候选、状态修改和最终提交仍由确定性核心完成。", 12, MUTED),
    ])
    paired_bars(p, [
        ("Hit@10", .580, .770),
        ("Exact Top-1", .270, .510),
        ("MRR", .365913, .605179),
    ], 285, 930, 195, 58, 1, [0, .25, .5, .75, 1], ".3f")
    p.extend([
        text(60, 395, "命中速度", 17, INK, weight=700),
        text(60, 418, "平均 MTTC 从 7.08 降至 3.72（减少 3.36 轮）。", 12, MUTED),
    ])
    paired_bars(p, [("MTTC (turns)", 7.08, 3.72)], 285, 930, 455, 58, 11, [0, 3, 6, 9, 11], ".2f")
    p.extend([
        text(60, 565, "场景级 Hit@10：自然表达带来的收益", 17, INK, weight=700),
        text(60, 588, "条形末端标注 Official → DeepSeek；绿色表示提升，红色表示退化。", 12, MUTED),
    ])
    scenarios = [
        ("Budget rating", .429, .714),
        ("Clarification", .714, .714),
        ("Direct search", .800, 1.000),
        ("Intent override", .429, .857),
        ("Multi constraint", .867, .800),
        ("Negative constraint", .643, .786),
        ("Profile hidden", .143, .500),
    ]
    x0, x1 = 285, 930
    for i, (name, before, after) in enumerate(scenarios):
        y = 620 + i * 35
        xb = x0 + (x1 - x0) * before
        xa = x0 + (x1 - x0) * after
        delta = after - before
        color = GREEN if delta > .0005 else RED if delta < -.0005 else MUTED
        p.extend([
            text(x0 - 18, y + 5, name, 12, INK, "end"),
            line(x0, y, x1, y, GRID, 2),
            line(xb, y, xa, y, color, 5),
            circle(xb, y, 5, BASE), circle(xa, y, 5, BLUE),
            text(x1 + 12, y + 5, f"{before:.3f} → {after:.3f}", 11, color, family=MONO),
        ])
    p.extend([
        text(60, 885, "资源代价", 17, INK, weight=700),
        text(60, 908, "100 题运行：总耗时 447.01 → 677.93 s；reported tokens 0 → 924,200（DeepSeek 922,047 + fallback 2,153）；峰值估算成本约 $0.036–$0.421。", 12, MUTED),
        text(60, 932, "结论：DeepSeek 的价值集中在自然语言理解边界，而不是替代召回、排序或状态机。", 11, INK, weight=600),
    ])
    write("natural_language_deepseek_gain.svg", p)


if __name__ == "__main__":
    official()
    natural()
