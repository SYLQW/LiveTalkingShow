# LiveTalking 本地交接说明

本文档是当前仓库的交接入口。运行规范使用 `uv`，配置优先级为：

```text
ENV > .env > config.yaml
```

核心目标：用 LiveTalking 加载数字人 avatar，接入 `robottts` 兼容 TTS 或外部 PCM 音频流，输出可给 Web/overlay 显示的数字人视频。

## 1. 链路

```text
TTS 服务，默认 8036
  提供 robottts 兼容接口，输出 16kHz mono PCM16

LiveTalking 主服务，默认 8050
  加载 avatar 和 wav2lip/musetalk/ultralight
  接收文字或外部音频
  输出本机 overlay raw alpha 或远程 packed WebRTC 透明视频

显示/测试端，Web 默认 8070，overlay 为本机 Electron
  testclient/web 用来发文字、测接口、看视频
  testclient/overlay 用来做透明置顶桌面数字人
```

两种输入方式：

| 方式 | 调用入口 | 音频来源 | 适合场景 |
| --- | --- | --- | --- |
| 文本驱动 | `POST /alpha/speak` | LiveTalking 内部 `robottts` 插件连接 TTS `/tts/ws` | 控制端只想发文字。 |
| 音频流驱动 | `POST /tts/task/start -> WS /alpha/input/audio` | 外部 TTS 主动推 PCM 给 LiveTalking | 业务系统已有 ASR/LLM/TTS 编排。 |

显示输出统一走：

```text
WS /alpha/ws                     本机 overlay 视频，24 字节帧头 + raw BGRA/RGBA/JPEG/PNG/WebP payload
WS /alpha/audio                  本机 overlay 音频，16kHz mono PCM16
POST /alpha/webrtc/packed_offer  远程显示链路，WebRTC audio + packed video
```

完整协议见 [docs/API-PROTOCOL.md](docs/API-PROTOCOL.md)。

## 2. 快速启动

### 2.1 准备环境和默认模型

```bash
cd /path/to/LiveTalking
uv sync --python 3.10 --inexact
cp .env.example .env
HF_ENDPOINT=https://hf-mirror.com ./scripts/download-models.sh wav2lip-demo
```

`wav2lip-demo` 会准备：

```text
models/wav2lip.pth
data/avatars/wav2lip_avatar_female_model/
```

模型下载脚本：

| 命令 | 作用 |
| --- | --- |
| `./scripts/download-models.sh wav2lip-demo` | 下载 Wav2Lip checkpoint 和默认 avatar。 |
| `./scripts/download-models.sh wav2lip` | 只下载 `models/wav2lip.pth`。 |
| `./scripts/download-models.sh s3fd` | 下载 Wav2Lip 制作 avatar 需要的人脸检测模型。 |
| `./scripts/download-models.sh musetalk` | 下载 MuseTalk、VAE、Whisper、DWPose、FaceParsing 模型。 |
| `./scripts/download-models.sh all` | 下载上述全部资产。 |

清晰度增强这块现在默认会去找仓库内的相对路径：

```text
tools/RealESRGAN/realesrgan-ncnn-vulkan.exe
```

这个目录里会放 `realesr-animevideov3`、`realesrgan-x4plus-anime` 这类小模型文件，别人拉到仓库以后不用再手动改盘符。要是本机想换地方，也可以再用 `REALESRGAN_PATH` 覆盖。

所有 `data/avatars/*`、`models/*`、`.env`、日志和依赖目录都不提交到 Git。

### 2.2 启动测试 TTS

生产环境接真实 `robot-tts` 或同协议服务。本地测试可以用 `testclient/backend`：

```bash
cd /path/to/LiveTalking/testclient
cp .env.example .env
TEST_TTS_PROVIDER=edge ./start-tts.sh
```

百炼 CosyVoice 测试：

```bash
TEST_TTS_PROVIDER=bailian DASHSCOPE_API_KEY=你的Key ./start-tts.sh
```

