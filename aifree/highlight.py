"""Подсветка кода в блоках markdown. Без зависимостей и Pygments.

Поддерживает Python, JS/TS, C-подобные языки, Go, Rust, Bash, SQL,
JSON, YAML/TOML, HTML/XML, CSS и diff. Для неизвестного языка красит
только строки, числа и комментарии.
"""

from __future__ import annotations

import re

RESET = "\033[0m"

# Monokai palette
C_KEYWORD = "38;2;249;38;114"   # pink
C_STRING = "38;2;230;219;116"   # yellow
C_COMMENT = "38;2;117;113;94"   # gray
C_NUMBER = "38;2;174;129;255"   # purple
C_FUNC = "38;2;166;226;46"      # green
C_BUILTIN = "38;2;102;217;239"  # cyan
C_OP = "38;2;248;248;242"       # white
C_TAG = "38;2;249;38;114"       # pink
C_ATTR = "38;2;166;226;46"      # green
C_KEY = "38;2;249;38;114"       # pink
C_ADD = "38;2;166;226;46"       # green
C_DEL = "38;2;249;38;114"       # pink
C_HUNK = "38;2;102;217;239"     # cyan


def _paint(code: str, text: str, enabled: bool) -> str:
    if not enabled or not text:
        return text
    return f"\033[{code}m{text}{RESET}"


ALIASES = {
    "py": "python",
    "python3": "python",
    "py3": "python",
    "js": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "sh": "bash",
    "shell": "bash",
    "zsh": "bash",
    "console": "bash",
    "yml": "yaml",
    "htm": "html",
    "xml": "html",
    "svg": "html",
    "c++": "cpp",
    "cc": "cpp",
    "h": "c",
    "hpp": "cpp",
    "cs": "csharp",
    "c#": "csharp",
    "rs": "rust",
    "golang": "go",
    "patch": "diff",
    "udiff": "diff",
    "toml": "toml",
}

PY_KEYWORDS = frozenset(
    """False None True and as assert async await break class continue def del elif else
    except finally for from global if import in is lambda nonlocal not or pass raise
    return try while with yield match case""".split()
)
PY_BUILTINS = frozenset(
    """abs all any ascii bin bool breakpoint bytearray bytes callable chr classmethod
    compile complex delattr dict dir divmod enumerate eval exec filter float format
    frozenset getattr globals hasattr hash help hex id input int isinstance issubclass
    iter len list locals map max memoryview min next object oct open ord pow print
    property range repr reversed round set setattr slice sorted staticmethod str sum
    super tuple type vars zip Exception ValueError TypeError KeyError IndexError
    RuntimeError OSError self cls""".split()
)

C_KEYWORDS = frozenset(
    """auto break case char const continue default do double else enum extern float for
    goto if inline int long register restrict return short signed sizeof static struct
    switch typedef union unsigned void volatile while class public private protected
    virtual override final namespace using template typename new delete this throw try
    catch bool true false nullptr NULL""".split()
)
C_BUILTINS = frozenset(
    """printf scanf malloc free strlen strcmp memcpy memset size_t uint8_t uint16_t
    uint32_t uint64_t int8_t int16_t int32_t int64_t std string vector map set cout cin
    endl""".split()
)

JS_KEYWORDS = frozenset(
    """break case catch class const continue debugger default delete do else export
    extends finally for function if import in instanceof let new return super switch
    this throw try typeof var void while with yield async await of static get set
    from as interface type enum implements declare readonly public private protected
    namespace abstract""".split()
)
JS_BUILTINS = frozenset(
    """console document window globalThis Math JSON Object Array String Number Boolean
    Promise Map Set WeakMap Date RegExp Error Symbol fetch setTimeout setInterval
    clearTimeout require module exports process Buffer parseInt parseFloat isNaN""".split()
)

