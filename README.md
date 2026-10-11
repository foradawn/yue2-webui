# YuE2 Web UI

基于 [YuE2](https://github.com/multimodal-art-projection/YuE) 的浏览器端歌曲生成界面：一个 FastAPI 后端 + 单页前端，支持**写词 → 生成 → 试听 → 转谱翻唱 → 编辑乐谱再渲染**的完整链路。

## 功能

- **生成歌曲**：填风格（style）与歌词（支持 `[verse]` / `[chorus]` 等结构标签），可选 CoT 模式（full / melody / off）、seed、CFG、显存预算。
- **风格助手**：页面内置风格词库与生成器（`/api/style/*`），可按 流派 / 情绪 / 人声 / BPM / 制作 五要素拼出合规的风格串，并做校验。
- **只生成乐谱（plan）**：秒级出 ABC 乐谱，反复改结构不烧 GPU 时间，满意后再渲染音频。
- **参考音频转谱（cover）**：上传音频，调用 SheetSage2 转成 ABC 旋律谱，配新歌词 / 新风格做翻唱。
- **任务队列与进度**：异步 job，实时 token 进度，可取消；产物（音频 / 乐谱 / 语义 token）落在 `outputs/<job_id>/`。

## 目录

```
app.py                 后端：流水线缓存、生成任务、转谱、风格助手接口
static/index.html      单页前端
data/style_lexicon.json 风格助手词库
start_webui.sh         一键启动（幂等，已运行则直接返回）
restart_webui.sh       停止旧进程并重新拉起
outputs/, uploads/     运行产物（已 gitignore）
```

## 环境依赖

Web 层只需要 `requirements.txt`；推理侧依赖 YuE2 官方环境：

- YuE2 代码与虚拟环境：`/workspace/YuE`（内含 `.venv`，已装 `torch` 与 `yue2` 包）
- 转谱环境：`/workspace/YuE/.venv-sheetsage2` + SheetSage2 权重
- NVIDIA GPU（默认 24 GiB 预算；FP8 量化需计算能力 8.9+）

若要在别的机器上跑，先按 YuE2 官方说明装好上述环境，再修改两个脚本里的 `APP_ROOT` / `VENV` 路径。

## 运行

```bash
cd /workspace/yue2-web
./start_webui.sh           # 默认 0.0.0.0:7860
PORT=8080 ./start_webui.sh # 换端口
./restart_webui.sh         # 改完代码后重启
```

等价的手动启动：

```bash
/workspace/YuE/.venv/bin/python app.py --host 0.0.0.0 --port 7860
```

打开 `http://<host>:7860`。日志在 `/workspace/logs/yue2-web.log`。

## 采样与合成参数

页面「5 · 采样与合成」分别控制两阶段自回归采样与流匹配解码，**留空即用官方默认**：

| 参数 | 规划（写乐谱） | 语义（音乐 token） | 说明 |
| --- | --- | --- | --- |
| temperature | 0.7 | 1.0 | 越低越保守可复现，越高越跳脱，0 = 贪心 |
| top_p | 0.9 | 0.95 | 核采样阈值，越低越稳 |
| max_tokens | 4096 | 9000 | 该阶段 token 上限，直接限制乐谱 / 成曲长度 |

ODE 步数默认 32，是流匹配的积分步数：越大越稳、线性变慢。`前缀 + max_tokens ≤ 24576`，超出会报错而不是截断。

## API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/device` | GPU / 环境信息 |
| GET | `/api/example/{name}` | 内置示例 |
| GET | `/api/style/fields` | 风格词库字段 |
| POST | `/api/style/build` | 按选项拼风格串 |
| GET | `/api/style/random` | 随机风格 |
| POST | `/api/style/check` | 校验风格串 |
| POST | `/api/generate` | 提交生成任务 |
| GET | `/api/job/{job_id}` · `/api/jobs` | 任务状态 |
| POST | `/api/cancel/{job_id}` | 取消任务 |
| POST | `/api/transcribe` · GET `/api/transcribe/{job_id}` | 参考音频转谱 |
| GET | `/outputs/{job_id}/{name}` | 下载产物 |
| GET | `/uploads/{job_id}/score/score.abc` | 查看转谱结果 |
