"""HWPX → 인쇄용 HTML. 브라우저의 인쇄(PDF로 저장)로 PDF 를 받게 한다.

운영 서버에는 한컴이 없고, LibreOffice 는 쪽이 넘어가 쓰지 않기로 했다(deploy-aws.md).
그래서 HWPX 를 직접 읽어 서식 모양(쪽 크기·여백, 문단 모양, 표의 칸 크기·병합·선·배경, 그림)대로
HTML 을 그리고, 인쇄는 사용자의 브라우저가 한다.

* 쪽 나눔은 한글이 저장해 둔 줄 위치(lineseg vertpos)를 따른다. 위치가 다시 작아지는 문단에서
  새 쪽이 시작된다(한글이 그렇게 배치했다). 문단 속성의 쪽 나누기(pageBreak)와 구역도 새 쪽이다.
* 글꼴·줄바꿈이 한글과 조금 달라 한 쪽이 넘칠 수 있다. 화면의 스크립트(FIT_SCRIPT)가 넘친 쪽을
  줄여 맞추고, 원래 여러 쪽으로 흐르는 글(보고서 본문 등)은 줄이지 않고 이어서 인쇄한다.
* 한글과 똑같지는 않다(줄바꿈 위치, 장평). 그림·표 밖의 도형(선·사각형)은 그리지 않는다.
"""

from __future__ import annotations

import base64
import html
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field

HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
HH = "{http://www.hancom.co.kr/hwpml/2011/head}"
HC = "{http://www.hancom.co.kr/hwpml/2011/core}"
OPF = "{http://www.idpf.org/2007/opf/}"

#: HWPUNIT(1/7200 인치) → mm
_PER_MM = 7200 / 25.4

_BORDER = {"NONE": "none", "SOLID": "solid", "DOT": "dotted", "DOUBLE_SLIM": "double",
           "SLIM_THICK": "double", "THICK_SLIM": "double", "SLIM_THICK_SLIM": "double"}  # fmt: skip
_ALIGN = {"LEFT": "left", "RIGHT": "right", "CENTER": "center", "JUSTIFY": "justify",
          "DISTRIBUTE": "justify", "DISTRIBUTE_SPACE": "justify"}  # fmt: skip
_VALIGN = {"TOP": "top", "CENTER": "middle", "BOTTOM": "bottom"}
#: 바탕·명조 계열은 명조로, 나머지는 고딕으로 대신한다 (한글 글꼴이 없는 PC)
_SERIF_RE = re.compile(r"바탕|명조|궁서|batang|myeongjo", re.I)


def mm(value: float | str | None) -> str:
    return f"{int(value or 0) / _PER_MM:.2f}mm"


@dataclass
class _Char:
    size: float = 10.0
    color: str = "#000000"
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    font: str = ""
    spacing: int = 0
    shade: str = ""

    def css(self) -> str:
        out = [f"font-size:{self.size:g}pt", f"font-family:{_font_stack(self.font)}"]
        if self.color.lower() not in ("#000000", "none"):
            out.append(f"color:{self.color}")
        if self.bold:
            out.append("font-weight:700")
        if self.italic:
            out.append("font-style:italic")
        lines = [
            x for x, on in (("underline", self.underline), ("line-through", self.strike)) if on
        ]
        if lines:
            out.append(f"text-decoration:{' '.join(lines)}")
        if self.spacing:
            out.append(f"letter-spacing:{self.spacing / 100:.3f}em")
        if self.shade:
            out.append(f"background:{self.shade}")
        return ";".join(out)


@dataclass
class _Para:
    align: str = "justify"
    left: int = 0
    right: int = 0
    indent: int = 0
    before: int = 0
    after: int = 0
    line: str = "1.6"


@dataclass
class _Styles:
    chars: dict[str, _Char] = field(default_factory=dict)
    paras: dict[str, _Para] = field(default_factory=dict)
    borders: dict[str, str] = field(default_factory=dict)
    images: dict[str, str] = field(default_factory=dict)


def _font_stack(face: str) -> str:
    generic = (
        "'HCR Batang','Batang','Noto Serif KR',serif"
        if _SERIF_RE.search(face)
        else ("'HCR Dotum','Malgun Gothic','Noto Sans KR',sans-serif")
    )
    return f"'{face}',{generic}" if face else generic


