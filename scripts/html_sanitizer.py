"""Turn HTML mail into a document that shows everything but runs nothing.

This is a personal viewer, so nothing is hidden for privacy reasons: remote
images, stylesheets, fonts, backgrounds, media, embedded frames and form
controls are all kept so the mail looks the way the sender designed it.

Only *active* content is removed, because it could act on this local app:
``<script>``, ``on*`` event handlers, ``javascript:``/``vbscript:`` URLs and
``data:`` documents. Two more layers make sure nothing can run even if the
markup is unusual: the document is served with ``script-src 'none'`` and shown
in an iframe whose ``sandbox`` does not allow scripts, form submission or
navigation of the app. Links open in a new tab.
"""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser
from typing import Callable

# Dropped together with everything inside them.
DROP_CONTENT_TAGS = {"script", "title", "template"}
# Dropped, but their content is kept (document-level wrappers; <noscript>
# content is shown because scripts never run here).
UNWRAP_TAGS = {"html", "head", "body", "noscript", "frameset", "frame", "plaintext"}
# Dropped without content: document metadata, plugins, and SVG animation
# elements (they can rewrite an href to javascript: after sanitizing).
DROP_TAGS = {"meta", "base", "embed", "applet", "param", "animate", "set", "animatemotion",
             "animatetransform", "handler", "listener"}
# Elements whose name/id become properties of ``document`` (DOM clobbering).
# The app reads the frame's document to size it, so those attributes go.
CLOBBER_ATTRS = {"form": {"name"}, "img": {"name"}, "iframe": {"name"}, "embed": {"name"},
                 "object": {"name", "id"}}
# URL attributes on these tags load or open documents, so data: is not allowed.
DOCUMENT_URL_TAGS = {"a", "area", "form", "button", "input", "iframe", "object", "frame"}
_NAME = re.compile(r"[a-z][-a-z0-9_:.]*")
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
    "source", "track", "wbr",
}
URL_ATTRS = {
    "href", "src", "action", "formaction", "background", "poster", "data", "cite", "longdesc",
    "lowsrc", "dynsrc", "xlink:href", "manifest", "icon",
}
# Attributes removed everywhere (besides on*): inline documents and beacons.
DROP_ATTRS = {"srcdoc", "ping", "http-equiv"}
MEDIA_DATA = re.compile(r"data:(?:image|audio|video|font)/[a-z0-9.+-]+[;,]", re.I)
_CONTROL_CHARS = re.compile(r"[\x00-\x20\x7f]+")
_SCHEME = re.compile(r"^([a-z][a-z0-9+.-]*):", re.I)
_CSS_URL = re.compile(r"""url\(\s*(['"]?)\s*(cid:[^'")\s]+)\s*\1\s*\)""", re.I)
_BLOCKED_SCHEMES = {"javascript", "vbscript", "livescript", "mocha", "jscript"}


def _compact(value: str) -> str:
    # Browsers ignore whitespace/control characters inside a scheme
    # ("java\tscript:"), so check the URL with them removed.
    return _CONTROL_CHARS.sub("", value or "")


def url_allowed(value: str, media: bool) -> bool:
    """False for URLs that execute code or load a document from data."""
    match = _SCHEME.match(_compact(value))
    if not match:
        return True  # Relative URL or fragment.
    scheme = match.group(1).lower()
    if scheme in _BLOCKED_SCHEMES:
        return False
    if scheme == "data":
        return media and bool(MEDIA_DATA.match(_compact(value)))
    return True


class RenderedHtml:
    __slots__ = ("body", "body_attrs", "base_href", "used_cids")

    def __init__(self, body: str, body_attrs: list[str], base_href: str, used_cids: set[str]) -> None:
        self.body = body
        self.body_attrs = body_attrs
        self.base_href = base_href
        self.used_cids = used_cids


