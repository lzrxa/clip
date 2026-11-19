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

新增后端 requirements.txt 固定13个本地已测试的依赖版本，工作流Python3.13；语音客户端及其依赖安装使用 --only-binary=:all:，不运行源码包安装脚本。依赖仍适用各自的原许可。参考视频程序使用Python3.11，本版独立音频工作流不修改它。
