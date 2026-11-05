"""improve-pass.py — Pipeline B: make an existing Arabic translation better, using the English as authority.

    python tools/improve-pass.py --pair shadow-slave \
        --en-site wuxiadreams --en-url https://wuxiadreams.com/novel/shadow-slave \
        --ar-site novltime      --ar-url https://novltime.com/ar/novel/shadow-slave \
        --chapters 1 --out out/pipelineB

Why it is shaped like this (his own instructions):
  * the English is read twice and is the authority — the Arabic is improved, not paraphrased
  * paragraph-level prompts, never chapter-level: a whole-chapter prompt makes the model copy
  * common Arabic words over dictionary words; convey the image, not the words
  * a second pass reviews the draft against the English for dropped or invented sentences
  * every changed paragraph lands in a review sheet, so nothing is published unseen

Cost is printed from the token usage the API returns, so the number is measured, not estimated.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "harvest"))
import runner  # noqa: E402

API = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"

# deepseek-chat list price, USD per 1M tokens
PRICE_IN = 0.27
PRICE_OUT = 1.10

RULES = """You are improving an existing Arabic translation of a Chinese web novel for a native Arabic reader.

The English is the authority for meaning. The Arabic you are given is a draft that may be flat, wordy,
over-literal, or wrong in places. Rewrite it so it reads like a novel written in Arabic.

Rules, in order of importance:
1. Keep the meaning of the English exactly. Add nothing, drop nothing, invent nothing.
2. Use the common Arabic word, not the dictionary word, whenever both are correct.
3. Convey the image, not the words. If a literal rendering sounds like a translation, say what it means.
4. Keep every name, title and term exactly as the draft spells it, unless it is garbled.
5. Natural Arabic sentence order. Arabic does not need "كان" in front of every past-tense action.
6. Dialogue stays dialogue and keeps its tone. No added honorifics, no softening.
7. Return EXACTLY {n} paragraphs, in the same order, separated by a blank line. No numbering, no notes."""

REVIEW = """You are reviewing an Arabic novel chapter that was just improved against its English source.

