"""hwp2hwpx 어댑터 (한컴 없는 운영 서버의 HWP -> HWPX). 자바는 부르지 않는다."""

from __future__ import annotations

import zipfile

from docflow.adapters import hwp2hwpx
from docflow.convert import ConversionJob


def formula(command: str, extra: str = "", cnt: int = 2) -> str:
    return (
        '<hp:fieldBegin id="1" type="FORMULA" name="" editable="0">'
        f'<hp:parameters cnt="{cnt}" name=""><hp:integerParam name="Prop">8</hp:integerParam>'
        f'<hp:stringParam name="Command" xml:space="preserve">{command}</hp:stringParam>{extra}'
        "</hp:parameters></hp:fieldBegin>"
    )


def spelled(tmp_path, xml: str) -> str:
    path = tmp_path / "a.hwpx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Contents/section0.xml", xml)
    hwp2hwpx.spell_out_formulas(path)
    with zipfile.ZipFile(path) as z:
        return z.read("Contents/section0.xml").decode("utf-8")


def test_수식_Command_를_한컴처럼_풀어_쓴다(tmp_path):
    out = spelled(tmp_path, formula("=SUM(ABOVE)??%g,;;3,080,000"))
    assert 'cnt="5"' in out
    assert '<hp:stringParam name="Formula">=SUM(ABOVE)</hp:stringParam>' in out
    assert '<hp:stringParam name="ResultFormat">%g,</hp:stringParam>' in out
    assert '<hp:stringParam name="LastResult">3,080,000</hp:stringParam>' in out


def test_물음표로_끝나는_수식과_특수문자(tmp_path):
    out = spelled(tmp_path, formula("=B?-C???%g,;;0") + formula("=A1&lt;B1??;;"))
    assert '<hp:stringParam name="Formula">=B?-C?</hp:stringParam>' in out
    assert '<hp:stringParam name="Formula">=A1&lt;B1</hp:stringParam>' in out


def test_이미_풀어_쓴_수식과_다른_누름틀은_그대로(tmp_path):
    done = formula("=A1??;;1", '<hp:stringParam name="Formula">=A1</hp:stringParam>', cnt=3)
    click = done.replace("FORMULA", "CLICK_HERE")
    assert spelled(tmp_path, done + click) == done + click


def test_HWPX_가_아닌_형식은_만들지_않는다(tmp_path):
    job = ConversionJob(source=tmp_path / "a.hwp", formats=("PDF",))
    result = hwp2hwpx.Hwp2HwpxConverter().convert(job)
    assert not result.ok and "HWPX 만" in result.errors["PDF"]