BASH_KEYWORDS = frozenset(
    """if then else elif fi for while until do done case esac function in select time
    coproc return exit break continue local export readonly declare unset shift source
    alias""".split()
)
BASH_BUILTINS = frozenset(
    """echo printf cd pwd ls mkdir rmdir rm cp mv cat grep sed awk curl wget git python3
    python pip node npm pkg apt sudo chmod chown tar zip unzip ssh scp test read set
    eval exec trap true false""".split()
)

GO_KEYWORDS = frozenset(
    """break case chan const continue default defer else fallthrough for func go goto if
    import interface map package range return select struct switch type var""".split()
)
GO_BUILTINS = frozenset(
    """append cap close complex copy delete imag len make new panic print println real
    recover bool byte error float32 float64 int int8 int16 int32 int64 rune string
    uint uint8 uint16 uint32 uint64 uintptr true false nil""".split()
)

RUST_KEYWORDS = frozenset(
    """as async await break const continue crate dyn else enum extern false fn for if impl
    in let loop match mod move mut pub ref return self Self static struct super trait
    true type unsafe use where while""".split()
)
RUST_BUILTINS = frozenset(
    """String Vec Option Some None Result Ok Err Box Rc Arc HashMap HashSet i8 i16 i32
    i64 u8 u16 u32 u64 f32 f64 usize isize bool char str print println format vec""".split()
)

SQL_KEYWORDS = frozenset(
    """select from where insert into values update set delete create table alter drop
    index view join inner left right full outer on as and or not null is in between
    like order by group having limit offset distinct union all case when then else end
    primary key foreign references default unique check database if exists begin commit
    rollback grant revoke""".split()
)

LANGS = {
    "python": {
        "keywords": PY_KEYWORDS,
        "builtins": PY_BUILTINS,
        "constants": frozenset(),
        "line_comment": "#",
        "block_comments": (),
        "strings": ('"""', "'''", '"', "'"),
    },
    "javascript": {
        "keywords": JS_KEYWORDS,
        "builtins": JS_BUILTINS,
        "constants": frozenset(("true", "false", "null", "undefined", "NaN", "Infinity")),
        "line_comment": "//",
        "block_comments": (("/*", "*/"),),
        "strings": ('"', "'", "`"),
    },
    "typescript": {
        "keywords": JS_KEYWORDS,
        "builtins": JS_BUILTINS,
        "constants": frozenset(("true", "false", "null", "undefined", "NaN", "Infinity")),
        "line_comment": "//",
        "block_comments": (("/*", "*/"),),
        "strings": ('"', "'", "`"),
    },
    "c": {
        "keywords": C_KEYWORDS,
        "builtins": C_BUILTINS,
        "constants": frozenset(),
        "line_comment": "//",
        "block_comments": (("/*", "*/"),),
        "strings": ('"', "'"),
    },
    "cpp": {
        "keywords": C_KEYWORDS,
        "builtins": C_BUILTINS,
        "constants": frozenset(),
        "line_comment": "//",
        "block_comments": (("/*", "*/"),),
        "strings": ('"', "'"),
    },
    "csharp": {
        "keywords": C_KEYWORDS
        | frozenset("string var async await foreach get set namespace using".split()),
        "builtins": C_BUILTINS,
        "constants": frozenset(("true", "false", "null")),
        "line_comment": "//",
        "block_comments": (("/*", "*/"),),
        "strings": ('"', "'"),
    },
    "java": {
        "keywords": C_KEYWORDS
        | frozenset("abstract assert boolean byte extends final implements import instanceof interface package synchronized throws transient".split()),
        "builtins": C_BUILTINS,
        "constants": frozenset(("true", "false", "null")),
        "line_comment": "//",
        "block_comments": (("/*", "*/"),),
        "strings": ('"', "'"),
    },
    "go": {
        "keywords": GO_KEYWORDS,
        "builtins": GO_BUILTINS,
        "constants": frozenset(("true", "false", "nil", "iota")),
        "line_comment": "//",
        "block_comments": (("/*", "*/"),),
        "strings": ('"', "`", "'"),
    },
    "rust": {
        "keywords": RUST_KEYWORDS,
        "builtins": RUST_BUILTINS,
        "constants": frozenset(("true", "false")),
        "line_comment": "//",
        "block_comments": (("/*", "*/"),),
        "strings": ('"', "'"),
    },
    "bash": {
        "keywords": BASH_KEYWORDS,
        "builtins": BASH_BUILTINS,
        "constants": frozenset(("true", "false")),
        "line_comment": "#",
        "block_comments": (),
        "strings": ('"', "'"),
    },
    "ruby": {
        "keywords": frozenset(
            "def end if elsif else unless while until for in do class module return yield begin rescue ensure raise self nil true false".split()
        ),
        "builtins": frozenset("puts print p require attr_accessor new".split()),
        "constants": frozenset(("true", "false", "nil")),
        "line_comment": "#",
        "block_comments": (),
        "strings": ('"', "'"),
    },
    "php": {
        "keywords": frozenset(
            "function class public private protected static return if else elseif foreach for while do switch case break continue new echo require include use namespace try catch throw".split()
        ),
        "builtins": frozenset("array count strlen str_replace explode implode isset empty".split()),
        "constants": frozenset(("true", "false", "null")),
        "line_comment": "//",
        "block_comments": (("/*", "*/"),),
        "strings": ('"', "'"),
    },
    "sql": {
        "keywords": SQL_KEYWORDS,
        "builtins": frozenset(),
        "constants": frozenset(("null", "true", "false")),
        "line_comment": "--",
        "block_comments": (("/*", "*/"),),
        "strings": ("'", '"'),
    },
    "generic": {
        "keywords": frozenset(),
        "builtins": frozenset(),
        "constants": frozenset(),
        "line_comment": None,
        "block_comments": (("/*", "*/"),),
        "strings": ('"', "'", "`"),
    },
}