Check, in this order:
1. Anything in the English that is missing from the Arabic, or anything in the Arabic that is not in
   the English (an invented sentence, a doubled line, a translator's footnote that leaked in).
2. Mistranslations: a wrong subject, a reversed negation, a wrong number, a wrong gender.
3. Arabic that still reads as translation rather than as a novel.

Return only the corrected Arabic, same number of paragraphs, same order, separated by a blank line.
If nothing needs changing, return the input unchanged."""

STYLE = ROOT / "style-rules.json"


def api_key() -> str:
    k = os.environ.get("DEEPSEEK_API_KEY")
    if k:
        return k.strip()
    for line in (Path(os.environ["USERPROFILE"]) / ".dsh" / ".credentials.yaml").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("DEEPSEEK_API_KEY"):
            return line.split(":", 1)[1].strip().strip("\"'")
    raise SystemExit("no DEEPSEEK_API_KEY")


class Meter:
    def __init__(self):
        self.pin = 0
        self.pout = 0
        self.calls = 0

    def add(self, u: dict) -> None:
        self.pin += u.get("prompt_tokens", 0)
        self.pout += u.get("completion_tokens", 0)
        self.calls += 1

    @property
    def usd(self) -> float:
        return self.pin / 1e6 * PRICE_IN + self.pout / 1e6 * PRICE_OUT

    def line(self) -> str:
        return f"{self.calls} calls, {self.pin} in / {self.pout} out tokens, {self.usd:.4f} USD"


def chat(key: str, system: str, user: str, meter: Meter, temperature: float = 0.6) -> str:
    r = None
    for attempt in range(4):
        r = requests.post(API, timeout=300, headers={"Authorization": f"Bearer {key}"},
                          json={"model": MODEL, "temperature": temperature,
                                "messages": [{"role": "system", "content": system},
                                             {"role": "user", "content": user}]})
        if r.status_code == 200:
            j = r.json()
            meter.add(j.get("usage", {}))
            return j["choices"][0]["message"]["content"]
        time.sleep(3 + attempt * 5)
    raise SystemExit(f"deepseek failed {r.status_code}: {r.text[:300]}")


def split_paras(t: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", t) if p.strip()]


def apply_style(text: str) -> str:
    if not STYLE.exists():
        return text
    for rule in json.loads(STYLE.read_text(encoding="utf-8")).get("rules", []):
        try:
            text = re.sub(rule["re"], rule["to"], text)
        except re.error:
            pass
    return text


def fetch_chapter(site: str, novel_url: str, n: int) -> tuple[str, list[str]]:
    cfg = runner.SITES[site]
    page = runner.get(novel_url)
    if not page:
        raise SystemExit(f"cannot read {novel_url}")
    links = runner.chapter_links(cfg, novel_url, runner.chapters_for(cfg, novel_url, page))
    if n > len(links):
        raise SystemExit(f"{site} has only {len(links)} chapters")
    title, body = runner.chapter_body(cfg, runner.get(links[n - 1]) or "")
    return title, split_paras(body)


def improve(key: str, en: list[str], ar: list[str], meter: Meter) -> list[str]:
    """Paragraph chunks, each with the matching slice of English. Never the whole chapter at once."""
    n = len(ar)
    out: list[str] = []
    CHUNK = 6
    for start in range(0, n, CHUNK):
        ar_chunk = ar[start:start + CHUNK]
        lo = int(start / n * len(en))
        hi = max(lo + 1, int(min(n, start + CHUNK) / n * len(en)))
        en_chunk = en[lo:hi] or en[:1]
        user = ("ENGLISH:\n" + "\n\n".join(en_chunk) +
                "\n\n---\n\nARABIC DRAFT:\n" + "\n\n".join(ar_chunk))
        got = split_paras(chat(key, RULES.replace("{n}", str(len(ar_chunk))), user, meter))
        if len(got) != len(ar_chunk):
            fix = (f"That was {len(got)} paragraphs, need exactly {len(ar_chunk)}. "
                   "Merge or split so the count matches, changing no content.\n\n" +
                   "\n\n".join(got))
            got = split_paras(chat(key, RULES.replace("{n}", str(len(ar_chunk))), fix, meter))
        while len(got) < len(ar_chunk):
            got.append(ar_chunk[len(got)])
        out.extend(got[:len(ar_chunk)])
    return out


def dedupe(paras: list[str]) -> list[str]:
    """Drop paragraphs that restate an earlier one.

    A chunk whose Arabic has more paragraphs than its English slice makes the model pad, and the padding
    is a re-wording of a sentence it already produced. Caught once on Shadow Slave ch1: paragraphs 12-14
    were paragraphs 8-10 said again.
    """
    seen: list[str] = []
    out: list[str] = []
    for p in paras:
        norm = re.sub(r"[^\w\s]", "", p)[:90].strip()
        if not norm:
            continue
        if any(norm in s or s in norm for s in seen if len(s) > 40):
            continue
        seen.append(norm)
        out.append(p)
    return out


def metrics(ar: list[str], after: list[str]) -> dict:
    def kan(p):
        txt = " ".join(p)
        words = len(txt.split())
        k = len(re.findall(r"كان|كانت", txt))
        return round(k / max(1, words) * 1000, 1)

    changed = sum(1 for a, b in zip(ar, after) if a.strip() != b.strip())
    return {"paragraphs": len(ar), "paragraphs_after": len(after),
            "kan_per_1000_before": kan(ar), "kan_per_1000_after": kan(after),
            "changed_paragraphs": changed,
            "changed_pct": round(changed / max(1, len(ar)) * 100)}


def review_sheet(out_dir: Path, title: str, en: list[str], ar: list[str], after: list[str], m: dict) -> None:
    lines = [f"# Review — {title}", "",
             f"- paragraphs: {m['paragraphs']} -> {m['paragraphs_after']}",
             f"- changed: {m['changed_paragraphs']} ({m['changed_pct']}%)",
             f"- «كان/كانت» per 1000 words: {m['kan_per_1000_before']} -> {m['kan_per_1000_after']}",
             "", "Read down the three blocks. Anything wrong goes in CORRECTION.", ""]
    n = max(len(ar), len(after))
    for i in range(n):
        lines.append(f"## {i + 1}")
        lines.append("**EN**  " + (en[i] if i < len(en) else ""))
        lines.append("")
        lines.append("**AR before**  " + (ar[i] if i < len(ar) else ""))
        lines.append("")
        lines.append("**AR after**  " + (after[i] if i < len(after) else ""))
        lines.append("")
        lines.append("**CORRECTION**  ")
        lines.append("")
    (out_dir / "review.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", required=True)
    ap.add_argument("--en-site", required=True, choices=list(runner.SITES))
    ap.add_argument("--en-url", required=True)
    ap.add_argument("--ar-site", required=True, choices=list(runner.SITES))
    ap.add_argument("--ar-url", required=True)
    ap.add_argument("--chapters", default="1", help="1 or 1-5")
    ap.add_argument("--out", default="out/pipelineB")
    ap.add_argument("--dry", action="store_true", help="fetch and align only, spend nothing")
    a = ap.parse_args()

    if "-" in a.chapters:
        lo, hi = (int(x) for x in a.chapters.split("-"))
    else:
        lo = hi = int(a.chapters)

    meter = Meter()
    key = "" if a.dry else api_key()
    for ch in range(lo, hi + 1):
        d = Path(a.out) / a.pair / f"ch{ch:04d}"
        d.mkdir(parents=True, exist_ok=True)
        en_title, en = fetch_chapter(a.en_site, a.en_url, ch)
        ar_title, ar = fetch_chapter(a.ar_site, a.ar_url, ch)
        print(f"ch{ch:04d}: EN {len(en)} paras, AR {len(ar)} paras")
        (d / "en.txt").write_text("\n\n".join(en), encoding="utf-8", newline="\n")
        (d / "ar-before.txt").write_text("\n\n".join(ar), encoding="utf-8", newline="\n")
        if a.dry:
            continue
        t0 = time.time()
        draft = [apply_style(p) for p in improve(key, en, ar, meter)]
        reviewed = split_paras(chat(key, REVIEW,
                                    "ENGLISH:\n" + "\n\n".join(en) + "\n\n---\n\nARABIC:\n" + "\n\n".join(draft),
                                    meter))
        if len(reviewed) != len(draft):
            reviewed = draft
        reviewed = dedupe(reviewed) or reviewed
        m = metrics(ar, reviewed)
        (d / "ar-after.txt").write_text("\n\n".join(reviewed), encoding="utf-8", newline="\n")
        (d / "metrics.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
        review_sheet(d, en_title or a.pair, en, ar, reviewed, m)
        print(f"   {m['changed_paragraphs']}/{m['paragraphs']} paragraphs changed "
              f"({m['changed_pct']}%), kan {m['kan_per_1000_before']} -> {m['kan_per_1000_after']}, "
              f"{time.time() - t0:.0f}s")

    print("\nmeter: " + meter.line())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
