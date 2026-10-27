"""파일 주고받기: 올린 HWP·HWPX 를 HWPX 로 받기, 내려받기 응답.

만든 파일은 암호화해 두므로(service/crypto.py) 내려받을 때 푼 내용을 바로 보낸다.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import quote

from fastapi import Response, UploadFile

from docflow.convert import SIGNATURES, ConversionJob, Converter
from docflow.service.catalog import Unavailable

#: 올릴 수 있는 파일 하나의 크기
MAX_UPLOAD = 20 * 1024 * 1024


def download_response(data: bytes, name: str) -> Response:
    media = mimetypes.guess_type(name)[0] or "application/octet-stream"
    disposition = f"attachment; filename*=UTF-8''{quote(name)}"
    return Response(data, media_type=media, headers={"Content-Disposition": disposition})


def hwpx_upload(
    upload: UploadFile, work: Path, converter: Converter | None, *, stem: str = "upload"
) -> tuple[Path, str]:
    """올린 HWP·HWPX 를 work 안의 HWPX 로. (HWPX 경로, 원래 파일 이름)

    HWP 는 한컴 변환이 있어야 한다. converter 는 한 번에 하나만 돌도록 감싼 것을 준다.
    """
    name = Path(upload.filename or "").name
    suffix = Path(name).suffix.lower()
    if suffix not in (".hwp", ".hwpx"):
        raise ValueError(f"{name or '파일'}: .hwp 나 .hwpx 파일만 받는다")
    data = upload.file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise ValueError(f"{name}: 파일은 {MAX_UPLOAD // 1024 // 1024}MB 까지 올린다")
    fmt = suffix[1:].upper()
    if not data.startswith(SIGNATURES[fmt]):
        raise ValueError(f"{name}: {fmt} 파일이 아니다")
    src = work / f"{stem}{suffix}"
    src.write_bytes(data)
    if fmt == "HWPX":
        return src, name
    if converter is None:
        # 운영 서버(리눅스)에는 한컴이 없다. 학생에게 HWPX 로 받기로 했다
        raise Unavailable("HWP 파일은 읽을 수 없습니다. 한글에서 HWPX 로 저장해 올리세요")
    result = converter.convert(ConversionJob(source=src, formats=("HWPX",), out_dir=work))
    if not result.ok:
        raise ValueError(f"{name}: HWPX 로 바꾸지 못했다 ({result.errors.get('HWPX', '')})")
    return result.outputs["HWPX"], name
