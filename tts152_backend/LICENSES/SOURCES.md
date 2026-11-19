# Edge TTS 来源与权利边界

确切方案：rany2/edge-tts Python 客户端 7.3.1 + 用户既有 GitHub Actions 执行器 + 微软 Edge 在线语音服务。不是可下载的本地模型，也不是微软官方开放API；没有把服务包装项目冒称模型。

来源：
- https://github.com/rany2/edge-tts
- https://pypi.org/project/edge-tts/7.3.1/
- https://raw.githubusercontent.com/rany2/edge-tts/master/LICENSE
- 原参考附件 SHA256：25427c69058e46ead57ce9a460adad2ee7dfa91f09d5c92f8e9792781520f139

本目录 edge-tts-7.3.1-LICENSE.txt 直接取自所测试的 edge-tts 7.3.1 官方PyPI wheel，主体 LGPLv3，srt_composer.py 单独 MIT。不是“全库MIT”。本包仅保留许可告知，未捆绑Python库、依赖wheel、微软模型权重、API凭据。用户安装时从PyPI获取固定版本依赖。GitHub后台新增脚本为本项目集成实现，未复制库的内部协议/密钥/伪装请求；调用已发布Python入口。

开源客户端许可不等于微软模型可再分发许可，也不等于微软授予无限免费/商业使用/SLA。这里没有可供再分发的模型权重。所用在线接口未提供本集成可据以承诺的正式免费额度与服务保证；音色可用性会变化，应按自身用途核实服务条款与权利。

文本将经过本站（Cloudflare）、GitHub执行器并发送给微软，不是离线。GitHub/R2/D1也有免费额度和超额收费条件。原云端Gemini同样可能收费，未改其计费方式。

GitHub用量依据：
https://docs.github.com/en/billing/managing-billing-for-your-products/managing-billing-for-github-actions/about-billing-for-github-actions
公开仓库标准runner免费不代表可把私人代码/信息公开；私有仓库消耗账户包含额度，超额可能收费。不要为了免费擅自改仓库可见性。

新增后端 requirements.txt 固定16个本地已测试的依赖版本及每个Linux x64 CPython3.13 wheel的SHA256，工作流Python3.13；语音客户端及其依赖安装使用 --only-binary=:all:，不运行源码包安装脚本。依赖仍适用各自的原许可。参考视频程序使用Python3.11，本版独立音频工作流不修改它。

## 缓存优化新增解码器（不是语音来源）
- miniaudio 1.73：官方发布 https://pypi.org/project/miniaudio/1.73/ ，上游 https://github.com/irmen/pyminiaudio 。Python绑定/内置miniaudio解码组件的wheel LICENSE为MIT，本目录保留原文。没有设备播放/录音权限调用，仅流式文件解码。
- 新增依赖cffi 2.1.1、pycparser 3.12：来自各自官方PyPI wheel，许可证原文分别保存在子目录；其余依赖版本未改。
- `dependencies-linux-x64-py313.json`记录全部官方wheel文件名、下载URL、大小及发布SHA256，已逐个与官方PyPI版本JSON核对。requirements使用同一哈希；未内嵌/下载随机第三方ffmpeg二进制。
- wheel解码器在首次部署工作流运行时才下载，不是需要新托管的模型；语音依然发送给原微软在线语音服务。
- 默认系统ffmpeg仅复用runner已有安装，不重新分发其二进制；原有许可依runner镜像与工具自身条款。

### 官方GitHub Actions固定提交（本次核对）
- actions/cache@v4: `0057852bfaa89a56745cba8c7296529d2fc39830` https://github.com/actions/cache/commit/0057852bfaa89a56745cba8c7296529d2fc39830
- actions/checkout@v4: `11d5960a326750d5838078e36cf38b85af677262` https://github.com/actions/checkout/commit/11d5960a326750d5838078e36cf38b85af677262
- actions/setup-python@v5: `a26af69be951a213d495a4c3e4e4022e16d87065` https://github.com/actions/setup-python/commit/a26af69be951a213d495a4c3e4e4022e16d87065

缓存权限/作用域/不可变性：https://docs.github.com/en/actions/using-workflows/caching-dependencies-to-speed-up-workflows 。使用官方actions/cache运行时缓存令牌，不需要给仓库GITHUB_TOKEN新增actions:write；仍保持contents:read，缓存不是私密存储。
