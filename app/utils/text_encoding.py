# app/utils/text_encoding.py
"""
网络来源文本编码规范化，避免特殊字体/错误编码导致乱码。
在代码侧统一转为可安全使用的 UTF-8 字符串。
小程序端对部分 Unicode 无法渲染会显示为 ◇，需在输出前做「可安全显示」过滤。
"""
from typing import Union

# 常见来源编码，按优先级尝试
_DECODE_ORDER = ("utf-8", "gbk", "gb2312", "big5", "latin-1", "cp1252")

# 小程序/终端常无法渲染，会显示为 ◇ 或方框，统一替换为空格
_REPLACEMENT_CHAR = "\uFFFD"  # Unicode replacement character


# 保留的格式控制符：换行、回车、制表，避免 agent 回复变成一大坨
_KEEP_CONTROL = (0x09, 0x0A, 0x0D)  # \t \n \r


def safe_for_display(value: Union[str, None]) -> str:
    """
    去掉会导致小程序或终端显示为 ◇/方框的字符，避免乱码观感。
    保留 \\n \\r \\t，不破坏 agent 回复的换行与格式。
    """
    if value is None:
        return ""
    s = value if isinstance(value, str) else str(value)
    result = []
    for c in s:
        code = ord(c)
        if code == 0xFFFD:
            result.append(" ")
        elif code < 0x20 or (0x7F <= code < 0xA0):
            if code in _KEEP_CONTROL:
                result.append(c)
            # 其余控制字符丢弃
        elif 0xD800 <= code <= 0xDFFF:
            continue  # surrogate 丢弃
        elif code in (0x200B, 0x200C, 0x200D, 0xFEFF):
            continue  # 零宽、BOM 丢弃
        else:
            result.append(c)
    return "".join(result)


def normalize_text(value: Union[str, bytes, None]) -> str:
    """
    将可能来自不同编码或含特殊字符的文本规范化为 UTF-8 字符串，避免乱码。

    - None -> ""
    - bytes: 依次尝试 utf-8 / gbk / gb2312 / big5 / latin-1，解码失败用 replacement
    - str: 按 UTF-8  round-trip 替换非法字符

    返回结果会再经 safe_for_display 过滤后再送入 LLM/前端，避免 ◇ 占位。
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        for enc in _DECODE_ORDER:
            try:
                return value.decode(enc, errors="replace")
            except (LookupError, UnicodeDecodeError):
                continue
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    return str(value)
