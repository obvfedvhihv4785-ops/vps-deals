# ILANG
# TYPE:lib PROJECT:vps-deals-promo-radar LANG:zh
#
# ::STATE{@FILE, role:I-Lang配置解析器, scope:只读不抓不渲染}
# ::RULE{本文件只负责把 .ilang/site.ilang 解析成 Python 字典 不做任何抓取和渲染}
# ::RULE{解析失败必须抛异常 不许静默用默认值顶上 否则站点规则就成了摆设}
# ::BOUNDARY{never:在代码里另写一份硬编码的厂商清单|scope:file}

"""Parser for the I-Lang (ilang.ai) config format used by .ilang/site.ilang.

This module is deliberately tiny and dependency-free (stdlib only). It exists so
that scraper.py and build.py share ONE reader for the site config, instead of each
hardcoding its own provider list.

The config file is plain text to the rest of the pipeline: nothing here depends on
an I-Lang runtime. If you deleted this module and swapped in YAML, the site would
still build. I-Lang is used because it can express BOUNDARY (what must never
happen), which plain YAML cannot.
"""

from __future__ import annotations

import re

__all__ = ["ILangError", "load", "SiteConfig"]

_TOKEN_RE = re.compile(
    r"::(STATE|MODULE|RULE|BOUNDARY|OBJECTIVE|FACT|LESSON|ASK|NEVER|MUST)\s*\{([\s\S]*?)\}",
    re.M,
)