TOKEN_RE = re.compile(
    r"@[A-Za-z_][A-Za-z0-9_.]*"
    r"|\$[A-Za-z_][A-Za-z0-9_]*"
    r"|\$\{[^}]*\}"
    r"|[A-Za-z_][A-Za-z0-9_]*"
    r"|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
    r"|==|!=|<=|>=|=>|->|::|\*\*|//"
    r"|[+\-*/%=<>!&|^~?:;,.(){}\[\]]"
)

VALUE_RE = re.compile(
    r'"(?:\\.|[^"\\])*"'
    r"|'(?:\\.|[^'\\])*'"
    r"|#[0-9a-fA-F]{3,8}\b"
    r"|\b(?:true|false|null|yes|no|on|off)\b"
    r"|-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b"
)


def normalize_lang(lang: str | None) -> str:
    key = (lang or "").strip().lower().split()[0] if (lang or "").strip() else "generic"
    key = ALIASES.get(key, key)
    return key if key in LANGS or key in ("json", "yaml", "toml", "html", "css", "diff") else "generic"


def new_state() -> dict:
    return {"block_end": None, "str_end": None, "html_comment": False, "css_block": False}


def _find_close(line: str, start: int, delim: str) -> int:
    j = start
    while j < len(line):
        if delim in ('"', "'") and line[j] == "\\":
            j += 2
            continue
        if line.startswith(delim, j):
            return j + len(delim)
        j += 1
    return -1


def _special_at(line: str, i: int, cfg: dict) -> bool:
    lc = cfg.get("line_comment")
    if lc and line.startswith(lc, i):
        return True
    for start, _end in cfg.get("block_comments", ()):
        if line.startswith(start, i):
            return True
    for quote in cfg.get("strings", ()):
        if line.startswith(quote, i):
            return True
    return False


