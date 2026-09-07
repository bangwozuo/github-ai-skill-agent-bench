# GitHub AI Skill / Agent 爆款评测引擎

从 GitHub 候选发现，到隔离安装、真实任务跑测、结果展示、结论和建议，一套证据链走到底。它不把 Star 当质量，不把 README 当实测，也不承诺“爆款”。

## 它解决的核心问题

- 同时发现老牌高热度项目与近期加速项目；
- 用两次同口径快照描述增量，避免拿仓库年龄冒充趋势；
- 先查官方仓库、许可证、README、归档状态和固定提交，再决定是否安装；
- 把不受信任代码放进一次性容器/虚拟机，不在日常工作主机直装；
- 用一份冻结真实任务跑测，保存环境、安装日志、运行日志和实际结果；
- 由人给出 `采用 / 限定试用 / 不适合本题`，不用总分掩盖硬伤；
- 自动生成可复核报告和诚实的 AS-C 评测内容候选。
- 用只读交接回执接入现有 FLOW 文本/审批/生产路线，不改现有 Skill、库表或状态机；
- 把每个验收通过的作品打成一个独立 GitHub 公版仓库，只含原创代码、脱敏示例和证据边界。

完整设计见 [`task_delivery/engine-design.md`](task_delivery/engine-design.md)，流程图见 [`workflow_asset/workflow-map.md`](workflow_asset/workflow-map.md)。
旁路融合与单作品仓库规范见 [`docs/existing-workflow-integration.md`](docs/existing-workflow-integration.md)。

## 快速开始

```bash
python -m pip install -e .

# 1. 获取当前候选快照
github-ai-bench discover \
  --config workflow_asset/discovery-config.json \
  --output runs/discovery-01.json

# 2. 一段时间后再取一次；只有两个快照才谈增长
github-ai-bench compare \
  --previous runs/discovery-01.json \
  --current runs/discovery-02.json \
  --output runs/momentum.json

# 3. 用硬门槛和独立信号形成复现队列
github-ai-bench shortlist \
  --snapshot runs/discovery-02.json \
  --momentum runs/momentum.json \
  --config workflow_asset/discovery-config.json \
  --output runs/shortlist.json

# 4. 冻结一个官方仓库和当前提交
github-ai-bench freeze-candidate \
  --repo OWNER/REPO \
  --output runs/OWNER__REPO-candidate.json

# 5. 创建隔离复现包
github-ai-bench init-run \
  --candidate runs/OWNER__REPO-candidate.json \
  --case workflow_asset/benchmark-case.json \
  --run-dir runs/OWNER__REPO \
  --run-id EVAL-001
```

随后在一次性容器或虚拟机里按官方文档安装和运行，把环境、输入、原始日志和结果保存到该运行目录。不要把未审查的安装脚本直接跑在日常主机。

将 [`workflow_asset/result-review-template.json`](workflow_asset/result-review-template.json) 复制到运行目录，逐项对应基准题里的验收 ID。成功运行的每项都必须有 `pass` 和具体结果证据；失败或阻塞运行需要明确标出 `fail` 或 `not_run`。

```bash
github-ai-bench record-run \
  --run-dir runs/OWNER__REPO \
  --status succeeded \
  --environment runs/OWNER__REPO/raw/environment.json \
  --install-log runs/OWNER__REPO/raw/install.log \
  --input workflow_asset/benchmark-case.json \
  --raw-log runs/OWNER__REPO/raw/run.log \
  --result runs/OWNER__REPO/results/result.json \
  --result-review runs/OWNER__REPO/results/result-review.json \
  --human-verdict trial \
  --claim-type actual_result \
  --claim-text "在固定提交、当前隔离环境和这份输入下生成了可用结果，仍需人工核对优先级"

github-ai-bench validate-run \
  --run-dir runs/OWNER__REPO \
  --output runs/OWNER__REPO/EVALUATION-RECEIPT.json

github-ai-bench report \
  --run-dir runs/OWNER__REPO \
  --output runs/OWNER__REPO/report.md

# 6. 生成只读交接回执，交给现有 FLOW 完整文本路线
github-ai-bench export-handoff \
  --run-dir runs/OWNER__REPO \
  --output runs/OWNER__REPO/FLOW-HANDOFF.json

# 7. 为这个评测作品生成独立 GitHub 公版仓库包
github-ai-bench package-work \
  --run-dir runs/OWNER__REPO \
  --manifest runs/OWNER__REPO/PUBLICATION.json \
  --output-dir public-works/ai-skill-lab-project-slug
```

Windows PowerShell 可以将每条命令写成一行。公开仓库请求可匿名调用；使用 `GITHUB_TOKEN` 只为提高只读 API 限额，运行候选代码时不要把该 Token 传进隔离环境。

## 决策规则

引擎不计算“综合 92 分”。它保留四组独立事实：

- 热度：当前 Star/Fork、两个快照间的增量、近期维护；
- 可复现：许可证、固定提交、文档、环境、安装日志；
- 真实适用：冻结输入、实际结果、人工接管和可继续使用的交付；
- 内容潜力：普通用户任务、5—10 秒可见证明、与已有内容的实质差异。

许可证未知、版本不一致、输入错版、日志缺失、结果不可用或主张越界，任一项都直接 HOLD，其他亮点不能抵消。

## 公开结论的强度

| 证据 | 可以说 | 不可以说 |
|---|---|---|
| 仓库快照 | “当前有 N Star” | “因此一定好用/会爆” |
| README/作者 Demo | “官方展示了某能力” | “我们已实测” |
| 本地结果+日志 | “当前提交、环境、输入下得到该结果” | “所有任务都稳定” |
| 失败日志 | “按官方步骤失败到某阶段” | “这个项目一直不行” |
| 可视过程+QA | “本次原生过程已记录” | 用重做界面冒充原生运行 |

## 安全与权利边界

安全说明见 [`SECURITY.md`](SECURITY.md) 和 [`docs/safe-reproduction.md`](docs/safe-reproduction.md)。仓库许可证识别不自动覆盖依赖、模型权重、素材、商标或示例数据；公开衍生物需要单独复核。

`package-work` 只生成本地公版包。精确 GitHub 账号/组织、仓库名和当次上传授权齐全时，状态才是 `ready_for_publish`；否则是 `draft_ready`。远程创建、推送和发布后核对是独立动作，不会把“已打包”冒充“已发布”。