class ILangError(Exception):
    """Raised when the config file cannot be parsed. Never swallowed."""


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split on `sep` while ignoring separators inside <> or quotes."""
    parts, buf, depth, quote = [], [], 0, None
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in "<([":
            depth += 1
            buf.append(ch)
        elif ch in ">)]":
            depth -= 1
            buf.append(ch)
        elif ch == sep and depth <= 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _kv(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in pairs:
        if ":" in p:
            k, v = p.split(":", 1)
            out[k.strip()] = v.strip()
    return out


class SiteConfig:
    """Parsed view of an I-Lang file.

    `@SITE` is required only for files that drive the site build. A documentation
    file such as AGENTS.md can declare other states and still parse.
    """

    def __init__(self, raw: dict):
        self.raw = raw
        self.header: dict[str, str] = raw["header"]
        self.states: dict[str, dict[str, str]] = raw["states"]
        self.modules: dict[str, dict] = raw["modules"]
        self.rules: list[str] = raw["rules"]
        self.boundaries: list[str] = raw["boundaries"]

    @property
    def state(self) -> dict[str, str]:
        return self.states.get("SITE", {})

    def _require_site(self, key: str) -> str:
        if "SITE" not in self.states:
            raise ILangError("::STATE{@SITE, ...} missing — brand/niche/domain unknown")
        if key not in self.states["SITE"]:
            raise ILangError(f"::STATE{{@SITE}} is missing required key '{key}'")
        return self.states["SITE"][key]

    # ---- module accessors -------------------------------------------------

    def _body(self, module: str) -> list[str]:
        if module not in self.modules:
            raise ILangError(f"::MODULE{{{module}}} missing from config")
        return self.modules[module]["body"]

    @property
    def providers(self) -> list[dict[str, str]]:
        """PROVIDERS rows: name | homepage | offer_url | affiliate_url (optional)."""
        rows = []
        for line in self._body("PROVIDERS"):
            if line.startswith("[") or line.startswith("#"):
                continue
            cells = [c.strip() for c in line.split("|")]
            if len(cells) < 3:
                raise ILangError(
                    f"PROVIDERS row needs at least 3 cells (name | homepage | offer_url), got: {line!r}"
                )
            while len(cells) < 4:
                cells.append("")
            rows.append({
                "name": cells[0],
                "homepage": cells[1],
                "offer_url": cells[2],
                "affiliate_url": cells[3],
            })
        if not rows:
            raise ILangError("PROVIDERS module is empty — nothing to scrape")
        return rows

    @property
    def fields(self) -> list[str]:
        """FIELDS module: whitespace-separated field names."""
        return [t for line in self._body("FIELDS") for t in line.split()]

    def settings(self) -> dict[str, str]:
        """SETTINGS module: key: value lines."""
        return _kv(self._body("SETTINGS"))

    def render(self) -> dict[str, str]:
        """RENDER module: key: value lines describing which pages to emit."""
        return _kv(self._body("RENDER"))

    # ---- derived helpers --------------------------------------------------

    @property
    def brand(self) -> str:
        return self._require_site("brand")

    @property
    def niche(self) -> str:
        return self._require_site("niche")

    @property
    def domain(self) -> str:
        return self._require_site("domain")

    @property
    def base_url(self) -> str:
        d = self.domain.rstrip("/")
        if not d.startswith("http"):
            d = "https://" + d
        return d

    @property
    def site_name(self) -> str:
        s = self.settings()
        return s.get("site_name") or self.brand


def load(path) -> SiteConfig:
    """Parse an I-Lang config file. Raises ILangError on any structural problem."""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            text = fh.read()
    except OSError as exc:
        raise ILangError(f"cannot read config {path}: {exc}") from exc

    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    if not lines or not lines[0].strip().upper().startswith("ILANG"):
        raise ILangError("first line must be the ILANG header (no version number)")

    # Second non-empty, non-comment line carries TYPE / PROJECT / LANG.
    header: dict[str, str] = {}
    for ln in lines[1:6]:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("::"):
            break
        header = _kv([p.strip() for p in s.split() if ":" in p])
        if header:
            break
    if not header:
        raise ILangError("second line must declare TYPE / PROJECT / LANG")

    # Drop comments and blanks, then scan the whole document for tokens. Tokens
    # may span several lines, so this works on joined text rather than per line.
    kept = [ln.strip() for ln in lines[2:] if ln.strip() and not ln.strip().startswith("#")]
    body_text = "\n".join(kept)

    states: dict[str, dict[str, str]] = {}
    modules: dict[str, dict] = {}
    rules: list[str] = []
    boundaries: list[str] = []
    current_module: str | None = None

    def absorb(chunk: str) -> None:
        if current_module and chunk.strip():
            modules[current_module]["body"].extend(
                ln.strip() for ln in chunk.split("\n") if ln.strip())

    pos = 0
    for m in _TOKEN_RE.finditer(body_text):
        absorb(body_text[pos:m.start()])
        pos = m.end()
        kind, inner = m.group(1), m.group(2).strip()
        current_module = None
        if kind == "STATE":
            parts = _split_top_level(inner)
            if parts and parts[0].startswith("@"):
                states[parts[0].lstrip("@").strip()] = _kv(parts[1:])
            else:
                states["SITE"] = _kv(parts)
        elif kind == "MODULE":
            parts = _split_top_level(inner, sep="|")
            name = parts[0].strip()
            modules[name] = {"meta": _kv(parts[1:]), "body": []}
            current_module = name
        elif kind == "RULE":
            rules.append(inner)
        elif kind == "BOUNDARY":
            boundaries.append(inner)
    absorb(body_text[pos:])

    return SiteConfig({"header": header, "states": states, "modules": modules,
                       "rules": rules, "boundaries": boundaries})


if __name__ == "__main__":  # tiny self-check
    import json
    import sys
    cfg = load(sys.argv[1] if len(sys.argv) > 1 else ".ilang/site.ilang")
    print(json.dumps({
        "header": cfg.header,
        "site": cfg.state,
        "providers": cfg.providers,
        "fields": cfg.fields,
        "settings": cfg.settings(),
        "render": cfg.render(),
        "rules": cfg.rules,
        "boundaries": cfg.boundaries,
    }, ensure_ascii=False, indent=2))