检查：

```bash
curl http://127.0.0.1:8036/health
curl http://127.0.0.1:8036/tts/voices
```

### 2.3 启动 LiveTalking

```bash
cd /path/to/LiveTalking
./entrypoint.sh
```

默认 `.env.example` 已开启：

```bash
LIVETALKING_ALPHA_OUTPUT=1
LIVETALKING_TTS=robottts
TTS_SERVER_URL=http://127.0.0.1:8036
```

临时覆盖示例：

```bash
LIVETALKING_PORT=8051 AVATAR_ID=my_avatar ./entrypoint.sh
```

### 2.4 启动 Web 测试页

```bash
cd /path/to/LiveTalking/testclient
./start-web.sh
```

访问：

```text
http://127.0.0.1:8070
```

Web 页可测试：

- TTS `/health`、`/tts/voices`
- `POST /alpha/speak`
- `POST /tts/task/start -> WS /alpha/input/audio`
- 默认 `POST /alpha/webrtc/packed_offer`，预览 WebRTC audio + packed alpha video
- `VITE_ALPHA_OUTPUT=ws` 时可用 `WS /alpha/ws` 调试视频帧、尺寸、fps
- `VITE_ALPHA_OUTPUT=ws` 时可用 `WS /alpha/audio` 调试音频播放

### 2.5 启动桌面 overlay

```bash
cd /path/to/LiveTalking/testclient
./start-overlay.sh
```

远程显示时改 LiveTalking 地址，并使用 packed WebRTC：

```bash
LIVETALKING_SERVER=http://<LiveTalking机器IP>:8050 \
LIVETALKING_OUTPUT=webrtc-packed \
./start-overlay.sh
```

overlay 只负责显示，不负责文字输入，不直接连接 TTS。

## 3. 从零部署到自己的 avatar

最短可执行流程：

1. 准备环境和 demo 资产。

```bash
cd /path/to/LiveTalking
uv sync --python 3.10 --inexact
cp .env.example .env
HF_ENDPOINT=https://hf-mirror.com ./scripts/download-models.sh wav2lip-demo
```

2. 先用 demo 跑通服务。

```bash
cd testclient
cp .env.example .env
./start-tts.sh
```

另开一个终端：

```bash
cd /path/to/LiveTalking
./entrypoint.sh
```

再开 Web 测试页：

```bash
cd /path/to/LiveTalking/testclient
./start-web.sh
```

浏览器访问 `http://127.0.0.1:8070`，确认 `health`、`alpha/speak`、视频预览正常。

3. 制作自己的 Wav2Lip avatar。

```bash
cd /path/to/LiveTalking
HF_ENDPOINT=https://hf-mirror.com ./scripts/download-models.sh s3fd
uv run --python .venv/bin/python python -m avatars.wav2lip.genavatar \
  --video_path /path/to/person.png \
  --img_size 256 \
  --avatar_id my_avatar
```

`--video_path` 可以是单张图片、PNG 序列目录或视频。透明显示建议用 RGBA PNG 或已抠好背景的 PNG 序列。
如果跳过了 demo 下载，先分别执行 `./scripts/download-models.sh wav2lip` 和 `./scripts/download-models.sh s3fd`。

4. 切换到自己的 avatar 启动。

```bash
AVATAR_ID=my_avatar ./entrypoint.sh
```

生成目录应为：

```text
data/avatars/my_avatar/
  full_imgs/
  face_imgs/
  coords.pkl
  metadata.json
```

5. 验证和排障。

- Web 默认走 `/alpha/webrtc/packed_offer` 看远程显示效果。
- 本机透明置顶运行 `cd testclient && ./start-overlay.sh`。
- 远程 overlay 运行 `LIVETALKING_SERVER=http://<LiveTalking机器IP>:8050 LIVETALKING_OUTPUT=webrtc-packed ./start-overlay.sh`。
- 人脸框不准时重做 avatar：下巴缺失调大 `--pads 0 20 0 0`，裁剪抖动可加 `--nosmooth`。

