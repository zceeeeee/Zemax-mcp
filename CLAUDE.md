# Ansys Zemax 光学设计 Agent 强制工程规范 (CLAUDE.md)

## 一、 工具调用红线 (触犯即任务失败)
后台已运行常驻守护进程 `zemax_server.py`。**严禁你直接编写含 `import zospy` 的临时脚本去连接 Zemax！**
你必须且只能通过命令行运行 `python zemax_cli.py` 与 Zemax 交互：

1. **查看当前状态**：`python zemax_cli.py status`
2. **加载现有/官方样例镜头**：`python zemax_cli.py load "<.zmx文件路径>"`
3. **全新构建系统**：先将结构保存为 `outputs/init_system.json`，再运行 `python zemax_cli.py build outputs/init_system.json`
4. **单面参数微调与设变量**：`python zemax_cli.py patch --index 2 --radius 100.0 --var-r 1 --var-t 1`；恢复自动半口径用 `--semi-diameter-auto`，移动光阑用 `--stop`。
5. **带物理约束优化**：`python zemax_cli.py optimize --effl 100.0 --max-totr 150.0 --achromatic --wavefront`
6. **深度像质评估 (MTF & 波前)**：`python zemax_cli.py evaluate --max-freq 50 --points 5`
7. **保存结果文件**：`python zemax_cli.py save outputs/final_design.zmx`

### `build` 命令 JSON 格式规范：
- 平面（无穷大曲率半径）必须填 `"radius": 0.0`。
- 不要手动写死 `"semi_diameter"`（保持省略或 `null`，让 Zemax 自动计算口径，防止人为切光）。
- 示例 JSON：
```json
{
  "epd": 25.0,
  "fovs": [0.0, 2.0, 4.0],
  "afocal_mode": false,
  "surfaces": [
    {"comment": "物镜前表面", "radius": 65.0, "thickness": 6.0, "material": "N-BK7", "is_stop": true, "radius_variable": true, "thickness_variable": false},
    {"comment": "胶合面", "radius": -45.0, "thickness": 3.0, "material": "N-F2", "radius_variable": true, "thickness_variable": false},
    {"comment": "物镜后表面", "radius": -130.0, "thickness": 95.0, "material": "", "radius_variable": true, "thickness_variable": true}
  ]
}

```

---

## 二、 光学物理分阶段工作流 (Phased Workflow)

### Phase 1: 初始结构获取（计算或借用 Samples）

* **简单系统（单透镜/双胶合/望远镜）**：先用高斯光学薄透镜公式算出理论焦距与空气间隔。赋予初始曲率时遵循“平缓弯曲原则”：初始曲率半径绝对值必须大于入瞳直径的 1.5 倍（$|R| > 1.5 \times \text{EPD}$），严禁给极小曲率半径触发全反射（TIR，导致评价函数变成 `9E9`）。
* **复杂系统（双高斯/反远距/大视场镜头）**：优先使用 `load` 命令读取本机 `Documents\Zemax\Samples\Sequential\Objectives\` 下的官方经典 `.zmx` 样例作为 Baseline，再修改优化。

### Phase 2: 渐进式约束优化 (`optimize`)

* `optimize` 会按本次请求重建评价函数。需要保留原有 MFE 时先 `save`；自定义约束置于 `DMFS` 上方。
* **自定义操作数行引用**：不要在 `--custom-op` 中直接复用旧 MFE 的 OPLT、OPGT、OPVA、DIFF、SUMM、GOTO 等绝对行号。内置约束会随请求改变行布局，客户端不会自动重映射引用；总长和后截距优先使用 `--max-totr`、`--min-bfl`。
* **有焦系统铁律**：调用 `optimize` 时**必须带上 `--effl <目标焦距>` 参数**！绝不允许在不锁定焦距的情况下裸跑优化。
* **无焦系统（如开普勒/伽利略望远镜）铁律**：**严禁一上来就开启 `afocal_mode: true` 全局盲目优化！** 必须拆分为：
1. 先按有焦模式独立构建并优化物镜（保存参数）。
2. 再按有焦模式独立构建并优化目镜（保存参数）。
3. 计算两者焦点重合的空气间隔（$d = f'_{obj} + f'_{eye}$），通过 `build` 拼接并开启 `"afocal_mode": true`，最后通过 `--custom-op` 控制角放大率 `PMAG` 或出瞳距 `EXPP` 进行微调。


* **遭遇 `9E9` 报错处理**：若 `optimize` 返回光线追迹失败，严禁原地重试优化！必须用 `patch` 放大弯曲过度的表面 `radius`，直到 `status` 返回 `"ray_trace_valid": true`。

### Phase 3: 像质验收与衍射极限判据 (`evaluate`)

优化收敛后，必须运行 `python zemax_cli.py evaluate` 进行光学体检：

1. **结果有效性**：先检查 `status`、`evaluation_valid` 和各视场的 `wavefront_valid`。RWCE 为零时视为待确认，不得按衍射极限验收；`strehl_ratio_approx=null` 表示无法评估。
2. **焦距核对**：确认 `effective_focal_length_mm` 与目标焦距误差 `< 1%`。
3. **马瑞（Maréchal）衍射极限判据**：检查 `wavefront_summary` 中的 `rms_wavefront_waves`。若 `< 0.0715`（`diffraction_limited: true`），则已达衍射极限；若点列图优化后波前差仍大于 `0.15`，应执行 `python zemax_cli.py optimize --effl <焦距> --achromatic --wavefront` 切换为波前差基底二次精修。
4. **MTF 采样**：`evaluate --mtf-sampling 3` 默认使用采样级别 3。实际曲线采用 `Grid=0` 稀疏积分，不能将级别解释成固定 FFT 网格尺寸；应对同一镜头逐级比较 2、3、4 的返回值，再判断是否满足所需精度。

