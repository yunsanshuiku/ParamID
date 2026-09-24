# 第三方组件说明

ParamID 的 MIT 许可证覆盖本项目原创代码与文档，不替代第三方组件的许可证。源码包不包含 Python 环境、WebView2 Runtime、Inno Setup 编译器或下载的依赖包。

| 组件 | 用途 | 上游许可证 / 条款 |
| --- | --- | --- |
| [NumPy](https://github.com/numpy/numpy) | 数组与线性代数 | BSD-3-Clause；wheel 可能含另附许可证的数学库 |
| [SciPy](https://github.com/scipy/scipy) | 数值优化、信号处理、矩阵函数 | BSD-3-Clause；捆绑组件按其许可 |
| [pyserial](https://github.com/pyserial/pyserial) | 串口采集 | BSD-3-Clause |
| [pywebview](https://github.com/r0x0r/pywebview) | 桌面窗口与 WebView 桥接 | BSD-3-Clause |
| [PyInstaller](https://pyinstaller.org/en/stable/license.html) | Windows 程序打包 | GPL-2.0-or-later，附允许生成应用按自身许可证分发的例外 |
| [Python](https://docs.python.org/3/license.html) | 解释器 | PSF License Agreement 及列出的第三方声明 |
| [Microsoft Edge WebView2](https://developer.microsoft.com/en-us/microsoft-edge/webview2/) | Windows 页面渲染 | Microsoft 提供的许可条款 |
| [Inno Setup](https://jrsoftware.org/isinfo.php) | 可选安装器构建 | 以所使用版本附带的许可证及使用条件为准 |

打包产物还可能包含 pythonnet、clr-loader、cffi、proxy-tools、bottle、OpenBLAS 等传递依赖；具体内容取决于 Python 环境和所安装的 wheel，其许可证以实际版本的 LICENSE / COPYING / NOTICE 为准。

本文件是组件索引，不是完整的第三方许可证合集。发布桌面二进制时，应保留相关组件的许可证、版权声明及 PyInstaller 收集到的声明文件；新增或升级依赖后需重新核对。

WebView2 Runtime 通常由系统提供，不包含在本项目安装包内。Inno Setup 是外部构建工具，本项目不下载或分发其编译器；部分版本对商业使用另有条件，构建者应遵循所用版本的条款。
