# MixCut Studio macOS 打包

当前版本号只维护在项目根目录的 `VERSION` 文件中。macOS 应用的 Info.plist、DMG 文件名、运行时接口和校验流程都读取该版本号。

## 构建环境

- macOS 12 或更高版本
- Python 3.12 或更高版本
- PyInstaller、Pillow
- FFmpeg、ffprobe（构建时会连同动态库一起收进应用）

```bash
python3 -m pip install pyinstaller pillow
python3 macos/build_tools/build.py
```

输出位于 `macos/output/`：

- `MixCutStudio-<版本>-macOS-<架构>.dmg`
- 同名 `.sha256` 校验文件

构建脚本会生成图标、冻结 `.app`、进行临时签名、启动成品验证版本号和内置 FFmpeg，最后生成并压缩 DMG。

## 发布新版本

1. 修改根目录 `VERSION`，使用 `主版本.次版本.修订号`，例如 `1.2.0`。
2. 运行完整测试：`python3 -m unittest discover -s tests -q`。
3. 运行构建：`python3 macos/build_tools/build.py`。
4. 校验 DMG 后提交代码并创建同版本 Git 标签。

当前构建是 Apple Silicon `arm64`。若要发布 Intel 版本，需要在 Intel runner 上重新构建；不要把 arm64 DMG 标记成通用版本。

## 签名说明

没有 Apple Developer ID 时，构建脚本使用临时签名，能校验包内文件完整性，但不能通过 Apple 公证。分发给其他用户时，macOS 可能提示“无法验证开发者”。正式公开分发应配置 Developer ID Application 证书并执行 notarization。
