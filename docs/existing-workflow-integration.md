# 与现有工作流的旁路融合方案

目标是让评测引擎提供事实和可发布素材，但不成为第二套内容中台、审批系统或生产状态机。

## 融合原则

1. **不改现有 Skill**：评测引擎只读取自己的运行包，输出标准回执；不复制、修改或上传已安装 Skill。
2. **不改现有库表**：引擎不写选题库、历史库、竞品库或生产状态。正式准入仍由现有原子准入入口执行。
3. **不新增用户审批**：评测完成不是文本批准、生产许可或发布许可。它只为现有完整文本提供事实输入。
4. **一个评测作品一个公版仓库**：每个仓库可单独传播、复现和统计；后续可再建一个只含链接的总索引仓库。
5. **公开与内部完全分层**：内部原始日志、绝对路径、录制门禁、私有素材留在本地；GitHub 只放原创代码、脱敏示例、窄结论和来源边界。

## 唯一接点

```text
GitHub 候选发现
  → 固定仓库/提交/许可证
  → 隔离安装与同任务实跑
  → EVALUATION-RECEIPT（事实源）
      ├─ export-handoff → 现有 FLOW 完整文本、审核、用户批准、准入与生产
      └─ package-work   → 单作品 GitHub 公版仓库（草稿或待发布）
```

现有工作流只消费 `GITHUB-AI-EVALUATION-HANDOFF-1.0`。该文件明确声明：不写现有库表、不创建批准、不授予生产或发布权限。这样即使评测引擎升级，现有流程也不用改。

## 字段映射

| 评测事实 | 现有 FLOW 消费位置 | GitHub 公版位置 |
|---|---|---|
| 普通人的冻结任务 | `真实任务与用户价值` | `workflow_asset/benchmark-case.json` |
| 固定仓库、提交、许可证 | 来源与权利边界 | `PROVENANCE.md` |
| 实际结果与人工判断 | 任务交付、证据决策 | `examples/output/`、`evaluation/report.md` |
| 可复用评测方法 | `双成果交付` 的 reusable asset | `code/` 与 README |
| 允许公开的窄结论 | 公开主张与证据矩阵 | `task_delivery/claim-boundary.json` |
| 是否有可视原生过程 | 结果证据是否需要录制 | 只记录状态；无 QA 不声称 native run |

## 推荐运行顺序

```bash
github-ai-bench validate-run --run-dir runs/OWNER__REPO --output runs/OWNER__REPO/EVALUATION-RECEIPT.json

github-ai-bench export-handoff --run-dir runs/OWNER__REPO --output runs/OWNER__REPO/FLOW-HANDOFF.json

github-ai-bench package-work --run-dir runs/OWNER__REPO --manifest runs/OWNER__REPO/PUBLICATION.json --output-dir public-works/ai-skill-lab-project-slug
```

`export-handoff` 的输出进入现有完整文本路线；`package-work` 的输出是独立发布物。二者没有先后强制关系，但公开仓库包只接受已通过的评测证据。

## 每作品仓库结构

```text
README_zh.md
LICENSE
SECURITY.md
PROVENANCE.md
PUBLICATION-MANIFEST.json
PUBLIC-BUNDLE-RECEIPT.json
code/                         # 我们原创的最小代码
examples/input/               # 脱敏输入
examples/output/              # 脱敏实际输出
evaluation/EVALUATION-RECEIPT.json
evaluation/report.md
workflow_asset/benchmark-case.json
task_delivery/claim-boundary.json
```

默认命名建议：`ai-skill-lab-<project-slug>`。仓库标题先卖普通人的结果变化，项目名和技术细节放在证明层；不要把内部 schema、门禁或“工作流箭头”当引流标题。

## 发布状态

- `draft_ready`：公版包已验证，但缺少精确 GitHub 所有者/仓库名或当前上传授权；不得远程创建或推送。
- `ready_for_publish`：精确公开仓库目标、当前授权、权利核对和安全扫描全部通过；可以执行远程创建和推送。
- `published`：仅在远端创建、推送并核对默认分支和公开 URL 后记录。打包工具不会用“已打包”冒充“已发布”。

## 不能进入公开仓库的内容

- 被评项目源码或安装后的第三方 Skill 文件；
- 内部门禁、审批回执、账号素材和私有工作流实现；
- 未脱敏日志、客户材料、Cookie、Token、密钥和本机绝对路径；
- 无权再发布的截图、字体、模型权重、商标素材或示例数据；
- README/作者 Demo 冒充我方实测的描述；
- “一定爆款”“所有场景稳定”等超出单次证据的结论。