## 4. 配置

主服务配置文件：

| 文件 | 用途 |
| --- | --- |
| `config.yaml` | 团队共享默认值。 |
| `.env.example` | 环境变量模板。 |
| `.env` | 当前机器真实配置，不提交。 |
| `testclient/.env.example` | 测试客户端模板。 |
| `testclient/.env` | 测试客户端真实配置，不提交。 |

常用变量：

| 变量 | 默认 | 取值/范围 | 说明 |
| --- | --- | --- | --- |
| `LIVETALKING_CONFIG_PATH` | `config.yaml` | 相对路径或绝对路径 | 主配置文件路径；相对路径按仓库根目录解析。 |
| `HF_ENDPOINT` | `https://hf-mirror.com` | `http/https` URL | Hugging Face 下载端点；直连稳定时可改官方端点。 |
| `LIVETALKING_HOST` | `0.0.0.0` | IP/hostname | 主服务监听地址；`0.0.0.0` 允许远程访问，`127.0.0.1` 仅本机。 |
| `LIVETALKING_PORT` | `8050` | `1-65535` | 主服务 HTTP/WebSocket 端口。 |
| `LIVETALKING_MAX_SESSION` | `1` | `>=1` | 最大并发 session 数；alpha 桌面显示通常只需要 1。 |
| `LIVETALKING_MODEL` | `wav2lip` | `wav2lip` / `musetalk` / `ultralight` | 数字人后端模型。 |
| `AVATAR_ID` | `wav2lip_avatar_female_model` | `data/avatars/` 下目录名 | 当前加载的 avatar。 |
| `LIVETALKING_BATCH_SIZE` | `4` | `>=1`，常用 `1-16` | 推理 batch；小一些延迟低，大一些可能吞吐高但更占显存。 |
| `LIVETALKING_FPS` | `25` | `>0`，建议 `25` | 内部视频节奏；素材通常按 25fps 制作。 |
| `LIVETALKING_TRANSPORT` | `webrtc` | `webrtc` / `rtmp` / `rtcpush` / `virtualcam` | 基础输出方式；本地 alpha/Web/overlay 显示建议保持 `webrtc`。 |
| `LIVETALKING_ALPHA_OUTPUT` | `1` | bool：`1/0`、`true/false` | 开启 `/alpha/input/audio`、`/alpha/webrtc/packed_offer`、`/alpha/ws`、`/alpha/audio`。 |
| `LIVETALKING_TTS` | `robottts` | 常用 `robottts` | TTS 插件；交接主链路统一用 robottts 兼容接口。 |
| `TTS_SERVER_URL` | `http://127.0.0.1:8036` | `http/https` URL | robottts 兼容 TTS 服务地址。 |
| `ROBOTTTS_MODE` | `instruct2` | 由 TTS 服务解释 | robottts 模式，透传给 TTS。 |
| `LIVETALKING_MOTION_STRATEGY` | `weighted_no_repeat` | `sequence` / `random` / `weighted_random` / `no_repeat_random` / `weighted_no_repeat` | 同一状态下多个动作素材的选择策略。 |
| `MOTION_LLM_BASE_URL` | 空 | `http/https` URL 或空 | `/motion/plan` 使用的可选 LLM 地址。 |
| `MOTION_LLM_MODEL` | 空 | string 或空 | `/motion/plan` 使用的可选 LLM 模型名。 |

端口联动：

| 改动 | 同步修改 |
| --- | --- |
| LiveTalking 端口 | `LIVETALKING_SERVER`、`VITE_LIVETALKING_URL`、`VITE_ALPHA_INPUT_WS`。 |
| TTS 端口 | `TTS_SERVER_URL`、`VITE_TTS_SERVER_URL`。 |
| 远程访问 | 客户端地址改成服务机器 IP 或端口转发后的地址。 |

## 5. 常用接口样例

接口协议和字段说明见 [docs/API-PROTOCOL.md](docs/API-PROTOCOL.md)。

