"""Extract the image from a takephoto API response JSON file."""

import argparse
import base64
import binascii
import json
import mimetypes
from pathlib import Path
from urllib.parse import unquote


IMAGE_FIELD = "image_base64"


def extract_image(response_path: Path) -> Path:
    with response_path.open("r", encoding="utf-8") as file:
        response = json.load(file)

    image_value = response.get(IMAGE_FIELD)
    if not isinstance(image_value, str) or not image_value.strip():
        raise ValueError(f"JSON 中没有有效的 {IMAGE_FIELD} 字段")

    image_value = image_value.strip()
    extension = ".img"
    if image_value.startswith("data:"):
        header, separator, image_value = image_value.partition(",")
        if not separator:
            raise ValueError("data URL 缺少图片数据")
        mime_type = header[5:].split(";", 1)[0]
        extension = mimetypes.guess_extension(mime_type) or extension
        image_value = unquote(image_value)

    try:
        image_bytes = base64.b64decode(image_value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError(f"{IMAGE_FIELD} 不是有效的 Base64 数据") from error

    if not image_bytes:
        raise ValueError("图片数据为空")

    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        extension = ".png"
    elif image_bytes.startswith(b"\xff\xd8\xff"):
        extension = ".jpg"
    elif image_bytes.startswith((b"GIF87a", b"GIF89a")):
        extension = ".gif"
    elif image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        extension = ".webp"

    output_path = response_path.with_name(f"{response_path.stem}_image{extension}")
    output_path.write_bytes(image_bytes)
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="从响应 JSON 提取图片")
    parser.add_argument(
        "response",
        nargs="?",
        type=Path,
        default=Path(__file__).with_name("response.json"),
        help="响应 JSON 文件路径，默认使用同目录下的示例文件",
    )
    args = parser.parse_args()

    try:
        output_path = extract_image(args.response)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        parser.error(str(error))

    print(f"图片已提取到: {output_path}")


if __name__ == "__main__":
    main()