def _styles(z: zipfile.ZipFile) -> _Styles:
    st = _Styles()
    if "Contents/header.xml" not in z.namelist():
        return st  # 모양 정의가 없으면 기본 모양으로 그린다
    head = ET.fromstring(z.read("Contents/header.xml"))
    faces: dict[str, str] = {}
    for ff in head.iter(HH + "fontface"):
        if ff.get("lang") == "HANGUL":
            faces = {f.get("id", ""): f.get("face", "") for f in ff.iter(HH + "font")}
    for c in head.iter(HH + "charPr"):
        ref = c.find(HH + "fontRef")
        sp = c.find(HH + "spacing")
        ul = c.find(HH + "underline")
        so = c.find(HH + "strikeout")
        shade = c.get("shadeColor", "none")
        st.chars[c.get("id", "")] = _Char(
            size=int(c.get("height", "1000")) / 100,
            color=c.get("textColor", "#000000"),
            bold=c.find(HH + "bold") is not None,
            italic=c.find(HH + "italic") is not None,
            underline=ul is not None and ul.get("type", "NONE") != "NONE",
            strike=so is not None and so.get("shape", "NONE") not in ("NONE", "3D"),
            font=faces.get(ref.get("hangul", "") if ref is not None else "", ""),
            spacing=int(sp.get("hangul", "0")) if sp is not None else 0,
            shade="" if shade.lower() in ("none", "#ffffff") else shade,
        )
    for p in head.iter(HH + "paraPr"):
        para = _Para()
        al = p.find(HH + "align")
        if al is not None:
            para.align = _ALIGN.get(al.get("horizontal", ""), "justify")
        # HwpUnitChar 쪽(hp:case)이 먼저 나온다: 단위가 HWPUNIT 이다
        margin = next(p.iter(HH + "margin"), None)
        if margin is not None:
            val = {k.tag.replace(HC, ""): int(k.get("value", "0")) for k in margin}
            para.left, para.right = val.get("left", 0), val.get("right", 0)
            para.indent, para.before, para.after = (
                val.get("intent", 0),
                val.get("prev", 0),
                val.get("next", 0),
            )
        ls = next(p.iter(HH + "lineSpacing"), None)
        if ls is not None:
            kind, value = ls.get("type", "PERCENT"), int(ls.get("value", "160"))
            para.line = {
                "PERCENT": f"{value / 100:.2f}",
                "FIXED": mm(value),
                "BETWEEN_LINES": f"calc(1em + {mm(value)})",
            }.get(kind, f"{max(value / 100, 1):.2f}")
        st.paras[p.get("id", "")] = para
    for bf in head.iter(HH + "borderFill"):
        css = []
        for side in ("left", "right", "top", "bottom"):
            b = bf.find(f"{HH}{side}Border")
            kind = b.get("type", "NONE") if b is not None else "NONE"
            if b is None or kind == "NONE":
                continue
            width = b.get("width", "0.12 mm").replace(" ", "")
            style = _BORDER.get(kind, "dashed" if "DASH" in kind else "solid")
            if style == "double":
                width = f"max({width},0.5mm)"
            css.append(f"border-{side}:{width} {style} {b.get('color', '#000000')}")
        brush = bf.find(f".//{HC}winBrush")
        if brush is not None and brush.get("faceColor", "none").lower() not in ("none", "#ffffff"):
            css.append(f"background:{brush.get('faceColor')}")
        st.borders[bf.get("id", "")] = ";".join(css)
    try:
        manifest = ET.fromstring(z.read("Contents/content.hpf"))
        for item in manifest.iter(OPF + "item"):
            href = item.get("href", "")
            if href.startswith("BinData/") and href in z.namelist():
                data = base64.b64encode(z.read(href)).decode()
                st.images[item.get("id", "")] = (
                    f"data:{item.get('media-type', 'image/png')};base64,{data}"
                )
    except KeyError:
        pass
    return st


@dataclass
class _Float:
    """종이·쪽에 붙여 둔 개체 (기안문 아래쪽의 결재·시행 칸 글상자 등). 쪽 위에 따로 놓는다."""

    rel: str  # PAPER / PAGE
    valign: str
    halign: str
    voff: int
    hoff: int
    width: int
    height: int
    html: str


