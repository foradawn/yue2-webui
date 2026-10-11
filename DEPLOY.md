# 在新服务器上部署

本仓库只包含 Web 层（`app.py` + 前端 + 风格助手）。推理依赖 YuE2 官方仓库与环境，需要单独装一次。

## 0. 先决条件

| 项 | 要求 |
| --- | --- |
| GPU | NVIDIA，BF16，≥ 24 GB 显存（实测环境 torch 2.10.0+cu128 / CUDA 12.8） |
| 系统 | Ubuntu 22.04 / 24.04，驱动已装好（`nvidia-smi` 可用） |
| Python | 3.12（主环境，官方推荐）；SheetSage2 需另建 3.10/3.11 环境 |
| FFmpeg | 6.1 及共享库（**仅转谱功能需要**，SheetSage2 硬性要求） |
| 磁盘 | ≥ 40 GB：模型缓存约 11 GB，两个虚拟环境约 13 GB |

## 1. 安装 YuE2 推理环境

```bash
git clone https://github.com/multimodal-art-projection/YuE.git /workspace/YuE
cd /workspace/YuE
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .          # 拉 torch==2.10.0 / transformers==4.57.6 等
```

首次生成时会自动从 Hugging Face 下载 `m-a-p/YuE2-3B` 与 `m-a-p/YuE2-Vae`（落到 `~/.cache/huggingface`）。想提前下：

```bash
.venv/bin/huggingface-cli download m-a-p/YuE2-3B
.venv/bin/huggingface-cli download m-a-p/YuE2-Vae
```

验证：

```bash
.venv/bin/python examples/generate.py --output outputs/first-song
```

## 2.（可选）装 SheetSage2 转谱环境

只有「上传参考音频 → 转谱翻唱」要用到。SheetSage2 与 YuE2 依赖版本冲突，必须独立环境。**缺了不影响生成功能**，页面 `/api/device` 里 `sheetsage2` 会显示 false。

```bash
cd /workspace/YuE
python3.11 -m venv .venv-sheetsage2
.venv-sheetsage2/bin/python -m pip install huggingface-hub==0.36.0
.venv-sheetsage2/bin/huggingface-cli download m-a-p/SheetSage2 --local-dir models/SheetSage2
.venv-sheetsage2/bin/python -m pip install torch==2.8.0 torchaudio==2.8.0 \
  --index-url https://download.pytorch.org/whl/cu126
.venv-sheetsage2/bin/python -m pip install -r models/SheetSage2/requirements.txt
```

按 SheetSage2 模型卡装好 FFmpeg 6.1 及其 dev 包（`libavcodec-dev` 等）。

## 3. 部署 Web 层

```bash
git clone https://github.com/foradawn/yue2-webui.git /workspace/yue2-web
cd /workspace/yue2-web
/workspace/YuE/.venv/bin/python -m pip install -r requirements.txt   # fastapi / uvicorn / ...
```

目录约定（可用环境变量覆盖，`app.py` 启动时读取）：

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `YUE2_REPO` | `/workspace/YuE` | YuE2 仓库根（其下找 `.venv-sheetsage2`、`models/SheetSage2`） |
| `YUE2_STYLE_SKILL` | `~/.codebuddy/skills/music-style-designer` | 风格助手 skill；**不存在时自动回落到仓库自带副本** |

YuE2 仓库不在默认路径时：

```bash
export YUE2_REPO=/opt/YuE
```

## 4. 启动

```bash
cd /workspace/yue2-web
./start_webui.sh                      # 默认 0.0.0.0:7860，幂等
PORT=8080 ./start_webui.sh
./restart_webui.sh                    # 改代码后重启
```

脚本里的 `APP_ROOT` / `VENV` 是绝对路径，路径不同直接改这两行。等价手动命令：

```bash
CUDA_VISIBLE_DEVICES=0 /workspace/YuE/.venv/bin/python app.py --host 0.0.0.0 --port 7860
```

日志：`/workspace/logs/yue2-web.log`。首次请求要加载权重，约 20–30 秒；切换 VAE / 显存预算 / 采样参数会重建流水线，同样的等待。

## 5. 作为服务常驻（systemd 样例）

`/etc/systemd/system/yue2-web.service`：

```ini
[Unit]
Description=YuE2 Web UI
After=network.target

[Service]
Type=simple
WorkingDirectory=/workspace/yue2-web
Environment=CUDA_VISIBLE_DEVICES=0
Environment=YUE2_REPO=/workspace/YuE
ExecStart=/workspace/YuE/.venv/bin/python app.py --host 127.0.0.1 --port 7860
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now yue2-web
```

## 6. 对外访问

页面**没有登录和鉴权**，不要直接暴露公网。用 Nginx 反代并加一层 Basic Auth 或只允许内网 / VPN：

```nginx
location / {
    proxy_pass http://127.0.0.1:7860;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_read_timeout 1800s;   # 生成任务耗时长
    proxy_buffering off;        # 保证 token 进度实时返回
}
```

## 7. 常见问题

- **显存不够**：页面把「显存预算」调到 8/12/16；或勾选 `offload_ar`（AR 权重卸载到 CPU，更慢）。FP8 量化需计算能力 8.9+，A 卡 8.6 不支持。
- **生成被截断 / 副歌没出来**：提高「语义 max_tokens」（默认 9000），乐谱写不下则提高「规划 max_tokens」（默认 4096）；上限是 `前缀 + max_tokens ≤ 24576`。
- **转谱报 503**：`.venv-sheetsage2` 或 `models/SheetSage2` 缺失，回到第 2 步；FFmpeg 版本不对也会失败。
- **GPU 正忙（409）**：转录与生成串行共用一张卡，等前一个任务结束或点取消。
- **outputs / uploads 涨得快**：`outputs/<job_id>/` 存音频、乐谱、语义 token，`uploads/` 存上传的参考音频，定期清理即可（两者都已 gitignore）。
