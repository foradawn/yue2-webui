"""YuE2 Web: 歌词 + 风格 -> 符号乐谱 -> 完整歌曲（GPU 推理服务）。

启动：
    /workspace/YuE/.venv/bin/python app.py --host 0.0.0.0 --port 7860
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import torch
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"
OUTPUTS.mkdir(parents=True, exist_ok=True)
UPLOADS = ROOT / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)
STATIC = ROOT / "static"
REPO = Path("/workspace/YuE")
STYLE_SKILL = Path("/workspace/.codebuddy/skills/music-style-designer")

SHEETSAGE_PY = REPO / ".venv-sheetsage2/bin/python"
SHEETSAGE_INFER = REPO / "models/SheetSage2/infer.py"
SHEETSAGE_MODEL = REPO / "models/SheetSage2"
TRANSCRIBE_TIMEOUT = 1800

MODEL_ID = "m-a-p/YuE2-3B"
VAES = {"standard": "m-a-p/YuE2-Vae", "legacy": "m-a-p/YuE2-Vae-legacy"}

app = FastAPI(title="YuE2 Web")

_state = {
    "pipe": None,
    "pipe_key": None,
    "pipe_lock": threading.Lock(),
    "busy": False,
    "jobs": {},
}


def build_generation_config(payload) -> object:
    """把页面上的采样/合成覆盖项组装成 GenerationConfig。"""
    from dataclasses import asdict

    from yue2.protocol import GenerationConfig, Sampling

    abc_default = Sampling(0.7, 0.9, 30, 1.005, 100, 32, 4096)
    semantic_default = Sampling()

    def merged(default, overrides):
        data = asdict(default)
        data.update({k: v for k, v in overrides.items() if v is not None})
        return Sampling(**data)

    return GenerationConfig(
        abc=merged(abc_default, {
            "temperature": payload.abc_temperature,
            "top_p": payload.abc_top_p,
            "max_tokens": payload.abc_max_tokens,
        }),
        semantic=merged(semantic_default, {
            "temperature": payload.semantic_temperature,
            "top_p": payload.semantic_top_p,
            "max_tokens": payload.semantic_max_tokens,
        }),
        ode_steps=payload.ode_steps or 32,
    )


def get_pipe(payload) -> object:
    """按流水线级参数缓存实例；任一参数变化就释放旧实例再重建。"""
    from yue2 import YuE2Pipeline

    key = (
        VAES.get(payload.vae, payload.vae), float(payload.budget), payload.backend,
        payload.quantization, bool(payload.offload_ar), payload.revision, payload.vae_revision,
        payload.abc_temperature, payload.abc_top_p, payload.abc_max_tokens,
        payload.semantic_temperature, payload.semantic_top_p, payload.semantic_max_tokens,
        payload.ode_steps,
    )
    if _state["pipe"] is not None and _state["pipe_key"] == key:
        return _state["pipe"]
    if _state["pipe"] is not None:
        _state["pipe"].close()
        _state["pipe"] = None
        torch.cuda.empty_cache()
    _state["pipe"] = YuE2Pipeline.from_pretrained(
        MODEL_ID, vae=VAES.get(payload.vae, payload.vae), revision=payload.revision,
        vae_revision=payload.vae_revision, device="cuda", memory_budget_gib=float(payload.budget),
        backend=payload.backend, quantization=payload.quantization, offload_ar=payload.offload_ar,
        generation_config=build_generation_config(payload), progress=False,
    )
    _state["pipe_key"] = key
    return _state["pipe"]


def gpu_info():
    if not torch.cuda.is_available():
        return {"available": False}
    i = torch.cuda.current_device()
    p = torch.cuda.get_device_properties(i)
    return {
        "available": True,
        "name": p.name,
        "memory_gib": round(p.total_memory / 2**30, 2),
        "bf16": torch.cuda.is_bf16_supported(),
        "torch": torch.__version__,
    }


def vocal_to_instrumental(abc: str) -> str:
    """把 Vocal 声部旋律移到 Ins 声部（伴奏化/乐器版）。

    交换两个声部的乐句内容，保留音高、起奏与时长；声部小节数不同则拒绝转换。
    """
    lines = abc.splitlines()
    blocks: dict[str, list[tuple[int, str]]] = {"Vocal": [], "Ins": []}
    voice = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("V:"):
            voice = "Vocal" if "Vocal" in stripped else "Ins" if "Ins" in stripped else None
            continue
        if voice and stripped and not stripped.startswith(("X:", "T:", "M:", "L:", "Q:", "K:", "%")):
            blocks[voice].append((index, line))
    if not blocks["Vocal"] or not blocks["Ins"]:
        raise ValueError("乐谱缺少 Vocal 或 Ins 声部，无法自动转换")
    if len(blocks["Vocal"]) != len(blocks["Ins"]):
        raise ValueError("两个声部小节数不同，请手动编辑乐谱后再提交")
    result = list(lines)
    for (vocal_index, vocal_line), (ins_index, ins_line) in zip(blocks["Vocal"], blocks["Ins"]):
        result[vocal_index] = ins_line
        result[ins_index] = vocal_line
    return "\n".join(result)


def sheetsage_ready():
    return SHEETSAGE_PY.is_file() and SHEETSAGE_INFER.is_file()


def ensure_sheetsage_code():
    """transformers 的远程代码目录若缺文件会报错；用仓库内完整 .py 预填。"""
    cache = Path.home() / ".cache/huggingface/modules/transformers_modules/SheetSage2"
    if not SHEETSAGE_MODEL.is_dir():
        return
    cache.mkdir(parents=True, exist_ok=True)
    for path in SHEETSAGE_MODEL.glob("*.py"):
        target = cache / path.name
        if not target.exists() or target.read_bytes() != path.read_bytes():
            shutil.copyfile(path, target)


def _transcribe(job_id: str, source: Path, workdir: Path) -> None:
    job = _state["jobs"][job_id]
    try:
        wav = workdir / "source.wav"
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg and source.suffix.lower() != ".wav":
            job["stage"] = "converting"
            subprocess.run(
                [ffmpeg, "-y", "-i", str(source), "-ar", "24000", "-ac", "1", str(wav)],
                check=True, capture_output=True, timeout=300,
            )
        else:
            wav = source
        job["stage"] = "transcribing"
        ensure_sheetsage_code()
        out_dir = workdir / "score"
        proc = subprocess.run(
            [str(SHEETSAGE_PY), str(SHEETSAGE_INFER), str(wav),
             "--output", str(out_dir), "--melody-only"],
            capture_output=True, text=True, timeout=TRANSCRIBE_TIMEOUT,
            cwd=str(REPO), env={**os.environ, "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", "0")},
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "").strip()[-800:] or "转录失败")
        abc_path = out_dir / "score.abc"
        if not abc_path.is_file():
            raise RuntimeError("转录未产出 score.abc")
        abc = abc_path.read_text(encoding="utf-8")
        if not abc.strip():
            raise RuntimeError("转录结果为空，请换一段更清晰的参考音频")
        job.update(status="complete", stage="done", abc=abc,
                   score_url=f"/uploads/{job_id}/score/score.abc")
    except Exception as exc:
        job.update(status="failed", stage="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        _state["busy"] = False


class GenerateRequest(BaseModel):
    id: str = Field("song", description="输出目录名（字母数字）")
    style: str
    lyrics: str
    cot: str = "full"
    seed: int = 42
    abc: str | None = None
    cfg_scale: float | None = Field(None, ge=0, le=20)
    vae: str = "standard"
    budget: float = Field(24, ge=4, le=48)
    instrumental: bool = False
    stage: str = Field("audio", description="audio（出音频）或 plan（只出乐谱）")

    # 采样与合成
    abc_temperature: float | None = None
    abc_top_p: float | None = None
    abc_max_tokens: int | None = None
    semantic_temperature: float | None = None
    semantic_top_p: float | None = None
    semantic_max_tokens: int | None = None
    ode_steps: int | None = Field(None, ge=1, le=200)

    # 流水线级
    backend: str = "torch"
    quantization: str = "none"
    offload_ar: bool = False
    revision: str | None = None
    vae_revision: str | None = None


def _run(job_id: str, payload: GenerateRequest) -> None:
    job = _state["jobs"][job_id]
    try:
        pipe = get_pipe(payload)
        request = {
            "id": payload.id or "song",
            "style": payload.style,
            "lyrics": payload.lyrics,
            "cot": payload.cot,
            "seed": payload.seed,
        }
        if payload.cfg_scale is not None:
            request["cfg_scale"] = payload.cfg_scale
        abc = payload.abc.strip() if payload.abc and payload.abc.strip() else None
        if abc and payload.instrumental:
            abc = vocal_to_instrumental(abc)
        if abc:
            request["abc"] = abc

        def on_token(phase, token):
            job["tokens"] += 1
            job["phase"] = phase

        def cancelled():
            return bool(job.get("cancel"))

        started = time.perf_counter()
        out_dir = OUTPUTS / job_id
        if payload.stage == "plan":
            job["stage"] = "planning"
            plan = pipe.plan(**request, cancelled=cancelled, on_token=on_token)
            plan.save(out_dir)
            job.update(status="complete", stage="done", plan_only=True, abc=plan.abc,
                       seconds=round(time.perf_counter() - started, 1),
                       score=f"/outputs/{job_id}/score.abc" if plan.abc else None,
                       truncated={"abc": plan.truncated})
            return
        job["stage"] = "generating"
        song = pipe(**request, cancelled=cancelled, on_token=on_token)
        job["stage"] = "saving"
        receipt = song.save_artifacts(out_dir)
        song.save(out_dir / "audio.wav")
        job.update(
            status="complete",
            stage="done",
            seconds=round(time.perf_counter() - started, 1),
            audio=f"/outputs/{job_id}/audio.wav",
            flac=f"/outputs/{job_id}/audio.flac",
            score=f"/outputs/{job_id}/score.abc" if song.abc else None,
            audio_seconds=receipt["audio_seconds"],
            truncated=receipt["truncated"],
            result=receipt,
        )
    except Exception as exc:  # 保留失败现场，便于排查
        status = "cancelled" if job.get("cancel") else "failed"
        job.update(status=status, stage="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        _state["busy"] = False


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/device")
def device():
    return {
        "gpu": gpu_info(),
        "busy": _state["busy"],
        "model": MODEL_ID,
        "vaes": VAES,
        "loaded_vae": _state["pipe_key"][0] if _state["pipe_key"] else None,
        "sheetsage2": sheetsage_ready(),
    }


@app.get("/api/example/{name}")
def example(name: str):
    mapping = {
        "melody.abc": REPO / "examples/melody.abc",
        "score.abc": REPO / "examples/score.abc",
    }
    path = mapping.get(name)
    if path is None or not path.is_file():
        raise HTTPException(404, "示例不存在")
    return JSONResponse({"name": name, "abc": path.read_text(encoding="utf-8")})


# ---------- 风格助手（music-style-designer skill） ----------
def _style_forge():
    """懒加载 style_forge 模块（纯标准库，不占显存）。"""
    if _state.get("forge") is None:
        import importlib.util

        path = STYLE_SKILL / "scripts" / "style_forge.py"
        spec = importlib.util.spec_from_file_location("style_forge", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _state["forge"] = mod
    return _state["forge"]


@app.get("/api/style/fields")
def style_fields():
    """下拉选项：语言 / 流派 / 情绪 / 人声 / 制作 / 负向约束 / 规则。"""
    forge = _style_forge()
    lex = forge.load_lexicon(STYLE_SKILL / "data" / "style_lexicon.json")
    return {
        "languages": lex["languages"],
        "genres": [{"name": g["name"], "label": g.get("label") or g["name"],
                    "bpm": forge.bpm_range(g), "tags": forge.genre_tags(g, 8)}
                   for g in lex["genres"]],
        "moods": [{"name": m["name"], "label": m.get("label") or m["name"],
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
        "presets": lex["presets"][:80],
        "rules": lex["rules"],
    }


class StyleBuild(BaseModel):
    language: str = "Mandarin"
    genre: str | None = None
    mood: str | None = None
    vocal: str | None = None
    tags: list[str] = Field(default_factory=list)
    bpm: int | None = None
    production: str | None = None
    instrumental: bool = False
    groove: str | None = None
    structure: str | None = None
    length: str | None = None
    key: str | None = None
    era: str | None = None
    negatives: list[str] = Field(default_factory=list)
    placement: str | None = None
    delivery: str | None = None


@app.post("/api/style/build")
def style_build(req: StyleBuild):
    forge = _style_forge()
    lex = forge.load_lexicon(STYLE_SKILL / "data" / "style_lexicon.json")
    style = forge.build_style(
        lex, language=req.language, genre=req.genre, mood=req.mood or None,
        vocal=req.vocal or None, tags=req.tags or None, bpm=req.bpm,
        production=req.production or None, instrumental=req.instrumental,
        groove=req.groove or None, structure=req.structure or None,
        length=req.length or None, key=req.key or None, era=req.era or None,
        negatives=req.negatives or None, placement=req.placement or None,
        delivery=req.delivery or None,
    )
    return {"style": style, "check": forge.check_style(lex, style)}


@app.get("/api/style/random")
def style_random(seed: int | None = None, genre: str | None = None,
                 mood: str | None = None, language: str | None = None,
                 instrumental: bool = False):
    forge = _style_forge()
    lex = forge.load_lexicon(STYLE_SKILL / "data" / "style_lexicon.json")
    style = forge.random_style(lex, seed=seed, genre=genre, mood=mood,
                               language=language, instrumental=instrumental)
    return {"style": style, "check": forge.check_style(lex, style)}


@app.post("/api/style/check")
def style_check(style: str = Query("", description="待检查的风格文本")):
    forge = _style_forge()
    lex = forge.load_lexicon(STYLE_SKILL / "data" / "style_lexicon.json")
    return forge.check_style(lex, style)


@app.get("/api/jobs")
def jobs():
    rows = [j for j in _state["jobs"].values() if j.get("kind") != "transcribe"]
    rows = sorted(rows, key=lambda j: j["created"], reverse=True)[:20]
    return [
        {
            k: r[k]
            for k in ("id", "status", "audio_seconds", "seconds", "style", "lyrics_head", "audio", "flac", "score", "created")
            if k in r
        }
        for r in rows
    ]


@app.post("/api/generate")
def generate(req: GenerateRequest):
    if req.cot not in {"full", "melody", "off"}:
        raise HTTPException(400, "cot 必须是 full / melody / off")
    if req.abc and req.abc.strip() and req.cot == "off":
        raise HTTPException(400, "提供乐谱时需要 full 或 melody 模式")
    if req.stage not in {"audio", "plan"}:
        raise HTTPException(400, "stage 必须是 audio 或 plan")
    if req.stage == "plan" and req.cot == "off":
        raise HTTPException(400, "cot=off 没有符号乐谱，无法只生成乐谱")
    if req.backend not in {"torch", "torch-eager", "vllm"}:
        raise HTTPException(400, "backend 必须是 torch / torch-eager / vllm")
    if req.quantization not in {"none", "fp8"}:
        raise HTTPException(400, "quantization 必须是 none / fp8")
    if req.quantization == "fp8":
        if not torch.cuda.is_available():
            raise HTTPException(400, "FP8 需要 CUDA")
        props = torch.cuda.get_device_properties(0)
        if (props.major, props.minor) < (8, 9):
            raise HTTPException(400, f"FP8 需要计算能力 8.9+，当前 GPU 为 {props.major}.{props.minor}")
    if _state["busy"]:
        raise HTTPException(409, "已有生成任务在进行中（单卡一次一首）")

    _state["busy"] = True
    job_id = uuid.uuid4().hex[:12]
    _state["jobs"][job_id] = {
        "id": job_id,
        "status": "running",
        "stage": "queue",
        "phase": None,
        "tokens": 0,
        "created": time.time(),
        "style": req.style,
        "lyrics_head": req.lyrics.splitlines()[0][:40],
        "request": json.dumps(req.model_dump(), ensure_ascii=False),
    }
    threading.Thread(target=_run, args=(job_id, req), daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...)):
    """上传参考音频 -> SheetSage2 转录旋律谱（melody-only），供翻唱使用。"""
    if not sheetsage_ready():
        raise HTTPException(503, "SheetSage2 环境未就绪（.venv-sheetsage2 或 models/SheetSage2 缺失）")
    if _state["busy"]:
        raise HTTPException(409, "GPU 正忙：转录与生成需串行使用同一张卡")

    job_id = uuid.uuid4().hex[:12]
    workdir = UPLOADS / job_id
    workdir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "audio").suffix.lower() or ".wav"
    source = workdir / f"source{suffix}"
    with source.open("wb") as handle:
        shutil.copyfileobj(file.file, handle)

    _state["busy"] = True
    _state["jobs"][job_id] = {
        "id": job_id, "status": "running", "stage": "queue", "kind": "transcribe",
        "created": time.time(), "filename": file.filename,
    }
    threading.Thread(target=_transcribe, args=(job_id, source, workdir), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/transcribe/{job_id}")
def transcribe_status(job_id: str):
    job = _state["jobs"].get(job_id)
    if job is None or job.get("kind") != "transcribe":
        raise HTTPException(404, "转录任务不存在")
    return {k: job[k] for k in job if k != "request"}


@app.get("/uploads/{job_id}/score/score.abc")
def upload_score(job_id: str):
    path = UPLOADS / job_id / "score" / "score.abc"
    if not path.is_file():
        raise HTTPException(404, "乐谱不存在")
    return FileResponse(path)


@app.post("/api/cancel/{job_id}")
def cancel(job_id: str):
    job = _state["jobs"].get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if job.get("status") != "running":
        return {"cancelled": False, "status": job.get("status")}
    job["cancel"] = True
    return {"cancelled": True}


@app.get("/api/job/{job_id}")
def job_status(job_id: str):
    job = _state["jobs"].get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    return job


@app.get("/outputs/{job_id}/{name}")
def artifact(job_id: str, name: str):
    path = OUTPUTS / job_id / name
    if not path.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(path)


app.mount("/", StaticFiles(directory=str(STATIC)), name="static")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()
    import uvicorn

    print("[YuE2 Web] GPU:", gpu_info(), flush=True)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