class _Renderer:
    def __init__(self, st: _Styles) -> None:
        self.st = st
        #: 문단을 그리다 만난 떠 있는 개체. 쪽을 묶을 때 가져간다
        self.floats: list[_Float] = []

    def take_floats(self) -> list[_Float]:
        out, self.floats = self.floats, []
        return out

    def place(self, node: ET.Element, inner: str) -> str:
        """글자처럼 놓인 개체는 그 자리에, 종이·쪽에 붙은 개체는 떼어 쪽 위에 둔다."""
        pos, sz = node.find(HP + "pos"), node.find(HP + "sz")
        if pos is None or sz is None or pos.get("treatAsChar") == "1":
            return inner
        if pos.get("vertRelTo") not in ("PAPER", "PAGE"):
            return inner
        self.floats.append(_Float(
            pos.get("vertRelTo", "PAPER"),
            pos.get("vertAlign", "TOP"),
            pos.get("horzAlign", "LEFT"),
            int(pos.get("vertOffset", "0")), int(pos.get("horzOffset", "0")),
            int(sz.get("width", "0")), int(sz.get("height", "0")), inner,
        ))  # fmt: skip
        return ""

    def rect(self, rect: ET.Element) -> str:
        """글상자: 안의 문단만 그린다 (선이 있으면 테두리로)."""
        sz = rect.find(HP + "sz")
        text = rect.find(HP + "drawText")
        sub = text.find(HP + "subList") if text is not None else None
        if sz is None or sub is None:
            return ""
        line = rect.find(HP + "lineShape")
        border = ""
        if line is not None and line.get("style", "NONE") != "NONE":
            width = mm(max(int(line.get("width", "0")), 28))
            border = f"border:{width} solid {line.get('color', '#000')};"
        valign = {"TOP": "flex-start", "CENTER": "center", "BOTTOM": "flex-end"}.get(
            sub.get("vertAlign", "TOP"), "flex-start"
        )
        body = "".join(self.para(p) for p in sub.findall(HP + "p"))
        return (
            f'<div class="box" style="width:{mm(sz.get("width"))};height:{mm(sz.get("height"))};'
            f'justify-content:{valign};{border}">{body}</div>'
        )

    # ------------------------------------------------------------ 문단
    def para(self, p: ET.Element) -> str:
        pp = self.st.paras.get(p.get("paraPrIDRef", ""), _Para())
        runs = p.findall(HP + "run")
        first = self.st.chars.get(runs[0].get("charPrIDRef", "") if runs else "", _Char())
        inner = "".join(self.run(r) for r in runs)
        visible = ("<table", "<img", 'class="box"')
        if not re.sub(r"<[^>]+>", "", inner).strip() and not any(v in inner for v in visible):
            inner += f'<span style="{first.css()}"> </span>'
        hang = -pp.indent if pp.indent < 0 else 0
        style = (
            f"text-align:{pp.align};line-height:{pp.line};font-size:{first.size:g}pt;"
            f"padding-left:{mm(pp.left + hang)};padding-right:{mm(pp.right)};"
            f"text-indent:{mm(pp.indent)};margin:{mm(pp.before)} 0 {mm(pp.after)}"
        )
        return f'<div class="p" style="{style}">{inner}</div>'

    def run(self, r: ET.Element) -> str:
        ch = self.st.chars.get(r.get("charPrIDRef", ""), _Char())
        out = []
        for node in r:
            tag = node.tag.replace(HP, "")
            if tag == "t":
                out.append(f'<span style="{ch.css()}">{self.text(node)}</span>')
            elif tag == "tbl":
                out.append(self.place(node, self.table(node)))
            elif tag == "pic":
                out.append(self.place(node, self.pic(node)))
            elif tag == "rect":
                out.append(self.place(node, self.rect(node)))
        return "".join(out)

    def text(self, t: ET.Element) -> str:
        buf = [html.escape(t.text or "")]
        for child in t:
            tag = child.tag.replace(HP, "")
            if tag == "lineBreak":
                buf.append("<br>")
            elif tag == "tab":
                width = int(child.get("width", "0") or 0) or 4000
                buf.append(f'<span class="tab" style="width:{mm(width)}"></span>')
            elif tag in ("nbSpace", "fwSpace"):
                buf.append(" ")
            buf.append(html.escape(child.tail or ""))
        return "".join(buf)

    def pic(self, pic: ET.Element) -> str:
        img = pic.find(f"{HC}img")
        src = self.st.images.get(img.get("binaryItemIDRef", "") if img is not None else "")
        if not src:
            return ""
        size = pic.find(HP + "curSz")
        if size is None or not int(size.get("width", "0") or 0):
            size = pic.find(HP + "sz")
        w, h = (size.get("width"), size.get("height")) if size is not None else (0, 0)
        return f'<img src="{src}" alt="" style="width:{mm(w)};height:{mm(h)}">'

    # ------------------------------------------------------------ 표
    def table(self, tbl: ET.Element) -> str:
        rows, cols = int(tbl.get("rowCnt", "0")), int(tbl.get("colCnt", "0"))
        inner = tbl.find(HP + "inMargin")
        cells = []
        for tc in _own_cells(tbl):
            addr = tc.find(HP + "cellAddr")
            if addr is None:
                continue
            span, size = tc.find(HP + "cellSpan"), tc.find(HP + "cellSz")
            sp = {
                k: int(span.get(k, "1")) if span is not None else 1 for k in ("rowSpan", "colSpan")
            }
            sz = {k: int(size.get(k, "0")) if size is not None else 0 for k in ("width", "height")}
            cells.append((
                int(addr.get("rowAddr", "0")), int(addr.get("colAddr", "0")),
                sp["rowSpan"], sp["colSpan"], sz["width"], sz["height"], tc,
            ))  # fmt: skip
        widths = _solve([(c, cs, w) for _r, c, _rs, cs, w, _h, _tc in cells], cols)
        heights = _solve([(r, rs, h) for r, _c, rs, _cs, _w, h, _tc in cells], rows)
        by_row: dict[int, list] = {}
        for cell in sorted(cells, key=lambda x: (x[0], x[1])):
            by_row.setdefault(cell[0], []).append(cell)
        out = [f'<table style="width:{mm(sum(widths))}"><colgroup>']
        out += [f'<col style="width:{mm(w)}">' for w in widths]
        out.append("</colgroup>")
        for r in range(rows):
            out.append(f'<tr style="height:{mm(heights[r])}">')
            for _r, _c, rs, cs, _w, _h, tc in by_row.get(r, []):
                out.append(self.cell(tc, rs, cs, inner))
            out.append("</tr>")
        out.append("</table>")
        return "".join(out)

    def cell(self, tc: ET.Element, rs: int, cs: int, table_margin: ET.Element | None) -> str:
        margin = tc.find(HP + "cellMargin") if tc.get("hasMargin") == "1" else table_margin
        sub = tc.find(HP + "subList")
        valign = _VALIGN.get(
            sub.get("vertAlign", "CENTER") if sub is not None else "CENTER", "middle"
        )
        pad = " ".join(mm(margin.get(k) if margin is not None else 141)
                       for k in ("top", "right", "bottom", "left"))  # fmt: skip
        border = self.st.borders.get(tc.get("borderFillIDRef", ""), "")
        style = f"padding:{pad};vertical-align:{valign};{border}"
        span = (f' rowspan="{rs}"' if rs > 1 else "") + (f' colspan="{cs}"' if cs > 1 else "")
        body = "".join(self.para(p) for p in (sub.findall(HP + "p") if sub is not None else []))
        return f'<td{span} style="{style}">{body}</td>'


