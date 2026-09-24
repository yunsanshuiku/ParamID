# 开发与贡献

建议先通过 Issue 说明可复现问题或拟加入的能力。提交 bug 时提供软件版本、采样周期、算法和窗长，以及可以公开的最小数据样例；仿真数据请给出真实模型和噪声生成方式。

## 本地开发

在 Python 3.10–3.12 环境安装 `requirements.txt` 即可运行核心算法与测试；桌面开发安装 `requirements-desktop.txt`。入口与目录见 [README](README.md)。

```powershell
python -m unittest discover -s tests -q
node --check web/app.js
node --check scripts/ui_smoke.mjs
```

修改界面或接口后运行 `node scripts/ui_smoke.mjs`。需要 Node.js 22+ 与 Chrome / Edge，可通过 `PARAMID_PYTHON`、`PARAMID_BROWSER` 指定位置。

修改桌面启动或打包配置后，额外运行 `python desktop.py --self-test`，并在 Windows 实际检查打包程序。

## 算法与数据约定

- 保持控制理论模型符号与实际参数顺序一致，注明矩阵维度、采样周期和延迟。
- 用 Docstring 说明算法目标、适用条件和数值处理，避免显式求正规方程的逆。
- 训练、选模、验证段不能相互泄漏；在线只读取当前及历史样本。
- 以已知系统仿真验证修正，比较预测误差与自由运行误差。
- 数值失败需给出原因，不能静默改阶、重置估计器或伪造成功。
- 核心算法变化应增加有明确失效场景的测试；纯文案修改无需新增机械测试。

## 提交内容

说明问题、最终行为与实际验证结果。不要提交个人实验记录、采集输出、本机环境、访问凭据或构建产物。更新功能时同步维护文档和 `CHANGELOG.md`。

贡献的原创内容按 MIT 许可证分发。引入其他来源代码或素材时，保留来源和许可要求。
