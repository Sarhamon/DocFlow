"""hwp2hwpx 어댑터 — 한컴 없이 HWP 를 HWPX 로 (리눅스 운영 서버용).

오픈소스 hwp2hwpx(Java)를 자식 프로세스로 돌린다. 이미지에 넣는 법과 고친 곳은 Dockerfile 과
tools/hwp2hwpx/ 에 있다. HWPX 만 만든다 — HWP·PDF 출력은 여전히 한컴이 있어야 한다.

한컴이 저장한 HWPX 와 다른 점 하나를 여기서 맞춘다: 표 수식을 한컴은 Formula·ResultFormat·
LastResult 매개변수로도 쓰는데 hwp2hwpx 는 Command("수식??형식;;결과") 하나에만 쓴다.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape, unescape

from docflow.convert import ConversionJob, ConversionResult, check_output

#: classes/ 와 lib/ 가 있는 폴더 (Dockerfile 이 정한다)
HOME_ENV = "DOCFLOW_HWP2HWPX"

_SECTION = re.compile(r"section\d+\.xml$")
_FORMULA = re.compile(
    r'(<hp:fieldBegin[^>]*type="FORMULA"[^>]*><hp:parameters cnt=")(\d+)("[^>]*>)(.*?)'
    r"(</hp:parameters>)",
    re.S,
)
_COMMAND = re.compile(r'<hp:stringParam name="Command"[^>]*>(.*?)</hp:stringParam>', re.S)


def available() -> bool:
    home = os.environ.get(HOME_ENV)
    return bool(home) and Path(home).is_dir() and shutil.which("java") is not None


def _spell_out(match: re.Match[str]) -> str:
    head, count, mid, params, tail = match.groups()
    command = _COMMAND.search(params)
    if command is None or 'name="Formula"' in params:
        return match.group(0)
    body, _, last = unescape(command.group(1)).partition(";;")
    formula, _, fmt = body.rpartition("??")  # 수식이 ? 로 끝날 수 있다 (=B?-C?)
    extra = "".join(
        f'<hp:stringParam name="{name}">{escape(value)}</hp:stringParam>'
        for name, value in (("Formula", formula), ("ResultFormat", fmt), ("LastResult", last))
    )
    return f"{head}{int(count) + 3}{mid}{params}{extra}{tail}"


def spell_out_formulas(path: Path) -> None:
    """수식 누름틀의 Command 를 한컴처럼 Formula·ResultFormat·LastResult 로도 풀어 쓴다."""
    tmp = path.with_suffix(".tmp")
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp, "w") as dst:
        for info in src.infolist():
            data = src.read(info)
            if _SECTION.search(info.filename):
                data = _FORMULA.sub(_spell_out, data.decode("utf-8")).encode("utf-8")
            dst.writestr(info, data, compress_type=info.compress_type)
    tmp.replace(path)


class Hwp2HwpxConverter:
    """HWP 를 HWPX 로 바꾼다 (docflow.convert.Converter). 다른 형식은 실패로 돌려준다."""

    def __init__(self, *, timeout: float = 60.0) -> None:
        self.timeout = timeout

    def convert(self, job: ConversionJob) -> ConversionResult:
        started = time.perf_counter()
        result = ConversionResult(job=job, attempts=1)
        for fmt in job.formats:
            if fmt != "HWPX":
                result.errors[fmt] = "hwp2hwpx 는 HWPX 만 만든다"
                continue
            dst = job.target(fmt)
            error = self._run(Path(job.source), dst) or check_output(dst, fmt)
            if error:
                result.errors[fmt] = error
            else:
                spell_out_formulas(dst)
                result.outputs[fmt] = dst
        result.elapsed = time.perf_counter() - started
        return result

    def _run(self, src: Path, dst: Path) -> str | None:
        home = Path(os.environ[HOME_ENV])
        classpath = f"{home / 'classes'}{os.pathsep}{home / 'lib'}{os.sep}*"
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            done = subprocess.run(
                ["java", "-cp", classpath, "Main", str(src), str(dst)],
                capture_output=True, timeout=self.timeout, check=False,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            return f"{self.timeout:.0f}초 안에 끝나지 않았다"
        if done.returncode != 0:
            return done.stderr.decode("utf-8", errors="replace").strip()[-300:] or "변환 실패"
        return None