def _classify(token: str, segment: str, end: int, cfg: dict, enabled: bool) -> str:
    if token.startswith("$"):
        return _paint(C_ATTR, token, enabled)
    if token.startswith("@"):
        return _paint(C_KEYWORD, token, enabled)
    if token[0].isdigit():
        return _paint(C_NUMBER, token, enabled)
    if token in cfg.get("constants", ()):
        return _paint(C_NUMBER, token, enabled)
    if token in cfg.get("keywords", ()):
        return _paint(C_KEYWORD, token, enabled)
    if token in cfg.get("builtins", ()):
        return _paint(C_BUILTIN, token, enabled)
    k = end
    while k < len(segment) and segment[k] == " ":
        k += 1
    if k < len(segment) and segment[k] == "(":
        return _paint(C_FUNC, token, enabled)
    if token[0] in "+-*/%=<>!&|^~?:;,.()[]{}":
        return _paint(C_OP, token, enabled)
    return token


def _segment(segment: str, cfg: dict, enabled: bool) -> str:
    out = []
    last = 0
    for match in TOKEN_RE.finditer(segment):
        if match.start() > last:
            out.append(segment[last:match.start()])
        out.append(_classify(match.group(), segment, match.end(), cfg, enabled))
        last = match.end()
    if last < len(segment):
        out.append(segment[last:])
    return "".join(out)


def _generic(line: str, cfg: dict, state: dict, enabled: bool) -> str:
    out = []
    i = 0
    n = len(line)
    while i < n:
        if state["block_end"]:
            end = line.find(state["block_end"], i)
            if end < 0:
                out.append(_paint(C_COMMENT, line[i:], enabled))
                return "".join(out)
            end += len(state["block_end"])
            out.append(_paint(C_COMMENT, line[i:end], enabled))
            state["block_end"] = None
            i = end
            continue

        if state["str_end"]:
            end = _find_close(line, i, state["str_end"])
            if end < 0:
                out.append(_paint(C_STRING, line[i:], enabled))
                return "".join(out)
            out.append(_paint(C_STRING, line[i:end], enabled))
            state["str_end"] = None
            i = end
            continue

        lc = cfg.get("line_comment")
        if lc and line.startswith(lc, i):
            out.append(_paint(C_COMMENT, line[i:], enabled))
            break

        handled = False
        for start, end_token in cfg.get("block_comments", ()):
            if line.startswith(start, i):
                close = line.find(end_token, i + len(start))
                if close < 0:
                    out.append(_paint(C_COMMENT, line[i:], enabled))
                    state["block_end"] = end_token
                    i = n
                else:
                    close += len(end_token)
                    out.append(_paint(C_COMMENT, line[i:close], enabled))
                    i = close
                handled = True
                break
        if handled:
            continue

        quote = None
        for delim in cfg.get("strings", ()):
            if line.startswith(delim, i):
                quote = delim
                break
        if quote:
            end = _find_close(line, i + len(quote), quote)
            if end < 0:
                out.append(_paint(C_STRING, line[i:], enabled))
                state["str_end"] = quote
                i = n
            else:
                out.append(_paint(C_STRING, line[i:end], enabled))
                i = end
            continue

        j = i + 1
        while j < n and not _special_at(line, j, cfg):
            j += 1
        out.append(_segment(line[i:j], cfg, enabled))
        i = j
    return "".join(out)


def _value(text: str, enabled: bool) -> str:
    out = []
    last = 0
    for match in VALUE_RE.finditer(text):
        if match.start() > last:
            out.append(text[last:match.start()])
        token = match.group()
        if token[0] in "\"'":
            out.append(_paint(C_STRING, token, enabled))
        else:
            out.append(_paint(C_NUMBER, token, enabled))
        last = match.end()
    if last < len(text):
        out.append(text[last:])
    return "".join(out)


def _split_comment(line: str, marker: str) -> tuple[str, str]:
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif line.startswith(marker, i):
            return line[:i], line[i:]
        i += 1
    return line, ""


