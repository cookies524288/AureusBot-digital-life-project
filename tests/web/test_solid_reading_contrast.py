"""3.64 M：按发布 CSS 的实际文字、底色和稳定状态计算，不替代实屏取样。"""

import re
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[2] / "aureus/web/static/index.html"
type RGB = tuple[float, ...]


def _rules() -> dict[str, dict[str, str]]:
    css = PAGE.read_text(encoding="utf-8").split("<style>", 1)[1].split("</style>", 1)[0]
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules: dict[str, dict[str, str]] = {}
    for group, block in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        declarations = dict(item.strip().split(":", 1) for item in block.split(";") if ":" in item)
        for selector in re.split(r",(?![^()]*\))", group):
            rules.setdefault(selector.strip(), {}).update(declarations)
    return rules


RULES = _rules()


def _mix(first: RGB, weight: float, second: RGB) -> RGB:
    return tuple(a * weight + b * (1 - weight) for a, b in zip(first, second, strict=True))


def _rgb(value: str, palette: dict[str, str]) -> RGB:
    if value.startswith("var("):
        key, _, fallback = value[4:-1].partition(",")
        return _rgb(palette.get(key, fallback), palette)
    if value.startswith("color-mix("):
        match = re.fullmatch(r"color-mix\(in srgb,(var\([^)]+\)) (\d+)%,(var\([^)]+\))\)", value)
        assert match, value
        first, percent, second = match.groups()
        return _mix(_rgb(first, palette), float(percent) / 100, _rgb(second, palette))
    assert re.fullmatch(r"#(?:[\da-f]{3}|[\da-f]{6})", value), value
    digits = value[1:]
    if len(digits) == 3:
        digits = "".join(char * 2 for char in digits)
    return tuple(int(digits[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _luminance(rgb: RGB) -> float:
    linear = (v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb)
    return sum(v * w for v, w in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_solid_reading_paths(dark: bool) -> None:
    """每一项列出实际命中的规则；背景透明或文字继承时取承载它的父级。"""
    theme = ":root[data-theme='dark']" if dark else ":root:not([data-theme='dark'])"
    palette = RULES[":root"] | RULES[theme]
    failures = []

    def prop(name: str, *matching: str, default: str = "inherit") -> str:
        # 同一节点按层叠优先级列出，最后生效者胜；不假定在父级改色就能覆盖子级。
        value = default
        for selector in matching:
            value = RULES.get(selector, {}).get(name, value)
        return value

    def check(role: str, fg: str, bg: str, *, minimum: float = 4.5,
              opacity: float = 1, behind: str = "var(--surface)", brightness: float = 1) -> None:
        colors = [_mix(_rgb(paint, palette), opacity, _rgb(behind, palette)) for paint in (fg, bg)]
        low, high = sorted(_luminance(tuple(v * brightness for v in color)) for color in colors)
        ratio = (high + 0.05) / (low + 0.05)
        if ratio < minimum:
            failures.append(f"{role}: {ratio:.3f}:1 < {minimum}:1")

    def background(selector: str) -> str:
        return RULES[selector]["background"]

    bubble = ".msg.user .bubble"
    check("私聊和平台金底正文", prop("color", bubble), background(bubble))
    for child in (".inbox-component.muted", ".inbox-image>p.muted"):
        color = prop("color", ".muted", ".msg.user " + child)
        check(child, prop("color", bubble) if color == "inherit" else color, background(bubble))
    for selector in (".typing-status", ".chip", ".chip.on"):
        check(selector, prop("color", selector), background(selector))

    for active in (False, True):
        for hover in (False, True):
            row = [".provider-row"]
            if active:
                row.append(".provider-row.active")
            if hover:
                row.append(".provider-row:not([aria-disabled=true]):hover")
            bg = prop("background", *row)
            for text in (".provider-name", ".provider-base", ".provider-count"):
                selectors = [ancestor + " " + text for ancestor in row]
                check(f"厂商 {active=} {hover=} {text}", prop("color", *selectors),
                      background(".model-vendors") if bg == "transparent" else bg)

    for path in ((".model-compact-meta",), (".muted", ".model-id", ".model-compact .model-id"),
                 (".model-use-toggle", ".model-use-toggle>span"),
                 (".model-vision-status", ".model-vision-status>span[aria-hidden]")):
        check(path[-1], prop("color", *path), background(".card"))
    switch = ".model-switch-copy small"
    check(switch, prop("color", switch), background(".model-switch-row"))
    check("禁用开关说明", prop("color", switch, ".model-switch-disabled " + switch),
          background(".model-switch-row"),
          opacity=float(RULES[".model-switch-disabled"]["opacity"]), behind=background("dialog"))
    for path in ((".dialog-body .hint",), (".field .hint", ".dialog-body .hint"),
                 (".model-editor-note", ".dialog-body .model-editor-note")):
        check(path[0], prop("color", *path), background("dialog"))

    for removed in (False, True):
        opacity = float(RULES[".memory-card.is-removed"]["opacity"]) if removed else 1
        for role in (".memory-card-summary", ".memory-source-tag"):
            path = [role] if role.endswith("summary") else [".memory-card-meta", role]
            if removed:
                path.append(".memory-card.is-removed " + role)
            for card in (".memory-card", ".memory-card:hover"):
                check(f"{card} {role} {removed=}", prop("color", *path),
                      prop("background", role, default=background(card)), opacity=opacity,
                      behind=background("#pages"))

    for hover in (False, True):
        danger = [".btn", ".btn.danger"]
        icon = [".btn", ".model-action.delete"]
        if hover:
            danger.append(".btn.danger:where(:not(:disabled,[aria-disabled=true])):hover")
            icon.append(".model-action.delete:hover")
        check(f"危险按钮 {hover=}", prop("color", *danger), prop("background", *danger))
        filter_value = prop("filter", *icon, default="brightness(1)")
        factor = float(filter_value.removeprefix("brightness(").removesuffix(")"))
        assert RULES[".ico"]["fill"] == "currentColor"
        check(f"删除图标 {hover=}", prop("color", *icon), prop("background", *icon),
              minimum=3, brightness=factor)
    assert not failures, "\n".join(failures)
