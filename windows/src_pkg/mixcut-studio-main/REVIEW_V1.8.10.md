# V1.8.10 验证与现场恢复

## 复现证据

最终冻结程序在用户真实后台上运行只读 --diagnose-startup：直连能取得后台版本及协议；旧 urllib.urlopen 调用可稳定复现 FileNotFoundError。异常路径为 urllib build_opener → HTTPSHandler → http.client._create_https_context → ssl.create_default_context → SSLContext.__new__。没有把此错误归因为用户代理或具体证书文件缺失；缺失对象的更底层来源尚未定位，本次修复使纯本机 HTTP 管理不再依赖 HTTPS 初始化。

## 检查

- 342 项 unittest 全部通过；新增真实本机 HTTP 服务测试覆盖 SSL 初始化不可用、代理查询不可用、退出 POST、连接失败处理与连接释放。
- 冻结包内版本 1.8.10、内置 FFmpeg/FFprobe、ad-hoc 深度严格签名验证通过。
- 通过 Applications 中的正式客户端图标启动：新后台 1.8.10 正常，主启动器及其原生窗口子进程正常。
- 原生 WebKit --smoke 实际访问后台页面并核验 API 协议，返回 native_window_smoke=ok。
- 现场安装前核对 active_work=False、无运行或排队批次；SQLite 一致性备份及旧 app 保存于用户 Application Support/MixCutStudio/升级备份。安装后原批次 ID、暂停状态和任务数保持一致，没有启动任务或修改素材。

本次只修复本机启动检测，不声称已修复打包环境所有 HTTPS 功能。安装包 arm64，使用 ad-hoc 签名而非 Apple 公证。