def _own_cells(tbl: ET.Element):
    """이 표의 칸만 (안에 든 표의 칸은 빼고)."""
    for tr in tbl.findall(HP + "tr"):
        yield from tr.findall(HP + "tc")


def _solve(spans: list[tuple[int, int, int]], n: int) -> list[float]:
    """칸 크기들(시작, 개수, 크기)로 열 너비·행 높이를 정한다.

    한 칸짜리를 먼저 정하고, 여러 칸에 걸친 칸은 아직 모르는 칸들이 나눠 갖는다.
    """
    size: list[float | None] = [None] * n
    for start, count, value in spans:
        if count == 1 and start < n:
            size[start] = max(size[start] or 0, value)
    for _ in range(n):
        changed = False
        for start, count, value in sorted(spans, key=lambda s: s[1]):
            idx = [i for i in range(start, min(start + count, n))]
            unknown = [i for i in idx if size[i] is None]
            if unknown:
                rest = value - sum(size[i] or 0 for i in idx)
                for i in unknown:
                    size[i] = max(rest / len(unknown), 0)
                changed = True
        if not changed:
            break
    return [s if s is not None else 0 for s in size]


# ---------------------------------------------------------------- 쪽
@dataclass
class _Page:
    width: int
    height: int
    margin: tuple[int, int, int, int]
    paras: list[str] = field(default_factory=list)
    floats: list[_Float] = field(default_factory=list)

    @property
    def body(self) -> tuple[int, int]:
        """본문 영역의 너비·높이."""
        top, right, bottom, left = self.margin
        return self.width - left - right, self.height - top - bottom

    def spot(self, f: _Float) -> tuple[int, int]:
        """떠 있는 개체의 왼쪽 위 (종이 기준)."""
        top, _right, _bottom, left = self.margin
        bw, bh = self.body
        ox, oy, aw, ah = (
            (0, 0, self.width, self.height) if f.rel == "PAPER" else (left, top, bw, bh)
        )
        y = {"BOTTOM": ah - f.voff - f.height, "CENTER": (ah - f.height) // 2 + f.voff}.get(
            f.valign, f.voff
        )
        x = {"RIGHT": aw - f.hoff - f.width, "CENTER": (aw - f.width) // 2 + f.hoff}.get(
            f.halign, f.hoff
        )
        return ox + x, oy + y

    def html(self) -> str:
        """종이 한 장.

        여백은 안쪽 여백(padding)이라 떠 있는 개체가 여백에 걸쳐도 쪽이 넘어가지 않는다.
        """
        top, right, bottom, left = self.margin
        _bw, bh = self.body
        room = bh
        out = []
        for f in self.floats:
            x, y = self.spot(f)
            if y - top > bh // 3:  # 아래쪽에 붙은 칸(결재·시행 칸) 위까지만 본문이 쓴다
                room = min(room, y - top)
            out.append(f'<div class="float" style="left:{mm(x)};top:{mm(y)}">{f.html}</div>')
        style = (
            f"width:{mm(self.width)};height:{mm(self.height)};"
            f"padding:{mm(top)} {mm(right)} {mm(bottom)} {mm(left)}"
        )
        return (
            f'<section class="page" data-room="{room / bh:.4f}" style="{style}">'
            f'<div class="body">{"".join(self.paras)}</div>{"".join(out)}</section>'
        )


