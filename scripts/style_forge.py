#!/usr/bin/env python3
"""YuE2 style-prompt designer built on the shared style lexicon.

数据来自 data/style_lexicon.json（由 scripts/build_lexicon.py 生成）：
musegen/music-prompt-skill 的方法与提示库 + bitwize 的 388 个流派关键词。

用法：
    python style_forge.py fields [--json]
    python style_forge.py presets [--json]
    python style_forge.py random [--seed N] [--genre G] [--mood M] [--language L] [--instrumental] [--json]
    python style_forge.py build --genre "city pop" [--mood nostalgic] [--vocal "soft female voice"]
                                [--tag "FM synthesis" ...] [--bpm 112] [--production "wide stereo reverb"]
                                [--language Japanese] [--instrumental] [--json]
    python style_forge.py check "风格文本" [--json]
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
LEXICON = HERE.parent / "data" / "style_lexicon.json"

from zh_label import ensure_labels  # noqa: E402  （需在 sys.path 处理之后导入）

GENRE_RE = re.compile(r"(\d{2,3})\s*BPM", re.I)


def load_lexicon(path: Path = LEXICON):
    if not path.exists():
        raise SystemExit(f"词库不存在：{path}，请先运行 scripts/build_lexicon.py")
    lex = json.loads(path.read_text(encoding="utf-8"))
    return ensure_labels(lex)  # 旧版词库缺 label 时，运行时按 zh_terms.json 补中文


def _bpm_of(style: str):
    m = GENRE_RE.search(style or "")
    return int(m.group(1)) if m else None


def bpm_range(genre: dict):
    if genre.get("bpm"):
        return genre["bpm"]
    from_style = _bpm_of(genre.get("style", ""))
    return [from_style, from_style] if from_style else [90, 110]


def find_genre(lex: dict, name: str):
    if not name:
        return None
    key = name.strip().lower()
    for g in lex["genres"]:
        if g["name"].lower() == key or g.get("slug", "").lower() == key:
            return g
    for g in lex["genres"]:
        if key in g["name"].lower():
            return g
    return None


def genre_tags(genre: dict, limit=5):
    """流派自带标签：bitwize 用 tags，musegen 用整条示例风格里的片段。"""
    if not genre:
        return []
    if genre.get("tags"):
        return genre["tags"][:limit]
    if genre.get("style"):
        parts = [p.strip() for p in genre["style"].split(",")]
        skip = {"no vocals", "mixed vocals", "female vocal", "male vocal", "no melody"}
        return [p for p in parts if not GENRE_RE.search(p) and p.lower() not in skip][:limit]
    return []


CJK_RE = re.compile(r"[一-鿿]")


def resolve_en(lex, text):
    """中文标签 → 英文取值。查不到就返回 None，避免中文混进 style。"""
    if isinstance(text, dict):  # 词库条目本身（{value,label,en}）直接取英文值
        return text.get("value") or text.get("en") or text.get("name")
    if not text:
        return None
    if not CJK_RE.search(text):
        return text
    key = text.strip()
    for g in lex.get("genres", []):
        if key == g.get("label"):
            return g["name"]
    for m in lex.get("moods", []):
        if key == m.get("label"):
            return m["name"].lower()
    for group in (lex.get("vocals") or {}).values():
        for v in group:
            if isinstance(v, dict) and key == v.get("label"):
                return v["value"]
    for v in (lex.get("production") or []) + (lex.get("negatives") or []):
        if isinstance(v, dict) and key == v.get("label"):
            return v["value"]
    for p in lex.get("presets", []):
        if key == p.get("label"):
            return p["style"]
    return None


def build_style(lex, language="Mandarin", genre=None, mood=None, vocal=None, tags=None,
                bpm=None, production=None, instrumental=False, seed=None, rng=None,
                groove=None, structure=None, length=None, key=None, era=None,
                negatives=None, placement=None, delivery=None):
    """按 YuE2 顺序拼装：
    语言 → 流派 → 情绪 → 人声(+演唱方式/混音位置) → 乐器标签 → 调性/年代
    → 律动 → 走向/篇幅 → 制作/空间 → 排除项 → BPM。
    """
    rng = rng or random.Random(seed)
    genre = resolve_en(lex, genre)
    mood = resolve_en(lex, mood)
    vocal = resolve_en(lex, vocal)
    production = resolve_en(lex, production)
    groove = resolve_en(lex, groove)
    structure = resolve_en(lex, structure)
    length = resolve_en(lex, length)
    key = resolve_en(lex, key)
    era = resolve_en(lex, era)
    placement = resolve_en(lex, placement)
    delivery = resolve_en(lex, delivery)

    def en_list(values):
        return [v for v in (resolve_en(lex, x) for x in (values or [])) if v]

    tags = en_list(tags)
    negatives = en_list(negatives)
    # 误选预设时会拿到整条风格串，这里拆成片段
    if any("," in t for t in tags):
        tags = [p.strip() for t in tags for p in t.split(",") if p.strip()]
    instrumental = instrumental or language == "Instrumental"
    g = find_genre(lex, genre)
    if g is None and genre is None:
        g = rng.choice(lex["genres"])

    lo, hi = bpm_range(g) if g else (90, 110)
    tempo = f"{int(bpm) if bpm else rng.randint(lo, hi)} BPM"

    chosen = list(tags or [])
    if not chosen and g:
        pool = genre_tags(g, limit=8)
        chosen = rng.sample(pool, min(3, len(pool))) if pool else []
    genre_name = (g["name"] if g else genre or "").strip().lower()
    chosen = [t for t in chosen if t.strip().lower() != genre_name]  # 避免流派名重复出现

    drops = list(negatives or [])
    if instrumental:
        drops = drops + [n for n in lex["rules"]["instrumental_negatives"] if n not in drops]

    # （优先级, 文本）：数字越小越先保留。超出 180 字符时按优先级丢弃，而不是硬截断。
    # 追加顺序即最终输出顺序：语言 → 流派 → 情绪 → 人声 → 乐器 → 调性 → 年代
    # → 律动 → 走向 → 篇幅 → 制作 → 排除 → BPM
    items = [(1, "Instrumental" if instrumental else language)]
    if g:
        items.append((2, g["name"].lower() if g.get("slug") else g["name"]))
    elif genre:
        items.append((2, genre))
    if mood:
        items.append((3, mood))
    if not instrumental and vocal:
        items.append((3, vocal))
        if delivery:
            items.append((9, delivery))
        if placement:
            items.append((10, placement))
    if chosen:
        items.append((5, ", ".join(chosen)))
    if key:
        items.append((7, key))
    if era:
        items.append((8, era))
    if groove:
        items.append((6, groove))
    if structure:
        items.append((12, structure))
    if length:
        items.append((13, length))
    if production:
        items.append((4, production))
    if drops:
        items.append((0 if instrumental else 11, ", ".join(drops)))
    items.append((0, tempo))

    limit = lex["rules"]["max_chars"]
    kept, total = [], 0
    for prio, text in sorted(items, key=lambda x: x[0]):
        add = len(text) + (2 if kept else 0)
        if total + add <= limit:
            kept.append((prio, text))
            total += add
    # 输出仍按语义顺序：语言 → 流派 → 情绪 → 人声 → 乐器 → 调性 → 律动 → 走向 → 制作 → 排除 → BPM
    order = [text for prio, text in items if any(text == t for _, t in kept)]
    return ", ".join(order)


def random_style(lex, seed=None, genre=None, mood=None, language=None, instrumental=False):
    rng = random.Random(seed)
    langs = [l["value"] for l in lex["languages"] if l["value"] != "Instrumental"]
    language = language or rng.choice(langs)
    vocal = None
    if not instrumental:
        vocal = rng.choice(lex["vocals"]["types"])
        if rng.random() < 0.5:
            vocal = f"{vocal}, {rng.choice(lex['vocals']['delivery'])}"
    if not mood:
        pick = rng.choice(lex["moods"]) if lex.get("moods") else None
        mood = pick["name"].lower() if pick else None
        extra = [pick["signature"]] if pick else []
    else:
        extra = []
    return build_style(
        lex, language=language, genre=genre, mood=mood, vocal=vocal,
        tags=extra or None,
        production=rng.choice(lex["production"]) if rng.random() < 0.6 else None,
        instrumental=instrumental, rng=rng,
    )


def check_style(lex, style: str):
    low = style.lower()
    return {
        "chars": len(style),
        "within_limit": len(style) <= lex["rules"]["max_chars"],
        "bad_words": [w for w in lex["rules"]["avoid"] if w in low],
        "has_bpm": bool(GENRE_RE.search(style)),
        "has_language": any(style.startswith(l["value"]) for l in lex["languages"]),
    }


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)

    def add_json(p):
        p.add_argument("--json", action="store_true")

    fields = commands.add_parser("fields"); add_json(fields)
    presets = commands.add_parser("presets"); add_json(presets)

    rand = commands.add_parser("random"); add_json(rand)
    rand.add_argument("--seed", type=int); rand.add_argument("--genre")
    rand.add_argument("--mood"); rand.add_argument("--language")
    rand.add_argument("--instrumental", action="store_true")

    build = commands.add_parser("build"); add_json(build)
    build.add_argument("--language", default="Mandarin")
    build.add_argument("--genre"); build.add_argument("--mood"); build.add_argument("--vocal")
    build.add_argument("--tag", action="append", default=[])
    build.add_argument("--bpm", type=int); build.add_argument("--production")
    build.add_argument("--groove"); build.add_argument("--structure")
    build.add_argument("--length"); build.add_argument("--key")
    build.add_argument("--era"); build.add_argument("--negative", action="append", default=[])
    build.add_argument("--placement"); build.add_argument("--delivery")
    build.add_argument("--usecase")
    build.add_argument("--instrumental", action="store_true")

    check = commands.add_parser("check"); add_json(check)
    check.add_argument("style")

    args = cli.parse_args(argv)
    lex = load_lexicon()

    if args.command == "fields":
        data = {
            "languages": lex["languages"],
            "genres": [{"name": g["name"], "label": g.get("label", g["name"]),
                        "bpm": bpm_range(g), "tags": genre_tags(g, 8)}
                       for g in lex["genres"]],
            "moods": [{"name": m["name"], "label": m.get("label", m["name"]),
                       "signature": m.get("signature", "")} for m in lex.get("moods", [])],
            "vocals": lex["vocals"],
            "production": lex["production"],
            "negatives": lex["negatives"],
            "groove": lex.get("groove", []),
            "structure": lex.get("structure", []),
            "length": lex.get("length", []),
            "keys": lex.get("keys", []),
            "eras": lex.get("eras", []),
            "usecases": lex.get("usecases", []),
            "rules": lex["rules"],
        }
    elif args.command == "presets":
        data = lex["presets"]
    elif args.command == "random":
        data = {"style": random_style(lex, args.seed, args.genre, args.mood, args.language, args.instrumental)}
    elif args.command == "build":
        extra = {}
        if args.usecase:  # 场景模板提供默认流派/标签，可再被显式选择覆盖
            uc = next((u for u in lex.get("usecases", [])
                       if u["name"].lower() == args.usecase.lower()), None)
            if uc:
                extra = {"tags": [p.strip() for p in uc["style"].split(",")]}
        data = {"style": build_style(lex, args.language, args.genre, args.mood, args.vocal,
                                     extra.get("tags") or args.tag, args.bpm, args.production,
                                     args.instrumental, groove=args.groove,
                                     structure=args.structure, length=args.length,
                                     key=args.key, era=args.era, negatives=args.negative,
                                     placement=args.placement, delivery=args.delivery)}
    else:
        data = check_style(lex, args.style)

    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False))
    elif isinstance(data, dict) and "style" in data:
        print(data["style"])
    elif isinstance(data, list):
        for row in data:
            print(f"{row['name']}\t{row['style']}")
    else:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