创建 alpha session：

```bash
curl -X POST http://127.0.0.1:8050/alpha/session \
  -H 'Content-Type: application/json' \
  -d '{"reuse":true}'
```

文本驱动：

```bash
curl -X POST http://127.0.0.1:8050/alpha/speak \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "你好，我是桌面数字人。",
    "type": "echo",
    "interrupt": true,
    "tts": {
      "voice_id": 0,
      "mode": "instruct2",
      "prompts": "请自然清晰地朗读。"
    }
  }'
```

外部 TTS 推音频：

```bash
curl -X POST http://127.0.0.1:8036/tts/task/start \
  -H 'Content-Type: application/json' \
  -d '{
    "task_id": "demo-001",
    "text": "这段话由外部 TTS 生成音频，再推给 LiveTalking。",
    "voice_id": 0,
    "mode": "instruct2",
    "prompts": "请自然清晰地朗读。",
    "target_hardware": "ws://127.0.0.1:8050/alpha/input/audio"
  }'
```

alpha 视频：

```text
ws://127.0.0.1:8050/alpha/ws?max_height=720&fps=25&format=jpeg&quality=80
ws://127.0.0.1:8050/alpha/ws?format=bgra
```

动作素材：

```bash
curl -X POST http://127.0.0.1:8050/motion/select \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionid": "当前 session id",
    "kind": "speaking",
    "action_id": "auto"
  }'
```

Wav2Lip 支持新版 avatar-local 动作格式：动作素材和 `motion.json` 都放在 `data/avatars/<avatar_id>/` 下。`speaking` 和 `idle` 两个状态可以分别制作多个动作素材，并用 `auto` 自动素材池轮换播放。没有 `motion.json` 的 avatar 继续按原有方式运行。制作流程、素材要求、批量 manifest 和参数范围见 [docs/SPEAKING-MOTION-CLIPS.md](docs/SPEAKING-MOTION-CLIPS.md)。

## 6. avatar 制作和导入

avatar 目录统一放在：

```text
data/avatars/<avatar_id>/
```

当前支持两种 avatar 目录格式：

| 格式 | 识别方式 | 适合场景 | 加载行为 |
| --- | --- | --- | --- |
| 基础格式 | 目录下没有 `motion.json` | 单套循环帧，或兼容旧素材 | 主 avatar 从 `data/avatars/<avatar_id>/` 加载；动作素材可继续放旧外置目录 `data/speaking_actions/<avatar_id>/`、`data/idle_actions/<avatar_id>/`。 |
| avatar-local 动作格式 | 目录下存在 `motion.json` | 一个 avatar 内管理多个说话/静息动作 | 主 avatar 和动作素材都放在 `data/avatars/<avatar_id>/` 内，动作池按 `motion.json` 配置选择。 |

基础格式最少需要：

```text
data/avatars/<avatar_id>/
  full_imgs/
  face_imgs/
  coords.pkl
  metadata.json        可选
```

avatar-local 动作格式推荐结构：

```text
data/avatars/<avatar_id>/
  full_imgs/
  face_imgs/
  coords.pkl
  metadata.json
  motion.json
  motions/
    speaking/<action_id>/
    idle/<action_id>/
```

每个 `motions/<kind>/<action_id>/` 目录本身也是一套基础格式，至少包含 `full_imgs/`、`face_imgs/`、`coords.pkl`。

