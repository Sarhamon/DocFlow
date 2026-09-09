# -*- coding: utf-8 -*-
"""HWPX 문서 읽기/쓰기 (순수 파이썬).

HWPX는 zip + XML 이므로 한컴오피스 없이 다룰 수 있다.
누름틀(CLICK_HERE 등)은 아래 형태로 저장된다::

    <hp:ctrl><hp:fieldBegin id="123" name="docnumber" .../></hp:ctrl>
    <hp:t>○○○○처-000000</hp:t>
    <hp:ctrl><hp:fieldEnd beginIDRef="123" .../></hp:ctrl>

fieldBegin.id 와 fieldEnd.beginIDRef 로 짝을 짓고, 그 사이의 <hp:t> 가 값이다.
"""
from __future__ import annotations

import re
import shutil
import zipfile
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from xml.etree import ElementTree as ET

HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
NS = {"hp": HP}
T = f"{{{HP}}}t"
FIELD_BEGIN = f"{{{HP}}}fieldBegin"
FIELD_END = f"{{{HP}}}fieldEnd"

_SECTION_RE = re.compile(r"^Contents/section\d+\.xml$")


@dataclass
class Field:
    """문서 안의 누름틀 하나."""

    name: str
    type: str
    value: str
    section: str
    #: 같은 이름이 여러 번 나올 때의 등장 순번 (0-base)
    occurrence: int = 0


@dataclass
class HwpxDocument:
    path: Path
    _entries: dict[str, bytes] = dc_field(default_factory=dict, repr=False)
    _order: list[str] = dc_field(default_factory=list, repr=False)
    _compress: dict[str, int] = dc_field(default_factory=dict, repr=False)

    # ---------------------------------------------------------------- 로딩
    @classmethod
    def open(cls, path: str | Path) -> "HwpxDocument":
        path = Path(path)
        doc = cls(path=path)
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                doc._order.append(info.filename)
                doc._compress[info.filename] = info.compress_type
                doc._entries[info.filename] = zf.read(info.filename)
        return doc

    @property
    def section_names(self) -> list[str]:
        return [n for n in self._order if _SECTION_RE.match(n)]

    # ---------------------------------------------------------------- 읽기
    def fields(self) -> list[Field]:
        """문서 전체의 누름틀을 등장 순서대로 반환한다."""
        found: list[Field] = []
        seen: dict[str, int] = {}
        for section in self.section_names:
            root = ET.fromstring(self._entries[section].decode("utf-8"))
            for name, ftype, texts in _walk_fields(root):
                occurrence = seen.get(name, 0)
                seen[name] = occurrence + 1
                found.append(
                    Field(
                        name=name,
                        type=ftype,
                        value="".join(t.text or "" for t in texts),
                        section=section,
                        occurrence=occurrence,
                    )
                )
        return found

    def field_names(self) -> list[str]:
        """중복 제거한 필드 이름 목록 (등장 순서 유지)."""
        names: list[str] = []
        for f in self.fields():
            if f.name not in names:
                names.append(f.name)
        return names

    # ---------------------------------------------------------------- 쓰기
    def fill(self, values: dict[str, str]) -> int:
        """이름이 일치하는 모든 누름틀에 값을 채운다. 채운 개수를 반환."""
        filled = 0
        for section in self.section_names:
            root = ET.fromstring(self._entries[section].decode("utf-8"))
            changed = False
            for name, _ftype, texts in _walk_fields(root):
                if name not in values or not texts:
                    continue
                texts[0].text = str(values[name])
                for extra in texts[1:]:
                    extra.text = ""
                filled += 1
                changed = True
            if changed:
                self._entries[section] = _serialize(root)
        return filled

    def save(self, path: str | Path) -> Path:
        """HWPX 로 저장한다. mimetype 은 규격대로 첫 항목·무압축을 유지한다."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in self._order:
                compress = self._compress.get(name, zipfile.ZIP_DEFLATED)
                if name == "mimetype":
                    compress = zipfile.ZIP_STORED
                zf.writestr(
                    zipfile.ZipInfo(name), self._entries[name], compress_type=compress
                )
        return path


def _walk_fields(root: ET.Element):
    """(name, type, [텍스트 노드]) 를 문서 순서대로 yield 한다.

    fieldBegin.id ↔ fieldEnd.beginIDRef 로 짝을 지으며, 중첩된 경우
    가장 안쪽 필드가 텍스트를 가져간다.
    """
    open_stack: list[tuple[str, str, str, list[ET.Element]]] = []
    for el in root.iter():
        if el.tag == FIELD_BEGIN:
            open_stack.append(
                (el.get("id", ""), el.get("name", ""), el.get("type", ""), [])
            )
        elif el.tag == FIELD_END:
            ref = el.get("beginIDRef", "")
            for i in range(len(open_stack) - 1, -1, -1):
                if open_stack[i][0] == ref:
                    _fid, name, ftype, texts = open_stack.pop(i)
                    yield name, ftype, texts
                    break
        elif el.tag == T and open_stack:
            open_stack[-1][3].append(el)


def _serialize(root: ET.Element) -> bytes:
    for prefix, uri in _ns_map(root).items():
        ET.register_namespace(prefix, uri)
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(
        root, encoding="utf-8", xml_declaration=False
    )


def _ns_map(root: ET.Element) -> dict[str, str]:
    """루트 태그에서 쓰이는 네임스페이스 접두어를 되살린다."""
    return {
        "hp": HP,
        "hs": "http://www.hancom.co.kr/hwpml/2011/section",
        "hc": "http://www.hancom.co.kr/hwpml/2011/core",
        "hh": "http://www.hancom.co.kr/hwpml/2011/head",
        "hhs": "http://www.hancom.co.kr/hwpml/2011/history",
        "hm": "http://www.hancom.co.kr/hwpml/2011/master-page",
        "hpf": "http://www.hancom.co.kr/schema/2011/hpf",
        "hv": "http://www.hancom.co.kr/hwpml/2011/version",
        "ha": "http://www.hancom.co.kr/hwpml/2011/app",
        "opf": "http://www.idpf.org/2007/opf/",
        "dc": "http://purl.org/dc/elements/1.1/",
        "ooxmlchart": "http://www.hancom.co.kr/hwpml/2016/ooxmlchart",
        "hwpunitchar": "http://www.hancom.co.kr/hwpml/2016/HwpUnitChar",
        "epub": "http://www.idpf.org/2007/ops",
        "config": "http://www.hancom.co.kr/hwpml/2011/config",
    }
