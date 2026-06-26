# RealESRGAN 资源说明

这个目录放的是 LiveTalking 的离线清晰度增强资源。

## 目录里需要有什么

如果要跑 `realesr-animevideov3` 或 `realesrgan-x4plus-anime`，至少要有这些东西：

```text
tools/RealESRGAN/
  realesrgan-ncnn-vulkan.exe
  models/
    realesr-animevideov3-x2.bin
    realesr-animevideov3-x2.param
    realesr-animevideov3-x3.bin
    realesr-animevideov3-x3.param
    realesr-animevideov3-x4.bin
    realesr-animevideov3-x4.param
    realesrgan-x4plus-anime.bin
    realesrgan-x4plus-anime.param
```

## 这些文件有多大

这几个模型文件都不大，真正占空间的是运行时本体。

- `realesr-animevideov3-*` 三套参数文件加起来大约 3.7 MB
- `realesrgan-x4plus-anime.*` 大约 9 MB
- `realesrgan-ncnn-vulkan.exe` 大约 6 MB

也就是说，模型本体加起来就是几十 MB 级别，不是那种必须放到网盘里单独交接的大包。

## 默认怎么找

后端会优先读环境变量 `REALESRGAN_PATH`。

如果没配，就会从当前仓库里找：

```text
tools/RealESRGAN/realesrgan-ncnn-vulkan.exe
```

所以只要把整个仓库拉下来，路径又没改坏，直接用相对路径就能跑。

## 下载来源

这些文件原本来自 Real-ESRGAN 的 ncnn-vulkan Windows 发行包。

如果本地没带齐，就去官方仓库或者 release 包里补回来，再放进这个目录。