带 `motion.json`、`motions/speaking`、`motions/idle` 的 avatar-local 制作流程，见 [docs/SPEAKING-MOTION-CLIPS.md#制作带多动作的-avatar-local-avatar](docs/SPEAKING-MOTION-CLIPS.md#制作带多动作的-avatar-local-avatar)。

切换数字人：

```bash
AVATAR_ID=<avatar_id> ./entrypoint.sh
```

### 5.1 Wav2Lip

适合快速制作、桌面助手、透明 PNG/PNG 序列。

必要文件：

```text
models/wav2lip.pth
data/avatars/<avatar_id>/full_imgs/
data/avatars/<avatar_id>/face_imgs/
data/avatars/<avatar_id>/coords.pkl
```

准备模型：

```bash
HF_ENDPOINT=https://hf-mirror.com ./scripts/download-models.sh wav2lip
./scripts/download-models.sh s3fd
```

制作命令：

```bash
HF_ENDPOINT=https://hf-mirror.com uv run --python .venv/bin/python python -m avatars.wav2lip.genavatar \
  --video_path /path/to/person.png \
  --img_size 256 \
  --avatar_id my_avatar
```

`--video_path` 支持图片、图片目录或视频。透明显示建议用 RGBA PNG 或 PNG 序列。

| 参数 | 范围/取值 | 默认 | 说明 |
| --- | --- | --- | --- |
| `--video_path` | 文件或目录 | 必填 | 输入素材。 |
| `--avatar_id` | 目录名 | `wav2lip_avatar1` | 输出目录名。 |
| `--save_path` | 目录 | `data/avatars` | avatar 根目录。 |
| `--img_size` | 正整数 | `96` | 普通模型可用 96；`wav2lip256` 建议 256。 |
| `--pads top bottom left right` | 常用 `-50` 到 `100` | `0 10 0 0` | 人脸框边距；下巴缺失增大 `bottom`。 |
| `--nosmooth` | 开关 | 关闭 | 裁剪跟不上脸时可试。 |
| `--face_det_batch_size` | 正整数 | `16` | OOM 时降到 `8/4/1`。 |

绿幕素材可先抠成 PNG 序列：

```bash
uv run --python .venv/bin/python python tools/chroma_key_video.py \
  --input /path/to/green-screen.mp4 \
  --out-dir tmp/green_avatar_alpha_frames \
  --preview tmp/green_avatar_alpha_preview.png
```

### 5.2 MuseTalk

质量潜力更高，依赖更重。制作前准备模型：

```bash
HF_ENDPOINT=https://hf-mirror.com ./scripts/download-models.sh musetalk
```

需要额外安装 MMPose/MMCV 相关依赖：

```bash
uv run --python .venv/bin/python pip install -U openmim
uv run --python .venv/bin/python mim install "mmengine" "mmcv" "mmdet" "mmpose"
```

制作命令：

```bash
HF_ENDPOINT=https://hf-mirror.com uv run --python .venv/bin/python python -m avatars.musetalk.genavatar \
  --file /path/to/person.mp4 \
  --avatar_id my_musetalk_avatar \
  --version v15 \
  --bbox_shift 0 \
  --extra_margin 10 \
  --parsing_mode jaw
```

| 参数 | 范围/取值 | 默认 | 说明 |
| --- | --- | --- | --- |
| `--file` | 图片、PNG 目录或视频 | 必填 | 输入素材。 |
| `--avatar_id` | 目录名 | `musetalk_avatar1` | 输出目录名。 |
| `--version` | `v1` / `v15` | `v15` | 推荐 `v15`。 |
| `--bbox_shift` | 常用 `-50` 到 `50` | `0` | 嘴部位置偏时小步调。 |
| `--extra_margin` | 常用 `0-40` | `10` | 下巴/嘴巴被截断时调大。 |
| `--parsing_mode` | `jaw` / `neck` / `raw` | `jaw` | mask 范围。 |
| `--gpu_id` | GPU 编号 | `0` | 兼容参数。 |

### 5.3 Ultralight

轻量方案，需要已有 checkpoint。

```bash
uv run --python .venv/bin/python python -m avatars.ultralight.genavatar \
  --video_path /path/to/person.mp4 \
  --img_size 168 \
  --checkpoint /path/to/ultralight.pth \
  --avatar_id my_ultralight_avatar
```

必要文件：

```text
data/avatars/<avatar_id>/full_imgs/
data/avatars/<avatar_id>/face_imgs/
data/avatars/<avatar_id>/coords.pkl
data/avatars/<avatar_id>/ultralight.pth
```

## 7. 测试客户端

测试客户端说明见 [testclient/README.md](testclient/README.md)。

最小启动：

```bash
cd /path/to/LiveTalking/testclient
cp .env.example .env
./start-tts.sh
./start-web.sh
./start-overlay.sh
```

常用变量：

| 变量 | 默认 | 取值/范围 | 说明 |
| --- | --- | --- | --- |
| `TEST_TTS_PROVIDER` | `edge` | `edge` / `bailian` | 测试 TTS provider。 |
| `TTS_SERVICE_PORT` | `8036` | `1-65535` | 测试 TTS 端口。 |
| `TEST_CLIENT_PORT` | `8070` | `1-65535` | Web 测试页端口。 |
| `VITE_LIVETALKING_URL` | `http://127.0.0.1:8050` | `http/https` URL | Web 浏览器访问 LiveTalking 的地址。 |
| `VITE_ALPHA_INPUT_WS` | `ws://127.0.0.1:8050/alpha/input/audio` | `ws/wss` URL | TTS task 推音频目标。 |
| `VITE_ALPHA_OUTPUT` | `webrtc-packed` | `webrtc-packed` / `ws` | Web 视频输出链路；正常使用保持默认。 |
| `LIVETALKING_SERVER` | `http://127.0.0.1:8050` | `http/https` URL | overlay 连接的 LiveTalking 地址。 |
| `LIVETALKING_OUTPUT` | `ws` | `ws` / `webrtc-packed` | overlay 视频输出链路；本机显示用 `ws`，远程显示用 `webrtc-packed`。 |
| `LIVETALKING_PLAY_AUDIO` | `0` | bool：`0/1` | overlay 是否播放 LiveTalking 输出音频。 |

Web/overlay 的帧率、最大高度、编码格式、渲染器等属于高级排障参数，默认值由代码提供，需要时见 [testclient/README.md](testclient/README.md) 临时覆盖。

## 8. 排错

端口：

```bash
ss -ltnp | grep -E ':8036|:8050|:8070'
```

接口：

```bash
curl http://127.0.0.1:8036/health
curl -X POST http://127.0.0.1:8050/alpha/session \
  -H 'Content-Type: application/json' \
  -d '{"reuse":true}'
```

常见问题：

| 问题 | 处理 |
| --- | --- |
| Web/overlay 连不上 | 检查端口、IP、VS Code/SSH 端口转发和 `.env` 地址。 |
| overlay 没画面 | 确认 `LIVETALKING_ALPHA_OUTPUT=1`；本机默认连接 `/alpha/ws`，远程 packed 模式连接 `/alpha/webrtc/packed_offer`。 |
| 画面尺寸不对 | 检查 `data/avatars/<avatar_id>/full_imgs` 原图宽高；显示缩放不裁剪原图。 |
| 声音重复 | 保持 `LIVETALKING_PLAY_AUDIO=0`，只让一个组件播放声音。 |
| 推理卡顿 | 降低 `LIVETALKING_BATCH_SIZE`，降低 overlay/web 拉流 `fps/max_height`。 |
| 人脸裁剪不准 | Wav2Lip 调 `--pads`；MuseTalk 调 `--bbox_shift`、`--extra_margin`。 |
| Hugging Face 访问慢 | 使用 `HF_ENDPOINT=https://hf-mirror.com`。 |

## 9. 交接清单

1. `uv sync --python 3.10 --inexact`
2. `cp .env.example .env`
3. `HF_ENDPOINT=https://hf-mirror.com ./scripts/download-models.sh wav2lip-demo`
4. 启动真实 `robottts` 兼容 TTS，或先启动 `testclient/backend`
5. `./entrypoint.sh`
6. `testclient/web` 验证文字、TTS task、视频
7. `testclient/overlay` 验证透明置顶显示
8. 替换自己的 avatar，设置 `AVATAR_ID`
