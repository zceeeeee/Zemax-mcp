# Zemax-mcp

Ansys Zemax OpticStudio 2024 R2 自动化光学设计脚手架，采用 **常驻服务端 + HTTP 命令行客户端** 架构。虽然仓库名包含 MCP，当前实现是本地 HTTP CLI，并非 MCP 协议服务器。

## 环境要求

- Windows，以及已安装并获得有效授权的 OpticStudio 2024 R2。
- Python 3.9–3.12；本机联调使用 Python 3.11。
- OpticStudio 中开启 **Programming → Interactive Extension**。

## 安装与启动

在 PowerShell 中执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

在独立终端中启动服务端。安装位置不同时，通过环境变量指定目录，该目录应包含 `ZOSAPI.dll` 与 `ZOSAPI_Interfaces.dll`：

```powershell
$env:OPTICSTUDIO_DIRECTORY = "E:\Zemax\ANSYS Inc\v242\Zemax OpticStudio"
.\.venv\Scripts\python.exe zemax_server.py
```

服务仅监听 `http://127.0.0.1:8765`，没有身份验证；不要将端口转发或暴露到外网。服务端改动后必须重启进程。

## 单透镜闭环示例

保持服务端运行，在另一个终端执行：

```powershell
.\.venv\Scripts\python.exe zemax_cli.py status
.\.venv\Scripts\python.exe zemax_cli.py build outputs/test_singlet.json
.\.venv\Scripts\python.exe zemax_cli.py optimize --effl 100.0
.\.venv\Scripts\python.exe zemax_cli.py evaluate --max-freq 50 --points 5
.\.venv\Scripts\python.exe zemax_cli.py save outputs/test_singlet_spot_optimized.zmx

# 若点列图优化后波前 RMS 仍大于 0.15 waves，按工程规范进行二次精修：
.\.venv\Scripts\python.exe zemax_cli.py optimize --effl 100.0 --achromatic --wavefront
.\.venv\Scripts\python.exe zemax_cli.py evaluate --max-freq 50 --points 5
.\.venv\Scripts\python.exe zemax_cli.py save outputs/test_singlet_wavefront_optimized.zmx
```

- 示例使用 N-BK7、入瞳直径 25 mm，目标焦距 100 mm；不保证单片球面透镜达到衍射极限。
- `build` 会替换当前光学系统；操作已有镜头前先 `save`。
- 用 `load outputs/test_singlet_wavefront_optimized.zmx` 可恢复本机已保存的二次优化结构；生成镜头不随仓库分发。
- 有焦系统优化必须指定 `--effl`。光线追迹失败时先修正结构，不能不改参数反复优化。
- 无穷大状态值输出为字符串 `"Infinity"` / `"-Infinity"`，NaN 输出为 `null`。
- `rms_spot_radius` 为“配置 × 视场”的二维列表，单位采用当前镜头单位（示例为 mm）。
- 波前 RMS 在主波长处计算，RWCE 使用系统设置对应的归一化视场坐标。
- MTF 的实际曲线采用多色光 MTFT/MTFS；衍射极限采用 MTFA 的 `Field=0, Grid=1` 参考响应，并非各视场独立的理想上限。频率单位遵循镜头设置（示例为 lp/mm）。

更多设计规则参见 [CLAUDE.md](CLAUDE.md)。[项目部署与使用指南.md](项目部署与使用指南.md) 是初始部署参考，运行代码已包含后续接口兼容修正，应以当前代码为准。

## 当前验证状态

2026-10-27 已在本机完成真实 CLI 闭环：连接 → 状态读取 → 构建 → 焦距约束点列图优化 → 评估 → 保存 → 波前/轴向色差约束二次优化 → 评估 → 保存。两次优化及最终状态均返回 `ray_trace_valid=true`，兼容性回归的 11 项测试和 `pip check` 通过。

测试结构为 N-BK7 球面单透镜，入瞳直径 25 mm、目标焦距 100 mm、唯一视场为轴上 0°，采用 F/d/C 可见光预设。

| 指标 | 点列图优化后 | 波前/轴向色差约束二次优化后 |
|---|---:|---:|
| 有效焦距（mm，CLI 舍入值） | 100.000 | 100.018 |
| 相对目标焦距误差（约） | 0.000% | 0.018% |
| RMS 点斑半径（μm） | 66.72 | 67.61 |
| 轴上主波长波前 RMS（waves） | 2.0192 | 1.6220 |
| 满足波前 RMS < 0.0715 waves | 否 | 否 |

最终工作 F 数为 3.944。**自动化闭环与焦距验收通过，但该单透镜尚未达到衍射极限。** 二次优化降低了波前 RMS，但点斑略增大；两次优化采用不同基底和约束，评价函数数值不能直接横向比较。

最终多色光 MTF 返回值（`evaluate --max-freq 50 --points 5`）：

| 空间频率（lp/mm） | Tangential | Sagittal | 平均值 | 衍射极限参考 |
|---|---:|---:|---:|---:|
| 10 | 0.1187 | 0.1189 | 0.1188 | 0.9702 |
| 20 | 0.0715 | 0.0708 | 0.0711 | 0.9412 |
| 30 | 0.0628 | 0.0588 | 0.0608 | 0.9121 |
| 40 | 0.0088 | 0.0310 | 0.0199 | 0.8830 |
| 50 | 0.0800 | 0.1350 | 0.1075 | 0.8539 |

- MTF 当前使用固定 `Samp=2`，尚未验证采样收敛。轴上高频处 T/S 差异明显，以上仅记录当前设置下的实际接口返回，不作为高精度像质验收依据。
- 此次波前 RMS 大于 1 wave，代码返回的 `strehl_ratio_approx=0.0` 是保护分支输出，不是精确 Strehl 测量。
- 两阶段镜头分别保存在本机 `outputs/test_singlet_spot_optimized.zmx` 和 `outputs/test_singlet_wavefront_optimized.zmx`，均未上传仓库。

## 离线检查

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
```

离线测试不导入服务端、不连接 OpticStudio，覆盖接口适配辅助函数及异常时的工具释放。测试通过仅说明离线逻辑正确，光学指标必须以真实 CLI 评估结果为准。

## 文件布局

| 文件 | 用途 |
|---|---|
| `zemax_server.py` | 保持 ZOS-API 连接，封装状态、构建、修改、优化、评估与保存 |
| `zemax_cli.py` | 本地 HTTP 客户端 |
| `requirements.txt` | Python 依赖 |
| `outputs/test_singlet.json` | 可复现的单透镜初始结构 |
| `tests/` | 不连接 OpticStudio 的离线兼容性测试 |

虚拟环境、凭据、运行日志、生成的镜头文件和本地会话记录不提交到仓库。仓库不包含 OpticStudio 软件、DLL 或授权文件。