def _first_vertpos(p: ET.Element) -> int | None:
    seg = p.find(f"{HP}linesegarray/{HP}lineseg")
    return int(seg.get("vertpos", "0")) if seg is not None else None


def _pages(z: zipfile.ZipFile, r: _Renderer) -> list[_Page]:
    sections = sorted(
        (n for n in z.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n)),
        key=lambda n: int(re.search(r"\d+", n.rsplit("/", 1)[1]).group()),
    )
    pages: list[_Page] = []
    for name in sections:
        root = ET.fromstring(z.read(name))
        pr = next(root.iter(HP + "pagePr"), None)
        m = pr.find(HP + "margin") if pr is not None else None
        mg = {k: int(m.get(k, d)) if m is not None else int(d) for k, d in (
            ("top", "5669"), ("right", "4251"), ("bottom", "4251"), ("left", "4251"),
            ("header", "0"), ("footer", "0"),
        )}  # fmt: skip
        width = int(pr.get("width", "59528")) if pr is not None else 59528
        height = int(pr.get("height", "84188")) if pr is not None else 84188
        if pr is not None and pr.get("landscape") == "NARROWLY" and width < height:
            width, height = height, width
        margin = (mg["top"] + mg["header"], mg["right"],
                  mg["bottom"] + mg["footer"], mg["left"])  # fmt: skip
        page = _Page(width, height, margin)
        pages.append(page)
        last = -1
        for p in root.findall(HP + "p"):
            pos = _first_vertpos(p)
            new = p.get("pageBreak") == "1" or (pos is not None and last >= 0 and pos < last)
            if new and page.paras:
                page = _Page(width, height, margin)
                pages.append(page)
            if pos is not None:
                last = pos
            page.paras.append(r.para(p))
            page.floats += r.take_floats()
    return [p for p in pages if p.paras]