class _Neutralizer(HTMLParser):
    def __init__(self, resolve_cid: Callable[[str], str | None]) -> None:
        super().__init__(convert_charrefs=True)
        self._resolve_cid = resolve_cid
        self.out: list[str] = []
        self.body_attrs: list[str] = []
        self.base_href = ""
        self._drop_tag: str | None = None
        self._drop_depth = 0
        self._raw_tag: str | None = None  # Inside <style>: data is CSS, not text.

    # ------------------------------------------------------------------ helpers
    def css(self, value: str) -> str:
        """Resolve cid: images in CSS; "</" cannot occur inside a style block."""
        def replace(match: re.Match) -> str:
            resolved = self._resolve_cid(match.group(2))
            return f'url("{resolved}")' if resolved else "none"

        return _CSS_URL.sub(replace, value).replace("</", "<\\/")

    def _url(self, value: str, media: bool) -> str | None:
        url = (value or "").strip()
        if _compact(url).lower().startswith("cid:"):
            return self._resolve_cid(_compact(url))
        if url.startswith("//"):
            url = "https:" + url
        return url if url_allowed(url, media) else None

    def _srcset(self, value: str) -> str:
        candidates = []
        for candidate in value.split(","):
            parts = candidate.strip().split(None, 1)
            if not parts:
                continue
            url = self._url(parts[0], media=True)
            if url:
                candidates.append(" ".join([url, *parts[1:]]))
        return ", ".join(candidates)

    def _attributes(self, tag: str, attrs: list[tuple[str, str | None]]) -> list[str]:
        kept: list[str] = []
        for name, value in attrs:
            name = (name or "").strip().lower()
            if not name or name.startswith("on") or name in DROP_ATTRS or not re.fullmatch(r"[a-z_:][-a-z0-9_:.]*", name):
                continue
            if tag in {"a", "area", "form"} and name in {"target", "rel"}:
                continue  # Forced below.
            if name in CLOBBER_ATTRS.get(tag, ()):
                continue
            value = value or ""
            if name in URL_ATTRS:
                value = self._url(value, media=name != "href" and tag not in DOCUMENT_URL_TAGS)
                if value is None:
                    continue
            elif name == "srcset":
                value = self._srcset(value)
            elif name == "style":
                value = self.css(value)
            kept.append(f'{name}="{escape(value, quote=True)}"')
        if tag in {"a", "area"}:
            kept.append('target="_blank" rel="noopener noreferrer"')
        return kept

    # ------------------------------------------------------------------ parser hooks
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._drop_depth:
            if tag == self._drop_tag:
                self._drop_depth += 1
            return
        if tag in DROP_CONTENT_TAGS:
            self._drop_tag, self._drop_depth = tag, 1
            return
        if tag == "body" and not self.body_attrs:
            self.body_attrs = self._attributes(tag, attrs)
            return
        if tag == "base":
            href = dict(attrs).get("href") or ""
            if not self.base_href and _compact(href).lower().startswith(("http://", "https://")):
                self.base_href = href.strip()
            return
        if tag in UNWRAP_TAGS or tag in DROP_TAGS or not _NAME.fullmatch(tag):
            return
        if tag == "link":
            values = dict(attrs)
            rel = (values.get("rel") or "").lower().split()
            href = values.get("href") or ""
            if "stylesheet" in rel and _compact(href).lower().startswith(("http://", "https://", "//")):
                self.out.append(f'<link rel="stylesheet" href="{escape(self._url(href, True), quote=True)}">')
            return
        kept = self._attributes(tag, attrs)
        self.out.append(f"<{tag}{''.join(' ' + k for k in kept)}>")
        if tag == "style":
            self._raw_tag = tag

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        tag = tag.lower()
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._drop_depth:
            if tag == self._drop_tag:
                self._drop_depth -= 1
            return
        if tag in DROP_CONTENT_TAGS or tag in UNWRAP_TAGS or tag in DROP_TAGS or tag in VOID_TAGS:
            return
        if not _NAME.fullmatch(tag):
            return
        self._raw_tag = None if tag == self._raw_tag else self._raw_tag
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self._drop_depth:
            return
        if self._raw_tag:
            self.out.append(self.css(data))
        else:
            self.out.append(escape(data, quote=False))

    def handle_comment(self, data):
        pass  # Includes Outlook conditional comments (<!--[if mso]>).

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass


def neutralize(html: str, inline_images: dict[str, str] | None = None) -> RenderedHtml:
    """Remove active content from ``html``.

    ``inline_images`` maps a Content-ID (lower case, without angle brackets)
    to a ``data:`` URI used for ``cid:`` references.
    """
    images = inline_images or {}
    used: set[str] = set()

    def resolve(reference: str) -> str | None:
        key = reference[4:].strip("<>").lower()
        if key in images:
            used.add(key)
            return images[key]
        return None

    parser = _Neutralizer(resolve)
    parser.feed(html or "")
    parser.close()
    return RenderedHtml("".join(parser.out), parser.body_attrs, parser.base_href, used)


_URL_IN_TEXT = re.compile(r"(https?://[^\s<>\"']+[^\s<>\"'.,;:!?)\]}'])", re.I)


def text_to_html(text: str) -> str:
    """Plain-text part shown as rich text: whitespace kept, links clickable."""
    pieces = []
    for index, chunk in enumerate(_URL_IN_TEXT.split(text or "")):
        if index % 2:
            url = escape(chunk, quote=True)
            pieces.append(f'<a href="{url}" target="_blank" rel="noopener noreferrer">{escape(chunk, quote=False)}</a>')
        else:
            pieces.append(escape(chunk, quote=False))
    return f'<div class="mail-text">{"".join(pieces)}</div>'


BASE_STYLE = (
    "html{color-scheme:light}"
    "body{margin:0;padding:4px 2px;color:#1d2420;background:#fff;overflow-x:auto;"
    "font:14px/1.7 -apple-system,'SF Pro Text','Helvetica Neue','PingFang SC',sans-serif;"
    "overflow-wrap:break-word}"
    "img{max-width:100%;height:auto}"
    ".mail-text{white-space:pre-wrap;overflow-wrap:anywhere}"
    ".mail-part+.mail-part{margin-top:24px;padding-top:20px;border-top:1px solid #dfe4de}"
    ".mail-attachments{margin-top:24px;padding-top:16px;border-top:1px solid #dfe4de;color:#566158;font-size:12px}"
    ".mail-attachments figure{margin:12px 0}"
    ".mail-attachments img{display:block;margin-bottom:6px}"
    ".mail-forward{margin-top:16px;padding:12px 14px;border:1px solid #dfe4de;border-radius:6px}"
    ".mail-forward-head{margin-bottom:10px;color:#566158;font-size:12px}"
)


def document(body: str, body_attrs: list[str] | None = None, base_href: str = "") -> str:
    """Wrap rendered parts in a standalone HTML document."""
    base = f'<base href="{escape(base_href, quote=True)}" target="_blank">' if base_href else '<base target="_blank">'
    attrs = "".join(" " + a for a in (body_attrs or []))
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"{base}<style>{BASE_STYLE}</style></head><body{attrs}>{body}</body></html>"
    )
