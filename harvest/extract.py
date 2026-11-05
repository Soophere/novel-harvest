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

# --------------------------------------------------------------------------- #
# Paragraph mode: the robust default for modern sites
# --------------------------------------------------------------------------- #
class _ParaCollector(HTMLParser):
    """Group <p> text by the nearest ancestor that carries a class or id.

    Modern sites (WordPress/Madara, Tailwind, Astro) put the story in plain <p> tags inside one container,
    and navigation in <a>/<li>. Measured by total paragraph characters, the story container wins by an
    order of magnitude, and choosing paragraphs instead of a whole block drops the menu, the breadcrumb and
    the "Prev / Next" row without any per-site selector.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []          # list of (tag, key)
        self.skip = 0
        self.in_p = 0
        self.buf = []
        self.groups = {}         # key -> list of paragraph strings
        self.order = []          # key order of first appearance

    @staticmethod
    def _key(tag, attrs):
        a = dict(attrs)
        if a.get("class"):
            return tag + "." + a["class"].split()[0]
        if a.get("id"):
            return tag + "#" + a["id"]
        return None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self.skip += 1
            return
        if tag == "p":
            self.in_p += 1
            self.buf = []
        self.stack.append((tag, self._key(tag, attrs)))

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg"):
            self.skip = max(0, self.skip - 1)
            return
        if tag == "p" and self.in_p:
            self.in_p -= 1
            text = re.sub(r"\s+", " ", "".join(self.buf)).strip()
            self.buf = []
            if len(text) >= 2:
                key = None
                for t, k in reversed(self.stack):
                    if k:
                        key = k
                        break
                key = key or "root"
                if key not in self.groups:
                    self.groups[key] = []
                    self.order.append(key)
                self.groups[key].append(text)
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_p:
            self.buf.append(data)


# Paragraphs that are site furniture, not story. Kept short and exact: a false match would delete a line
# of dialogue, so only strings that cannot be dialogue are listed.
NOISE = {
    "الفصل التالي", "الفصل السابق", "التالي", "السابق", "الفصل السابق التالي",
    "next chapter", "previous chapter", "prev chapter", "next", "prev", "back to list",
    "chapter list", "table of contents", "report error", "share", "comments",
}


def best_paragraphs(raw: str) -> str:
    """Return the story as newline-separated paragraphs, or "" when the page has too few <p> tags."""
    p = _ParaCollector()
    try:
        p.feed(raw)
    except Exception:
        pass
    # score by *prose*: a navigation block is many short paragraphs and out-totals the story otherwise
    # ("next chapter" / "previous" rows are links, and links are short lines)
    best, best_n = "", 0
    for key in p.order:
        paras = p.groups[key]
        n = sum(len(x) for x in paras if len(x) > 40)
        if n > best_n:
            best_n, best = n, paras
    if len(best) < 3 or best_n < 400:
        return ""
    cleaned = [x for x in best if x.strip().lower() not in NOISE]
    if len(cleaned) < 3:
        cleaned = best
    return "\n\n".join(cleaned)