FIT_SCRIPT = """
// 한 쪽이 넘치면 줄여 맞춘다. 원래 여러 쪽으로 흐르는 글(많이 넘침)은 줄이지 않고 이어서 인쇄한다
function fitPages() {
  for (const page of document.querySelectorAll(".page")) {
    const body = page.firstElementChild;
    body.style.zoom = 1;
    page.classList.remove("flow");
    const cs = getComputedStyle(page);
    const inner = page.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);
    const room = inner * Number(page.dataset.room || 1), need = body.scrollHeight;
    if (need <= room + 1) continue;
    const scale = room / need;
    if (scale >= 0.75) body.style.zoom = scale;
    else page.classList.add("flow");
  }
}
"""


#: 화면 위쪽 안내 (인쇄에는 나오지 않는다)
TOOLBAR = """<div class="bar no-print"><b>PDF로 저장</b>
<span>인쇄 창의 대상(프린터)에서 <b>PDF로 저장</b>을 고르세요.
여백은 '없음', 배경 그래픽은 켜 두세요.
한글에서 연 것과 글자 간격·줄바꿈이 조금 다를 수 있습니다. 제출 원본은 HWPX 입니다.</span>
<button type="button" onclick="window.print()">인쇄 / PDF로 저장</button></div>"""


#: 열자마자 인쇄 창 (줄여 맞춘 뒤)
AUTO_PRINT = "setTimeout(() => window.print(), 300);"


def render(data: bytes, title: str = "", *, toolbar: bool = False, auto: bool = False) -> str:
    """HWPX 바이트를 인쇄용 HTML 문서 한 장으로. auto 면 열자마자 인쇄 창을 띄운다."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        renderer = _Renderer(_styles(z))
        pages = _pages(z, renderer)
    first = pages[0] if pages else _Page(59528, 84188, (5669, 4251, 4251, 4251))
    top, right, bottom, left = first.margin
    body = "".join(p.html() for p in pages)
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
@page {{ size: {mm(first.width)} {mm(first.height)}; margin: 0; }}
/* 여러 쪽으로 흐르는 글은 종이 여백을 인쇄 여백으로 둔다 (이어지는 쪽에도 여백이 생긴다) */
@page flow {{ margin: {mm(top)} {mm(right)} {mm(bottom)} {mm(left)}; }}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; background: #e9ecef; }}
body {{ font-family: {_font_stack("함초롬바탕")}; color: #000;
  -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
.page {{ position: relative; background: #fff; margin: 8mm auto;
  box-shadow: 0 1px 4px rgba(0,0,0,.25); overflow: hidden; }}
.page.flow {{ page: flow; height: auto !important; padding: 0 !important;
  overflow: visible; }}
.float {{ position: absolute; }}
.box {{ display: flex; flex-direction: column; }}
.p {{ white-space: pre-wrap; word-break: keep-all; overflow-wrap: anywhere; }}
.p table {{ display: inline-table; vertical-align: top; text-indent: 0; }}
table {{ border-collapse: collapse; table-layout: fixed; }}
td {{ overflow: hidden; }}
.tab {{ display: inline-block; }}
img {{ vertical-align: bottom; }}
.bar {{ position: sticky; top: 0; z-index: 9; display: flex; gap: 12px; align-items: center;
  padding: 10px 16px;
  background: #0c3a34; color: #e6f4f0; font: 14px/1.5 'Malgun Gothic', sans-serif; }}
.bar span {{ flex: 1; color: #9fc7bd; }} .bar span b {{ color: #fff; }}
.bar button {{ font: inherit; font-weight: 700; padding: 6px 14px; border: 0; border-radius: 6px;
  background: #5fc4ae; color: #0c3a34; cursor: pointer; }}
@media print {{
  html, body {{ background: none; }}
  .page {{ margin: 0; outline: none; box-shadow: none; break-after: page; }}
  .page:last-child {{ break-after: auto; }}
  .no-print {{ display: none !important; }}
}}
</style></head>
<body>{TOOLBAR if toolbar else ""}{body}
<script>{FIT_SCRIPT}
document.fonts.ready.then(() => {{ fitPages(); {AUTO_PRINT if auto else ""} }});
</script></body></html>"""
