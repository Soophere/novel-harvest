"""extract.py — pull the story text out of any chapter page, without per-site selectors.

Sites differ in markup but agree on one thing: the chapter is the biggest block of text on the page.
This walks the HTML, measures how much readable text each <div>/<article>/<section> holds, and returns
the richest one. It also keeps paragraph breaks, which a naive tag-strip loses.

    from extract import best_text
    title, body = best_text(html_string)
"""
import html
import re
from html.parser import HTMLParser

BLOCK = {"div", "article", "section", "main"}
BREAK = {"p", "br", "h1", "h2", "h3", "h4", "li", "tr"}


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []          # tag names
        self.bufs = {}           # depth -> list of text pieces
        self.best = (0, "")
        self.skip = 0            # inside <script>/<style>
        self.title = ""
        self.in_h1 = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
            return
        if tag == "h1":
            self.in_h1 = True
        if tag in BLOCK:
            self.bufs[len(self.stack)] = []
            self.stack.append(tag)
        elif tag in BREAK:
            if self.stack:
                self.bufs.setdefault(len(self.stack) - 1, []).append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.skip = max(0, self.skip - 1)
            return
        if tag == "h1":
            self.in_h1 = False
        if tag in BLOCK and self.stack and self.stack[-1] == tag:
            self.stack.pop()
            pieces = self.bufs.pop(len(self.stack), [])
            text = "".join(pieces)
            # score by *prose*, not by raw size: menus and chapter lists are long but made of short lines
            prose = sum(len(x.strip()) for x in text.split("\n") if len(x.strip()) > 40)
            if prose > self.best[0]:
                self.best = (prose, text)
            # bubble the text up to the parent so wrappers are measured correctly
            if self.stack:
                self.bufs.setdefault(len(self.stack) - 1, []).append(text)

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_h1:
            self.title += data
        if self.stack:
            self.bufs.setdefault(len(self.stack) - 1, []).append(data)


def best_text(raw: str) -> tuple[str, str]:
    """Return (title, body) where body is the richest text block with paragraph breaks kept."""
    p = _Collector()
    try:
        p.feed(raw)
    except Exception:
        pass
    body = p.best[1]
    lines = [re.sub(r"[ \t\u00a0]+", " ", x).strip() for x in body.split("\n")]
    lines = [x for x in lines if x]
    title = re.sub(r"\s+", " ", html.unescape(p.title)).strip()
    return title, "\n\n".join(lines)


if __name__ == "__main__":
    import sys
    import urllib.request

    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"}
    raw = urllib.request.urlopen(urllib.request.Request(sys.argv[1], headers=ua), timeout=30).read().decode("utf-8", "replace")
    t, b = best_text(raw)
    print("title:", t[:80])
    print("chars:", len(b))
    print(b[:300])

class _ByClass(HTMLParser):
    """Collect the text of every element whose class contains `needle`, keeping the richest one."""

    def __init__(self, needle):
        super().__init__(convert_charrefs=True)
        self.needle = needle
        self.depth = 0
        self.capture = None
        self.capture_depth = 0
        self.results = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
            return
        a = dict(attrs)
        cls = (a.get("class") or "") + " " + (a.get("id") or "")
        self.depth += 1
        if self.capture is None and self.needle in cls:
            self.capture = []
            self.capture_depth = self.depth
        elif self.capture is not None and tag in ("p", "br"):
            self.capture.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.skip = max(0, self.skip - 1)
            return
        if self.capture is not None and self.depth == self.capture_depth:
            self.results.append("".join(self.capture))
            self.capture = None
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        if self.skip:
            return
        if self.capture is not None:
            self.capture.append(data)


def text_in_class(raw: str, needle: str) -> str:
    p = _ByClass(needle)
    try:
        p.feed(raw)
    except Exception:
        pass
    best = ""
    for r in p.results:
        lines = [re.sub(r"[ \t\u00a0]+", " ", x).strip() for x in r.split("\n")]
        lines = [x for x in lines if x]
        txt = "\n\n".join(lines)
        if len(txt) > len(best):
            best = txt
    return best