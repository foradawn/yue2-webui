#!/usr/bin/env python3
"""中文标签：data/zh_terms.json 对照表 + 长短语优先的术语级翻译。

被 build_lexicon.py（写进 JSON）与 style_forge.py（运行时兜底）共用，
保证即使词库 JSON 是旧版、没带 label 字段，WebUI 下拉也能显示中文。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
ZH_PATH = SKILL / "data" / "zh_terms.json"


def load_zh(path: Path = ZH_PATH):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def make_translator(zh: dict):
    """长短语优先的整词替换：命中即占位，未命中的英文原样保留。"""
    phrases = {k.lower(): v for k, v in zh.get("phrases", {}).items()}

    def translate(name: str) -> str:
        text = name
        slots = []

        def rep(m):
            slots.append(phrases[m.group(0).lower()])
            return f"\x00{len(slots) - 1}\x00"

        for ph in sorted(phrases, key=len, reverse=True):
            pat = re.compile(rf"(?<![\u4e00-\u9fff]){re.escape(ph)}(?![\u4e00-\u9fff])", re.I)
            text = pat.sub(rep, text)
        for i, val in enumerate(slots):
            text = text.replace(f"\x00{i}\x00", val)
        text = re.sub(r"\s+and\s+", " ", text)
        text = text.replace("-", "").replace("/", "")
        text = re.sub(r"(?<=[\u4e00-\u9fff]) (?=[\u4e00-\u9fff])", "", text)
        text = re.sub(r"\s{2,}", " ", text).strip(" ,")
        return text or name

    return translate


def labelled(values, mapping, translate=None):
    """英文取值 -> {value, label}：优先手工对照，其次词表翻译。"""
    out = []
    for v in values:
        if isinstance(v, dict):
            out.append(v)
            continue
        zh = mapping.get(v) or (translate(v) if translate else v)
        out.append({"value": v, "label": zh, "en": v})
    return out


def ensure_labels(lex: dict, zh: dict | None = None) -> dict:
    """就地把词库补上中文 label（默认跳过已有 label 的条目）。"""
    zh = zh if zh is not None else load_zh()
    if not zh:
        return lex
    tr = make_translator(zh)
    presets_map = zh.get("presets", {})
    moods_map = zh.get("moods", {})

    def stale(item):
        """没有 label，或 label 与英文同名（未翻译）时都要重算。"""
        name = item.get("name", "")
        return not item.get("label") or item["label"] == name

    for g in lex.get("genres", []):
        if stale(g):
            g["label"] = presets_map.get(g.get("name", "")) or tr(g.get("name", ""))
    for m in lex.get("moods", []):
        if isinstance(m, dict) and stale(m):
            m["label"] = moods_map.get(m.get("name", ""), m.get("name", ""))
    for p in lex.get("presets", []):
        if isinstance(p, dict) and stale(p):
            p["label"] = presets_map.get(p.get("name", "")) or tr(p.get("name", ""))

    voc = lex.get("vocals")
    if isinstance(voc, dict):
        for key, map_key in (("types", "vocals_types"), ("placement", "vocals_placement"),
                             ("delivery", "vocals_delivery")):
            if key in voc:
                voc[key] = labelled(voc[key], zh.get(map_key, {}), tr)
    # 旧版词库没有这些分组时，直接从常量补齐（避免必须重建 JSON）
    if any(k not in lex for k in ("groove", "structure", "length", "keys", "eras", "usecases")):
        try:
            from build_lexicon import (ERAS, GROOVE, KEYS, LENGTH, STRUCTURE,
                                       parse_usecases)
            lex.setdefault("groove", GROOVE)
            lex.setdefault("structure", STRUCTURE)
            lex.setdefault("length", LENGTH)
            lex.setdefault("keys", KEYS)
            lex.setdefault("eras", ERAS)
            lex.setdefault("usecases", parse_usecases(SKILL.parent / "music-prompt"))
        except Exception:  # 常量不可用时静默降级，仅缺这几个分组
            pass

    for key in ("production", "negatives", "groove", "structure", "length", "keys", "eras"):
        if key in lex:
            lex[key] = labelled(lex[key], zh.get(key, {}), tr)
    for u in lex.get("usecases", []):
        if isinstance(u, dict) and stale(u):
            u["label"] = zh.get("usecases", {}).get(u.get("name", "")) or tr(u.get("name", ""))
    return lex


if __name__ == "__main__":
    tr = make_translator(load_zh())
    for name in ["City Pop", "Drum and Bass", "Guofeng electronic", "Melodic death", "Lo-fi soul"]:
        print(f"{name:22s} -> {tr(name)}")