def _json(line: str, enabled: bool) -> str:
    out = []
    i = 0
    n = len(line)
    while i < n:
        if line[i] == '"':
            j = i + 1
            while j < n:
                if line[j] == "\\":
                    j += 2
                    continue
                if line[j] == '"':
                    j += 1
                    break
                j += 1
            token = line[i:j]
            rest = line[j:].lstrip()
            out.append(_paint(C_KEY if rest.startswith(":") else C_STRING, token, enabled))
            i = j
            continue
        match = re.match(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", line[i:])
        if match:
            out.append(_paint(C_NUMBER, match.group(), enabled))
            i += len(match.group())
            continue
        match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", line[i:])
        if match:
            word = match.group()
            out.append(
                _paint(C_NUMBER, word, enabled)
                if word in ("true", "false", "null")
                else word
            )
            i += len(word)
            continue
        out.append(_paint(C_OP, line[i], enabled) if line[i] in "{}[],:" else line[i])
        i += 1
    return "".join(out)


def _yaml(line: str, enabled: bool) -> str:
    code, comment = _split_comment(line, "#")
    out = []
    match = re.match(r"^(\s*)([^:#\n][^:\n]*?)(\s*:)(\s*)(.*)$", code)
    if match:
        out.append(match.group(1))
        out.append(_paint(C_KEY, match.group(2), enabled))
        out.append(_paint(C_OP, match.group(3), enabled))
        out.append(match.group(4))
        out.append(_value(match.group(5), enabled))
    else:
        dash = re.match(r"^(\s*)(-)(\s+)(.*)$", code)
        if dash:
            out.append(dash.group(1))
            out.append(_paint(C_OP, dash.group(2), enabled))
            out.append(dash.group(3))
            out.append(_value(dash.group(4), enabled))
        else:
            out.append(_value(code, enabled))
    if comment:
        out.append(_paint(C_COMMENT, comment, enabled))
    return "".join(out)


def _tag_end(line: str, start: int) -> int:
    quote = None
    i = start + 1
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == ">":
            return i + 1
        i += 1
    return -1


def _tag(tag: str, enabled: bool) -> str:
    match = re.match(r"(</?)([A-Za-z][\w:-]*)(.*?)(/?>)$", tag)
    if not match:
        return _paint(C_TAG, tag, enabled)
    out = [_paint(C_OP, match.group(1), enabled), _paint(C_TAG, match.group(2), enabled)]
    rest = match.group(3)
    i = 0
    while i < len(rest):
        ch = rest[i]
        if ch in "\"'":
            j = i + 1
            while j < len(rest):
                if rest[j] == "\\":
                    j += 2
                    continue
                if rest[j] == ch:
                    j += 1
                    break
                j += 1
            out.append(_paint(C_STRING, rest[i:j], enabled))
            i = j
        elif ch.isalpha() or ch in "_-:@":
            j = i + 1
            while j < len(rest) and (rest[j].isalnum() or rest[j] in "_-:@"):
                j += 1
            out.append(_paint(C_ATTR, rest[i:j], enabled))
            i = j
        else:
            out.append(_paint(C_OP, ch, enabled) if ch in "=/<>" else ch)
            i += 1
    out.append(_paint(C_OP, match.group(4), enabled))
    return "".join(out)


def _html(line: str, state: dict, enabled: bool) -> str:
    out = []
    i = 0
    n = len(line)
    while i < n:
        if state["html_comment"]:
            end = line.find("-->", i)
            if end < 0:
                out.append(_paint(C_COMMENT, line[i:], enabled))
                return "".join(out)
            out.append(_paint(C_COMMENT, line[i:end + 3], enabled))
            state["html_comment"] = False
            i = end + 3
            continue
        if line.startswith("<!--", i):
            end = line.find("-->", i + 4)
            if end < 0:
                out.append(_paint(C_COMMENT, line[i:], enabled))
                state["html_comment"] = True
                return "".join(out)
            out.append(_paint(C_COMMENT, line[i:end + 3], enabled))
            i = end + 3
            continue
        if line[i] == "<":
            end = _tag_end(line, i)
            if end < 0:
                out.append(_paint(C_TAG, line[i:], enabled))
                return "".join(out)
            out.append(_tag(line[i:end], enabled))
            i = end
            continue
        j = line.find("<", i)
        if j < 0:
            out.append(line[i:])
            break
        out.append(line[i:j])
        i = j
    return "".join(out)


def _css(line: str, state: dict, enabled: bool) -> str:
    if state["block_end"]:
        end = line.find("*/")
        if end < 0:
            return _paint(C_COMMENT, line, enabled)
        state["block_end"] = None
        return _paint(C_COMMENT, line[:end + 2], enabled) + _css(line[end + 2:], state, enabled)

    start = line.find("/*")
    if start >= 0:
        end = line.find("*/", start + 2)
        if end < 0:
            state["block_end"] = "*/"
            return line[:start] + _paint(C_COMMENT, line[start:], enabled)
        return (
            _css(line[:start], state, enabled)
            + _paint(C_COMMENT, line[start:end + 2], enabled)
            + _css(line[end + 2:], state, enabled)
        )

    stripped = line.strip()
    if stripped.startswith("}"):
        state["css_block"] = False
        return line.replace("}", _paint(C_OP, "}", enabled))

    if "{" in line:
        state["css_block"] = True
        head, rest = line.split("{", 1)
        return _paint(C_KEY, head, enabled) + _paint(C_OP, "{", enabled) + _value(rest, enabled)

    if stripped.startswith("@"):
        return _paint(C_KEYWORD, line, enabled)

    if state["css_block"] and ":" in line:
        prop, value = line.split(":", 1)
        return _paint(C_KEY, prop, enabled) + _paint(C_OP, ":", enabled) + _value(value, enabled)

    return _value(line, enabled)


def _toml(line: str, enabled: bool) -> str:
    code, comment = _split_comment(line, "#")
    stripped = code.strip()
    out: list[str] = []
    if stripped.startswith("[") and stripped.endswith("]"):
        out.append(_paint(C_KEYWORD, code, enabled))
    else:
        match = re.match(r"^(\s*)([^=\n]+?)(\s*)(=)(\s*)(.*)$", code)
        if match:
            out.append(match.group(1))
            out.append(_paint(C_KEY, match.group(2), enabled))
            out.append(match.group(3))
            out.append(_paint(C_OP, match.group(4), enabled))
            out.append(match.group(5))
            out.append(_value(match.group(6), enabled))
        else:
            out.append(_value(code, enabled))
    if comment:
        out.append(_paint(C_COMMENT, comment, enabled))
    return "".join(out)


def _diff(line: str, enabled: bool) -> str:
    if line.startswith(("diff ", "index ", "--- ", "+++ ")):
        return _paint("38;2;248;248;242", line, enabled)
    if line.startswith("@@"):
        return _paint(C_HUNK, line, enabled)
    if line.startswith("+"):
        return _paint(C_ADD, line, enabled)
    if line.startswith("-"):
        return _paint(C_DEL, line, enabled)
    return line


def highlight_line(
    line: str, lang: str | None, state: dict | None = None, enabled: bool = True
) -> str:
    """Красит одну строку кода. state переносится между строками блока."""
    lang = normalize_lang(lang)
    if state is None:
        state = new_state()
    if lang == "json":
        return _json(line, enabled)
    if lang in ("yaml", "toml"):
        return _toml(line, enabled) if lang == "toml" else _yaml(line, enabled)
    if lang == "html":
        return _html(line, state, enabled)
    if lang == "css":
        return _css(line, state, enabled)
    if lang == "diff":
        return _diff(line, enabled)
    return _generic(line, LANGS.get(lang, LANGS["generic"]), state, enabled)
